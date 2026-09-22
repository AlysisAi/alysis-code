package com.alysis.ide;

import com.google.gson.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;

/** One owned process, bounded JSONL frames, correlated requests, no terminal scraping. */
public final class StdioClient implements AutoCloseable {
    private static final int MAX_FRAME = 8 * 1024 * 1024;
    private final Process process;
    private final Writer input;
    private final Map<String, CompletableFuture<JsonObject>> pending = new ConcurrentHashMap<>();
    private final AtomicLong sequence = new AtomicLong();
    private final AtomicBoolean disconnected = new AtomicBoolean();
    private final Consumer<JsonObject> events;
    private final Consumer<String> exited;
    private volatile boolean closing;

    public StdioClient(List<String> command, Path workspace, Consumer<JsonObject> events,
                       Consumer<String> exited) throws IOException {
        this.events = events;
        this.exited = exited;
        // ProcessBuilder uses an argument array. No shell or workspace-provided executable lookup.
        ProcessBuilder builder = new ProcessBuilder(command).directory(workspace.toFile());
        builder.environment().put("PYTHONUTF8", "1");
        builder.environment().put("PYTHONIOENCODING", "utf-8");
        process = builder.start();
        input = new BufferedWriter(new OutputStreamWriter(process.getOutputStream(), StandardCharsets.UTF_8));
        daemon("Alysis protocol", this::readOutput);
        daemon("Alysis stderr", () -> {
            try (InputStream stream = process.getErrorStream()) {
                // Diagnostics can include provider details. Drain without logging or forwarding them.
                byte[] buffer = new byte[4096];
                while (stream.read(buffer) >= 0) { }
            } catch (IOException ignored) { }
        });
    }

    public boolean isAlive() { return process.isAlive() && !closing; }

    public CompletableFuture<JsonObject> request(String method, JsonObject params) {
        if (!isAlive()) return CompletableFuture.failedFuture(new IOException("The local agent is disconnected."));
        if (pending.size() >= 64) return CompletableFuture.failedFuture(new IOException("Too many pending agent requests."));
        String id = "jb-" + sequence.incrementAndGet();
        JsonObject request = new JsonObject();
        request.addProperty("protocol_version", "1");
        request.addProperty("id", id);
        request.addProperty("method", method);
        request.add("params", params);
        String frame = request.toString();
        if (frame.getBytes(StandardCharsets.UTF_8).length > 1024 * 1024)
            return CompletableFuture.failedFuture(new IOException("Agent request is too large."));
        CompletableFuture<JsonObject> result = new CompletableFuture<>();
        pending.put(id, result);
        result.orTimeout(90, TimeUnit.SECONDS).whenComplete((value, failure) -> {
            pending.remove(id);
            if (failure instanceof TimeoutException)
                daemon("Alysis timeout", () -> disconnect("The local agent took too long to respond. Reconnect to start a new conversation."));
        });
        try {
            synchronized (input) { input.write(frame); input.write('\n'); input.flush(); }
        } catch (IOException error) { result.completeExceptionally(new IOException("Could not send to the local agent.")); }
        return result;
    }

    private void readOutput() {
        try (Reader reader = new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8)) {
            StringBuilder line = new StringBuilder();
            char[] buffer = new char[4096];
            int read;
            while ((read = reader.read(buffer)) >= 0) {
                for (int i = 0; i < read; i++) {
                    char c = buffer[i];
                    if (c == '\n') {
                        if (line.length() > 0) receive(line.toString());
                        line.setLength(0);
                    } else if (c != '\r') {
                        if (line.length() >= MAX_FRAME) throw new IOException("Oversized protocol frame.");
                        line.append(c);
                    }
                }
            }
        } catch (Exception ignored) {
            // Do not expose raw malformed frames, parser snippets, stderr or credentials.
        } finally {
            pending.values().forEach(f -> f.completeExceptionally(new IOException("The local agent disconnected.")));
            disconnect("The local agent disconnected. Reconnect to start a new conversation.");
        }
    }

    private void disconnect(String reason) {
        if (closing || !disconnected.compareAndSet(false, true)) return;
        closing = true;
        exited.accept(reason);
        pending.values().forEach(f -> f.completeExceptionally(new IOException("The local agent disconnected.")));
        terminateOwnedProcess();
    }

    private void receive(String line) throws IOException {
        JsonObject frame;
        try { frame = JsonParser.parseString(line).getAsJsonObject(); }
        catch (RuntimeException e) { throw new IOException("Malformed protocol frame."); }
        if (!"1".equals(string(frame, "protocol_version"))) throw new IOException("Incompatible protocol version.");
        if (frame.has("id")) {
            CompletableFuture<JsonObject> future = pending.get(string(frame, "id"));
            if (future == null) return;
            if (frame.has("ok") && frame.get("ok").isJsonPrimitive() && frame.get("ok").getAsBoolean()) {
                if (!frame.has("result") || !frame.get("result").isJsonObject()) throw new IOException("Malformed protocol result.");
                future.complete(frame.getAsJsonObject("result"));
            } else {
                JsonObject error = frame.has("error") && frame.get("error").isJsonObject() ? frame.getAsJsonObject("error") : new JsonObject();
                String message = string(error, "message");
                future.completeExceptionally(new IOException(message.isEmpty() ? "The local agent rejected the request." : message.substring(0, Math.min(4000, message.length()))));
            }
        } else if (frame.has("type") && frame.has("session_id") && frame.has("sequence")) events.accept(frame);
    }

    public static String string(JsonObject value, String key) {
        return value.has(key) && value.get(key).isJsonPrimitive() ? value.get(key).getAsString() : "";
    }

    @Override public void close() {
        if (closing) return;
        try {
            if (process.isAlive()) request("bridge.shutdown", new JsonObject()).get(2, TimeUnit.SECONDS);
        } catch (Exception ignored) { }
        closing = true;
        try { input.close(); } catch (IOException ignored) { }
        try { process.waitFor(2, TimeUnit.SECONDS); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        terminateOwnedProcess();
        pending.values().forEach(f -> f.completeExceptionally(new IOException("The local agent was stopped.")));
    }

    private void terminateOwnedProcess() {
        if (!process.isAlive()) return;
        List<ProcessHandle> children = process.descendants().toList();
        children.forEach(ProcessHandle::destroy);
        process.destroy();
        try { process.waitFor(1, TimeUnit.SECONDS); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        children.stream().filter(ProcessHandle::isAlive).forEach(ProcessHandle::destroyForcibly);
        if (process.isAlive()) process.destroyForcibly();
    }

    private static void daemon(String name, Runnable action) {
        Thread thread = new Thread(action, name); thread.setDaemon(true); thread.start();
    }
}

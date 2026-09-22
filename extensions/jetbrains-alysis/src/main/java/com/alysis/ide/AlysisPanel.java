package com.alysis.ide;

import com.google.gson.*;
import com.intellij.ide.BrowserUtil;
import com.intellij.openapi.ide.CopyPasteManager;
import com.intellij.ide.impl.TrustedProjects;
import com.intellij.ide.util.PropertiesComponent;
import com.intellij.openapi.Disposable;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.fileChooser.FileChooser;
import com.intellij.openapi.fileChooser.FileChooserDescriptorFactory;
import com.intellij.openapi.fileEditor.OpenFileDescriptor;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.ui.Messages;
import com.intellij.openapi.util.Disposer;
import com.intellij.openapi.vfs.LocalFileSystem;
import com.intellij.ui.jcef.*;
import com.intellij.util.ui.UIUtil;
import org.cef.browser.CefBrowser;
import org.cef.browser.CefFrame;
import org.cef.handler.CefRequestHandlerAdapter;
import org.cef.network.CefRequest;
import javax.swing.JComponent;
import java.awt.Color;
import java.awt.datatransfer.StringSelection;
import java.io.*;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.*;

/** Native authority boundary; browser messages cannot select a process, workspace or arbitrary RPC. */
public final class AlysisPanel implements Disposable {
    private static final String CLI_PROPERTY = "alysis.developmentCli";
    private static final Set<String> METHODS = Set.of("initialize", "session.create", "chat.send",
        "session.cancel", "approval.respond", "job.status", "session.setMode", "session.modelInfo");
    private final Project project;
    private final JBCefBrowser browser = new JBCefBrowser();
    private final JBCefJSQuery query = JBCefJSQuery.create((JBCefBrowserBase) browser);
    private final ExecutorService executor = new ThreadPoolExecutor(1, 1, 0L, TimeUnit.MILLISECONDS,
        new ArrayBlockingQueue<>(64), r -> {
        Thread t = new Thread(r, "Alysis IDE actions"); t.setDaemon(true); return t;
    });
    private final Object connectionLock = new Object();
    private final Map<String, JsonObject> approvals = new ConcurrentHashMap<>();
    private volatile StdioClient client;
    private volatile String sessionId = "";
    private volatile String jobId = "";
    private volatile int epoch;
    private volatile boolean disposed;

    public AlysisPanel(Project project) {
        this.project = project;
        Disposer.register(this, browser);
        Disposer.register(this, query);
        query.addHandler(payload -> { receive(payload); return null; });
        browser.getJBCefClient().addRequestHandler(new CefRequestHandlerAdapter() {
            @Override public boolean onBeforeBrowse(CefBrowser source, CefFrame frame, CefRequest request,
                                                     boolean userGesture, boolean redirect) {
                return !"about:blank".equals(request.getURL());
            }
        }, browser.getCefBrowser());
        try { browser.loadHTML(html()); }
        catch (IOException failure) { browser.loadHTML("<p>Alysis Code sidebar resources are missing. Rebuild the plugin.</p>"); }
    }

    public JComponent component() { return browser.getComponent(); }

    private void receive(String payload) {
        if (disposed || payload.length() > 150000) return;
        JsonObject message;
        try { message = JsonParser.parseString(payload).getAsJsonObject(); }
        catch (RuntimeException ignored) { return; }
        String id = StdioClient.string(message, "id");
        if (!id.matches("ui-[0-9]{1,12}")) return;
        if ("native".equals(StdioClient.string(message, "type"))) {
            ApplicationManager.getApplication().invokeLater(() -> {
                if (disposed || project.isDisposed()) return;
                try { respond(id, nativeAction(message.getAsJsonObject("action")), null); }
                catch (Exception failure) { respond(id, null, safeMessage(failure)); }
            });
        } else if ("rpc".equals(StdioClient.string(message, "type"))) {
            try { executor.execute(() -> {
                int generation = epoch;
                try {
                    requireTrustedProject();
                    String method = StdioClient.string(message, "method");
                    if (!METHODS.contains(method)) throw new IOException("This agent action is not supported by the JetBrains preview.");
                    JsonObject params = message.has("params") && message.get("params").isJsonObject()
                        ? message.getAsJsonObject("params").deepCopy() : new JsonObject();
                    CompletableFuture<JsonObject> response;
                    synchronized (connectionLock) {
                        if (disposed || generation != epoch) throw new IOException("The agent connection changed. Try again.");
                        validateParams(method, params);
                        StdioClient target = ensureClient(method);
                        generation = epoch;
                        response = target.request(method, params);
                    }
                    // Serialize ownership transitions off the UI thread. A second task cannot
                    // pass validation before the first chat.send has assigned its job ID.
                    JsonObject result = response.get();
                    synchronized (connectionLock) {
                        if (disposed || generation != epoch) return;
                        if (method.equals("session.create")) {
                            sessionId = StdioClient.string(result, "session_id"); jobId = ""; approvals.clear();
                        } else if (method.equals("chat.send")) jobId = StdioClient.string(result, "job_id");
                        else if (method.equals("job.status") && Set.of("completed", "failed", "cancelled", "interrupted").contains(StdioClient.string(result, "status"))) { jobId = ""; approvals.clear(); }
                        else if (method.equals("approval.respond")) approvals.remove(StdioClient.string(params, "approval_id"));
                        else if (method.equals("session.cancel") && "closed".equals(StdioClient.string(result, "status"))) { sessionId = ""; jobId = ""; approvals.clear(); }
                        respond(id, result, null);
                    }
                } catch (Exception failure) {
                    if (!disposed) respond(id, null, safeMessage(failure));
                    if (failure instanceof InterruptedException) Thread.currentThread().interrupt();
                }
            }); } catch (RejectedExecutionException failure) {
                respond(id, null, "Too many pending IDE actions. Wait for the current action to finish.");
            }
        }
    }

    private void requireTrustedProject() throws IOException {
        if (disposed || project.isDisposed() || project.getBasePath() == null) throw new IOException("Open a local project first.");
        if (!TrustedProjects.isTrusted(project)) throw new IOException("Trust this project in the IDE before starting Alysis Code.");
    }

    private StdioClient ensureClient(String method) throws IOException {
        if (client != null && client.isAlive()) return client;
        if (!method.equals("initialize")) throw new IOException("Connect to the local agent first.");
        Path executable = selectedExecutable();
        int generation = ++epoch;
        sessionId = ""; jobId = ""; approvals.clear();
        client = new StdioClient(List.of(executable.toString(), "ide-bridge", "--stdio"),
            Path.of(project.getBasePath()).toRealPath(), event -> {
                if (disposed || epoch != generation || !sessionId.equals(StdioClient.string(event, "session_id"))) return;
                if (!jobId.isEmpty() && event.has("job_id") && !event.get("job_id").isJsonNull() && !jobId.equals(StdioClient.string(event, "job_id"))) return;
                if ("prompt_for_input".equals(StdioClient.string(event, "type")) && event.has("payload") && event.get("payload").isJsonObject()) {
                    JsonObject payload = event.getAsJsonObject("payload");
                    if ("approval".equals(StdioClient.string(payload, "kind")) && approvals.size() < 64)
                        approvals.put(StdioClient.string(payload, "approval_id"), payload.deepCopy());
                }
                JsonObject envelope = new JsonObject(); envelope.addProperty("type", "native.event"); envelope.add("event", event); send(envelope);
            }, reason -> {
                synchronized (connectionLock) {
                    if (disposed || epoch != generation) return;
                    ++epoch; client = null; sessionId = ""; jobId = ""; approvals.clear();
                    JsonObject envelope = new JsonObject(); envelope.addProperty("type", "native.disconnected"); envelope.addProperty("message", reason); send(envelope);
                }
            });
        return client;
    }

    private Path selectedExecutable() throws IOException {
        String configured = PropertiesComponent.getInstance().getValue(CLI_PROPERTY, "");
        if (configured.isBlank()) throw new IOException("Choose your configured Alysis CLI executable first.");
        Path executable = Path.of(configured);
        if (!executable.isAbsolute() || !Files.isRegularFile(executable)) throw new IOException("The selected CLI executable is missing.");
        executable = executable.toRealPath();
        if (executable.startsWith(Path.of(project.getBasePath()).toRealPath())) throw new IOException("Choose an installed CLI outside this project.");
        String name = executable.getFileName().toString().toLowerCase(Locale.ROOT);
        if (name.endsWith(".cmd") || name.endsWith(".bat") || name.endsWith(".ps1")) throw new IOException("Choose a native Alysis executable, not a shell wrapper.");
        if (!Files.isExecutable(executable)) throw new IOException("The selected file is not executable.");
        return executable;
    }

    private void validateParams(String method, JsonObject params) throws IOException {
        Set<String> keys = switch (method) {
            case "initialize" -> Set.of();
            case "session.create" -> Set.of("mode");
            case "chat.send" -> Set.of("session_id", "message", "idempotency_key");
            case "session.cancel", "session.modelInfo" -> Set.of("session_id");
            case "session.setMode" -> Set.of("session_id", "mode", "workspace_trusted");
            case "job.status" -> Set.of("job_id");
            case "approval.respond" -> Set.of("session_id", "approval_id", "allow", "allow_for_session");
            default -> Set.of();
        };
        if (!keys.containsAll(params.keySet())) throw new IOException("Unexpected agent parameters.");
        if (method.equals("session.create")) {
            if (!sessionId.isEmpty()) throw new IOException("Close the current conversation before creating another.");
            params.addProperty("workspace", Path.of(project.getBasePath()).toRealPath().toString());
            params.addProperty("workspace_trusted", true);
        } else if (!method.equals("initialize") && !method.equals("job.status")) {
            if (sessionId.isEmpty() || !sessionId.equals(StdioClient.string(params, "session_id"))) throw new IOException("This conversation is no longer active.");
        }
        if (method.equals("session.create") || method.equals("session.setMode")) {
            if (!Set.of("readonly", "review", "auto").contains(StdioClient.string(params, "mode"))) throw new IOException("Unsupported permissions.");
            if (!jobId.isEmpty()) throw new IOException("Permissions cannot change while a task is running.");
            if (method.equals("session.setMode")) params.addProperty("workspace_trusted", true);
        }
        if (method.equals("chat.send")) {
            if (!jobId.isEmpty()) throw new IOException("Wait for the current task to finish.");
            String message = StdioClient.string(params, "message");
            String key = StdioClient.string(params, "idempotency_key");
            if (message.isBlank() || message.length() > 20000 || key.isEmpty() || key.length() > 128) throw new IOException("Invalid task submission.");
        }
        if (method.equals("job.status") && (jobId.isEmpty() || !jobId.equals(StdioClient.string(params, "job_id")))) throw new IOException("This task is no longer active.");
        if (method.equals("approval.respond")) {
            JsonObject approval = approvals.get(StdioClient.string(params, "approval_id"));
            if (approval == null) throw new IOException("This approval is no longer pending.");
            for (String key : List.of("allow", "allow_for_session")) {
                if (!params.has(key) || !params.get(key).isJsonPrimitive() || !params.getAsJsonPrimitive(key).isBoolean()) throw new IOException("Invalid approval decision.");
            }
            String expiry = StdioClient.string(approval, "expires_at");
            if (!expiry.isEmpty() && !Instant.parse(expiry).isAfter(Instant.now())) throw new IOException("This approval has expired.");
            if (params.get("allow_for_session").getAsBoolean() && (!approval.has("allow_for_session_supported") || !approval.get("allow_for_session_supported").getAsBoolean())) throw new IOException("This approval supports one action only.");
        }
    }

    private JsonElement nativeAction(JsonObject action) throws IOException {
        String type = StdioClient.string(action, "type");
        if (type.equals("configure")) {
            if (!jobId.isEmpty()) throw new IOException("Stop the task before changing the CLI.");
            var descriptor = FileChooserDescriptorFactory.createSingleFileDescriptor();
            descriptor.setTitle("Choose the Alysis CLI executable");
            var file = FileChooser.chooseFile(descriptor, project, null);
            if (file == null) return new JsonPrimitive(false);
            if (Messages.showYesNoDialog(project, "Use this executable as the development Alysis runtime?\n" + file.getPath()
                + "\nIt will run with your account and use your CLI provider configuration. It is not a verified managed runtime.",
                "Alysis Code development runtime", Messages.getQuestionIcon()) != Messages.YES) return new JsonPrimitive(false);
            PropertiesComponent.getInstance().setValue(CLI_PROPERTY, file.getPath());
            synchronized (connectionLock) {
                ++epoch; sessionId = ""; jobId = ""; approvals.clear();
                StdioClient previous = client; client = null;
                closeAsync(previous);
            }
            return new JsonPrimitive(true);
        }
        if (type.equals("clipboard.copy")) {
            String text = StdioClient.string(action, "text");
            if (text.length() > 100000) throw new IOException("The selection is too large.");
            CopyPasteManager.getInstance().setContents(new StringSelection(text));
            return new JsonPrimitive(true);
        }
        if (type.equals("open.external")) {
            String value = StdioClient.string(action, "url");
            if (value.length() > 2048) throw new IOException("The URL is too long.");
            URI uri = URI.create(value);
            if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null) throw new IOException("Only HTTPS links can be opened.");
            BrowserUtil.browse(uri); return new JsonPrimitive(true);
        }
        if (type.equals("open.file")) {
            requireTrustedProject();
            Path root = Path.of(project.getBasePath()).toRealPath();
            String value = StdioClient.string(action, "path");
            if (value.length() > 2048) throw new IOException("The path is too long.");
            Path path = root.resolve(value).toRealPath();
            if (!path.startsWith(root) || !Files.isRegularFile(path)) throw new IOException("Only files in the current project can be opened.");
            var file = LocalFileSystem.getInstance().refreshAndFindFileByNioFile(path);
            if (file != null) new OpenFileDescriptor(project, file).navigate(true);
            return new JsonPrimitive(file != null);
        }
        throw new IOException("This native action is not supported.");
    }

    private void respond(String id, JsonElement result, String error) {
        JsonObject envelope = new JsonObject(); envelope.addProperty("type", "native.result");
        envelope.addProperty("id", id); envelope.addProperty("ok", error == null);
        if (error == null) envelope.add("result", result == null ? JsonNull.INSTANCE : result);
        else envelope.addProperty("error", error);
        send(envelope);
    }

    private void send(JsonObject data) {
        ApplicationManager.getApplication().invokeLater(() -> {
            if (disposed || project.isDisposed()) return;
            // Gson escapes HTML-sensitive characters; never build JavaScript from unquoted user text.
            browser.getCefBrowser().executeJavaScript("window.dispatchEvent(new MessageEvent('message',{data:"
                + new Gson().toJson(data) + "}));", "about:blank", 0);
        });
    }

    private String html() throws IOException {
        String nonce = UUID.randomUUID().toString().replace("-", "");
        String document = resource("startView.html");
        String nativeScript = "window.alysisNativeSend=function(payload){" + query.inject("payload") + "};";
        String scripts = nativeScript + resource("portable-chat.js") + "\n" + resource("jetbrains-host.js") + "\n" + resource("startView.js");
        String csp = "default-src 'none'; script-src 'nonce-" + nonce + "'; style-src 'nonce-" + nonce + "'; img-src data:; base-uri 'none'; form-action 'none'; frame-src 'none'";
        document = document.replace("<link rel=\"stylesheet\" href=\"${styleUri}?v=${assetVersion}\">",
            "<style nonce=\"" + nonce + "\">" + theme() + resource("startView.css") + resource("jetbrains.css") + "</style>");
        document = document.replace("<script nonce=\"${nonce}\" src=\"${scriptUri}?v=${assetVersion}\"></script>",
            "<script nonce=\"" + nonce + "\">" + scripts.replace("</script", "<\\/script") + "</script>");
        String icon;
        try (InputStream input = AlysisPanel.class.getResourceAsStream("/alysis-logo.png")) {
            if (input == null) throw new IOException("Missing sidebar logo.");
            icon = Base64.getEncoder().encodeToString(input.readAllBytes());
        }
        return document.replace("${csp}", csp).replace("${markUri}", "data:image/png;base64," + icon);
    }

    private static String resource(String name) throws IOException {
        try (InputStream input = AlysisPanel.class.getResourceAsStream("/" + name)) {
            if (input == null) throw new IOException("Missing sidebar asset.");
            return new String(input.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static String theme() {
        String background = color(UIUtil.getPanelBackground());
        String foreground = color(UIUtil.getLabelForeground());
        return ":root{--vscode-sideBar-background:" + background + ";--vscode-sideBar-foreground:" + foreground
            + ";--vscode-foreground:" + foreground + ";--vscode-editor-background:" + background
            + ";--vscode-font-family:system-ui,sans-serif;--vscode-font-size:13px;--vscode-editor-font-family:monospace;"
            + "--vscode-editor-font-size:13px;--vscode-descriptionForeground:" + foreground
            + ";--vscode-button-background:#3574f0;--vscode-button-hoverBackground:#467ff2;--vscode-button-foreground:#fff;"
            + "--vscode-focusBorder:#3574f0;--vscode-input-background:" + background + ";--vscode-input-foreground:" + foreground
            + ";--vscode-input-border:#808080;--vscode-widget-border:#808080;--vscode-panel-border:#808080;"
            + "--vscode-list-hoverBackground:#80808022;--vscode-textLink-foreground:#589df6;--vscode-errorForeground:#e05b5b;}";
    }
    private static String color(Color color) { return String.format("#%02x%02x%02x", color.getRed(), color.getGreen(), color.getBlue()); }

    private static String safeMessage(Throwable error) {
        while (error.getCause() != null && error != error.getCause()) error = error.getCause();
        if (error instanceof IOException && error.getMessage() != null) return error.getMessage();
        if (error instanceof TimeoutException) return "The local agent took too long to respond. Reconnect and try again.";
        return "The IDE could not complete the action. Check the selected CLI and project.";
    }

    @Override public void dispose() {
        synchronized (connectionLock) {
            disposed = true; ++epoch;
            StdioClient previous = client; client = null;
            executor.shutdownNow();
            closeAsync(previous);
        }
    }

    private static void closeAsync(StdioClient previous) {
        if (previous == null) return;
        Thread thread = new Thread(previous::close, "Alysis agent shutdown");
        thread.setDaemon(true); thread.start();
    }
}

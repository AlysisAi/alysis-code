package com.alysis.ide;

import com.google.gson.*;
import java.io.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;

/** Test-only bridge from browser messages to the exact production Java transport. Not packaged. */
public class TransportHarness {
    public static void main(String[] args) throws Exception {
        Path workspace = Path.of(args[2]);
        try (StdioClient client = new StdioClient(List.of(args[0], args[1]), workspace,
            event -> { JsonObject value = new JsonObject(); value.addProperty("type", "native.event"); value.add("event", event); System.out.println(value); },
            reason -> { JsonObject value = new JsonObject(); value.addProperty("type", "native.disconnected"); value.addProperty("message", reason); System.out.println(value); })) {
            BufferedReader reader = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            for (String line; (line = reader.readLine()) != null;) {
                JsonObject input = JsonParser.parseString(line).getAsJsonObject();
                String method = StdioClient.string(input, "method");
                JsonObject params = input.getAsJsonObject("params");
                if (method.equals("session.create")) params.addProperty("workspace", workspace.toString());
                JsonObject output = new JsonObject(); output.addProperty("type", "native.result"); output.add("id", input.get("id"));
                try {
                    output.add("result", client.request(method, params).get(15, TimeUnit.SECONDS));
                    output.addProperty("ok", true);
                } catch (Exception failure) {
                    output.addProperty("ok", false);
                    output.addProperty("error", failure.getCause() == null ? failure.getMessage() : failure.getCause().getMessage());
                }
                System.out.println(output);
            }
        }
    }
}

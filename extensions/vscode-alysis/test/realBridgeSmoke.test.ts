import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { delimiter, resolve } from "node:path";
import { createInterface } from "node:readline";
import test from "node:test";

import { PROTOCOL_VERSION } from "../src/client/AlysisProtocol";

const BRIDGE_SMOKE_RESPONSE_TIMEOUT_MS = 20_000;
const BRIDGE_SMOKE_EXIT_TIMEOUT_MS = 10_000;

test("actual Python stdio bridge responds to non-model protocol requests", async () => {
  const repoRoot = resolve(process.cwd(), "../..");
  const python = pythonExecutable(repoRoot);
  const secret = "bridge-smoke-secret-value";
  const configDir = mkdtempSync(resolve(tmpdir(), "alysis-bridge-smoke-config-"));
  const dataDir = mkdtempSync(resolve(tmpdir(), "alysis-bridge-smoke-data-"));
  const workspace = mkdtempSync(resolve(tmpdir(), "alysis-bridge-smoke-workspace-"));
  const child = spawn(python, ["-m", "alysis_code.cli", "ide-bridge", "--stdio"], {
    cwd: repoRoot,
    env: {
      ...process.env,
      PYTHONPATH: [resolve(repoRoot, "src"), process.env.PYTHONPATH ?? ""].filter(Boolean).join(delimiter),
      ALYSIS_CONFIG_DIR: configDir,
      ALYSIS_DATA_DIR: dataDir,
      ALYSIS_SHELL_SANDBOX_MODE: "off",
      ALYSIS_API_KEY: secret
    },
    stdio: ["pipe", "pipe", "pipe"]
  });
  const stderr: string[] = [];
  const stdout: string[] = [];
  child.stderr.on("data", (chunk) => stderr.push(chunk.toString("utf8")));
  const responses = new Map<string | number | null, any>();
  const waiters = new Map<string | number | null, Array<(value: any) => void>>();
  createInterface({ input: child.stdout }).on("line", (line) => {
    stdout.push(line);
    const payload = JSON.parse(line) as { id: string | number | null };
    responses.set(payload.id, payload);
    for (const resolveWaiter of waiters.get(payload.id) ?? []) {
      resolveWaiter(payload);
    }
    waiters.delete(payload.id);
  });

  try {
    send(child, "init", "initialize");
    const init = await responseFor(responses, waiters, "init");
    assert.equal(init.ok, true, stderr.join(""));
    assert.equal(init.result.protocol_version, PROTOCOL_VERSION);

    send(child, "caps", "getCapabilities");
    const caps = await responseFor(responses, waiters, "caps");
    assert.equal(caps.ok, true, stderr.join(""));
    assert.equal(caps.result.transport, "stdio-jsonl");
    assert.equal(caps.result.methods.includes("session.list"), true);

    send(child, "sessions", "session.list");
    const sessions = await responseFor(responses, waiters, "sessions");
    assert.equal(sessions.ok, true, stderr.join(""));
    assert.deepEqual(sessions.result.sessions, []);

    send(child, "create", "session.create", {
      workspace,
      mode: "readonly",
      model: "bridge-smoke-model",
      base_url: "http://127.0.0.1:9/v1"
    });
    const created = await responseFor(responses, waiters, "create");
    assert.equal(created.ok, true, stderr.join(""));
    const sessionId = created.result.session_id;
    assert.equal(typeof sessionId, "string");

    send(child, "sessions-after-create", "session.list");
    const sessionsAfterCreate = await responseFor(responses, waiters, "sessions-after-create");
    assert.equal(sessionsAfterCreate.ok, true, stderr.join(""));
    assert.equal(
      sessionsAfterCreate.result.sessions.some((session: any) => session.session_id === sessionId && session.closed === false),
      true
    );

    send(child, "cancel", "session.cancel", { session_id: sessionId });
    const cancelled = await responseFor(responses, waiters, "cancel");
    assert.equal(cancelled.ok, true, stderr.join(""));
    assert.equal(cancelled.result.status, "closed");

    send(child, "sessions-after-cancel", "session.list");
    const sessionsAfterCancel = await responseFor(responses, waiters, "sessions-after-cancel");
    assert.equal(sessionsAfterCancel.ok, true, stderr.join(""));
    assert.equal(
      sessionsAfterCancel.result.sessions.some((session: any) => session.session_id === sessionId && session.closed === true),
      true
    );
    assert.equal(stdout.join("\n").includes(secret), false);
    assert.equal(stderr.join("\n").includes(secret), false);

    child.stdin.end();
    const exitCode = await waitForExit(child);
    assert.equal(exitCode, 0, stderr.join(""));
  } finally {
    await terminateChild(child);
    removeTempDirectory(configDir);
    removeTempDirectory(dataDir);
    removeTempDirectory(workspace);
  }
});

test("actual Python bridge completes a readonly agent turn against the mock provider", async () => {
  const repoRoot = resolve(process.cwd(), "../..");
  const python = pythonExecutable(repoRoot);
  const readme = "# Bridge agent smoke\n";
  const task = "Read existing README.md and report its contents.";
  const provider = await startReadmeProvider(readme, task);
  const baseUrl = provider.baseUrl;
  const workspace = mkdtempSync(resolve(tmpdir(), "alysis-bridge-agent-workspace-"));
  const configDir = mkdtempSync(resolve(tmpdir(), "alysis-bridge-agent-config-"));
  const dataDir = mkdtempSync(resolve(tmpdir(), "alysis-bridge-agent-data-"));
  writeFileSync(resolve(workspace, "README.md"), readme, "utf8");
  writeFileSync(
    resolve(configDir, "config.json"),
    JSON.stringify({
      base_url: baseUrl,
      model: "qa-mock-model",
      default_mode: "readonly",
      stream: false,
      routing_mode: "code_only",
      skills_enabled: false,
      update_check_enabled: false,
      max_steps: 4,
      profiles: {
        mock: {
          name: "mock",
          protocol: "openai_compat",
          base_url: baseUrl,
          api_key_env: "ALYSIS_API_KEY",
          default_model: "qa-mock-model",
          extra_headers: {}
        }
      },
      active_profile: "mock",
      default_workspace_path: workspace
    }),
    "utf8"
  );
  const secret = "bridge-agent-smoke-secret";
  const child = spawn(python, ["-m", "alysis_code.cli", "ide-bridge", "--stdio"], {
    cwd: repoRoot,
    env: {
      ...process.env,
      PYTHONPATH: [resolve(repoRoot, "src"), process.env.PYTHONPATH ?? ""].filter(Boolean).join(delimiter),
      ALYSIS_CONFIG_DIR: configDir,
      ALYSIS_DATA_DIR: dataDir,
      ALYSIS_API_KEY: secret,
      OPENAI_API_KEY: secret,
      ALYSIS_ROUTING_MODE: "code_only",
      ALYSIS_SHELL_SANDBOX_MODE: "off",
      ALYSIS_VERIFY_SANDBOX_MODE: "off",
      ALYSIS_SKILLS_ENABLED: "0",
      ALYSIS_UPDATE_CHECK_ENABLED: "0",
      ALYSIS_TUI: "0",
      NO_COLOR: "1"
    },
    stdio: ["pipe", "pipe", "pipe"]
  });
  const stderr: string[] = [];
  const stdout: string[] = [];
  child.stderr.on("data", (chunk) => stderr.push(chunk.toString("utf8")));
  const responses = new Map<string | number | null, any>();
  const waiters = new Map<string | number | null, Array<(value: any) => void>>();
  createInterface({ input: child.stdout }).on("line", (line) => {
    stdout.push(line);
    const payload = JSON.parse(line) as { id: string | number | null };
    responses.set(payload.id, payload);
    for (const resolveWaiter of waiters.get(payload.id) ?? []) {
      resolveWaiter(payload);
    }
    waiters.delete(payload.id);
  });

  try {
    send(child, "init", "initialize");
    assert.equal((await responseFor(responses, waiters, "init")).ok, true, stderr.join(""));

    send(child, "create", "session.create", {
      workspace,
      mode: "readonly",
      model: "qa-mock-model",
      base_url: baseUrl,
      stream: false,
      max_steps: 4,
      no_log: true
    });
    const created = await responseFor(responses, waiters, "create");
    assert.equal(created.ok, true, `${JSON.stringify(created)}\n${stderr.join("")}`);
    const sessionId = String(created.result.session_id);

    send(child, "chat", "chat.send", {
      session_id: sessionId,
      message: task
    });
    const started = await responseFor(responses, waiters, "chat");
    assert.equal(started.ok, true, stderr.join(""));
    const jobId = String(started.result.job_id);
    const terminal = await waitForTerminalJob(child, responses, waiters, jobId);
    assert.equal(terminal.result.status, "completed", `${JSON.stringify(terminal)}\n${provider.failures.join("\n")}`);
    assert.equal(terminal.result.exit_code, 0, `${JSON.stringify(terminal)}\n${provider.failures.join("\n")}`);
    provider.assertComplete();

    send(child, "events", "session.getEvents", { session_id: sessionId, max_events: 200 });
    const replay = await responseFor(responses, waiters, "events");
    assert.equal(replay.ok, true, stderr.join(""));
    const events = replay.result.events as Array<{ type?: string; payload?: Record<string, unknown> }>;
    const startedEvents = events.filter((event) => event.type === "tool_call_started");
    assert.equal(startedEvents.length, 1, JSON.stringify(events));
    assert.equal(startedEvents[0].payload?.name, "fs_read");
    const callId = String(startedEvents[0].payload?.call_id ?? "");
    assert.notEqual(callId, "", JSON.stringify(events));
    for (const type of ["tool_call_started", "tool_call_progress", "tool_call_completed"]) {
      assert.equal(
        events.filter((event) => event.type === type && event.payload?.call_id === callId).length,
        1,
        `${type} must be canonical and emitted once: ${JSON.stringify(events)}`
      );
    }
    const completion = events.find(
      (event) => event.type === "tool_call_completed" && event.payload?.call_id === callId
    );
    assert.equal(completion?.payload?.success, true, JSON.stringify(events));
    assert.equal(events.filter((event) => event.type === "message_end").length, 1, JSON.stringify(events));
    assert.equal(events.find((event) => event.type === "message_end")?.payload?.text, readme.trim());
    assert.equal(readFileSync(resolve(workspace, "README.md"), "utf8"), readme);
    assert.equal(stdout.join("\n").includes(secret), false);
    assert.equal(stderr.join("\n").includes(secret), false);

    send(child, "close", "session.cancel", { session_id: sessionId });
    assert.equal((await responseFor(responses, waiters, "close")).ok, true);
    child.stdin.end();
    assert.equal(await waitForExit(child), 0, stderr.join(""));
  } finally {
    await terminateChild(child);
    await provider.close();
    removeTempDirectory(workspace);
    removeTempDirectory(configDir);
    removeTempDirectory(dataDir);
  }
});

function pythonExecutable(repoRoot: string): string {
  if (process.env.PYTHON) {
    return process.env.PYTHON;
  }
  const local = process.platform === "win32"
    ? resolve(repoRoot, ".venv", "Scripts", "python.exe")
    : resolve(repoRoot, ".venv", "bin", "python");
  if (existsSync(local)) {
    return local;
  }
  for (const candidate of process.platform === "win32" ? ["python"] : ["python3", "python"]) {
    if (isPython3(candidate)) {
      return candidate;
    }
  }
  throw new Error("No Python 3 executable found for bridge smoke test. Set PYTHON or create .venv.");
}

function isPython3(command: string): boolean {
  const result = spawnSync(command, [
    "-c",
    "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
  ], { stdio: "ignore" });
  return !result.error && result.status === 0;
}

function send(
  child: ReturnType<typeof spawn>,
  id: string,
  method: string,
  params: Record<string, unknown> = {}
): void {
  if (!child.stdin) {
    throw new Error("bridge stdin is unavailable");
  }
  child.stdin.write(
    JSON.stringify({
      protocol_version: PROTOCOL_VERSION,
      id,
      method,
      params
    }) + "\n"
  );
}

function removeTempDirectory(path: string): void {
  try {
    rmSync(path, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  } catch {
    // Test cleanup is best effort, especially on Windows where process handles may close late.
  }
}

function responseFor(
  responses: Map<string | number | null, any>,
  waiters: Map<string | number | null, Array<(value: any) => void>>,
  id: string
): Promise<any> {
  const existing = responses.get(id);
  if (existing) {
    return Promise.resolve(existing);
  }
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(
      () => reject(new Error(`timed out waiting for ${id}`)),
      BRIDGE_SMOKE_RESPONSE_TIMEOUT_MS
    );
    const wrapped = (value: any) => {
      clearTimeout(timeout);
      resolve(value);
    };
    waiters.set(id, [...(waiters.get(id) ?? []), wrapped]);
  });
}

async function waitForTerminalJob(
  child: ReturnType<typeof spawn>,
  responses: Map<string | number | null, any>,
  waiters: Map<string | number | null, Array<(value: any) => void>>,
  jobId: string
): Promise<any> {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    const requestId = `job-${attempt}`;
    send(child, requestId, "job.status", { job_id: jobId });
    const response = await responseFor(responses, waiters, requestId);
    if (["completed", "failed", "cancelled"].includes(String(response.result?.status))) {
      return response;
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 50));
  }
  throw new Error(`job ${jobId} did not reach a terminal state`);
}

async function startReadmeProvider(expectedContents: string, expectedTask: string) {
  const callId = "readme-smoke-call";
  const failures: string[] = [];
  let requestCount = 0;
  let toolResultReceived = false;
  // This fixture tests the bridge's protocol sequence, not model prompt interpretation.
  const server = createServer(async (request, response) => {
    try {
      assert.equal(request.method, "POST");
      assert.equal(request.url, "/v1/chat/completions");
      const chunks: Buffer[] = [];
      for await (const chunk of request) {
        chunks.push(Buffer.from(chunk));
      }
      const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      assert.equal(payload.model, "qa-mock-model");
      const messages = payload.messages as Array<Record<string, any>>;
      const toolResults = messages.filter((message) => message.role === "tool");
      requestCount += 1;
      let message: Record<string, unknown>;
      if (requestCount === 1) {
        assert.equal(toolResults.length, 0);
        assert.equal(messages.some((item) => item.role === "user" && item.content === expectedTask), true);
        assert.equal(payload.tools.some((tool: any) => tool.function?.name === "fs_read"), true);
        message = {
          role: "assistant",
          content: "",
          tool_calls: [{
            id: callId,
            type: "function",
            function: { name: "fs_read", arguments: JSON.stringify({ path: "README.md" }) }
          }]
        };
      } else {
        assert.equal(requestCount, 2, "unexpected request after the final response");
        assert.equal(toolResults.length, 1);
        assert.equal(toolResults[0].tool_call_id, callId);
        const result = JSON.parse(toolResults[0].content);
        assert.equal(result.path, "README.md");
        assert.equal(result.content, expectedContents);
        assert.equal(result.truncated, false);
        toolResultReceived = true;
        message = { role: "assistant", content: result.content };
      }
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        id: `bridge-smoke-${requestCount}`,
        object: "chat.completion",
        model: "qa-mock-model",
        choices: [{ index: 0, message, finish_reason: requestCount === 1 ? "tool_calls" : "stop" }],
        usage: { prompt_tokens: 18, completion_tokens: 6, total_tokens: 24 }
      }));
    } catch (error) {
      failures.push(String(error));
      response.writeHead(500, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { message: "Bridge smoke protocol assertion failed" } }));
    }
  });
  await new Promise<void>((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.listen(0, "127.0.0.1", resolveListen);
  });
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  return {
    baseUrl: `http://127.0.0.1:${address.port}/v1`,
    failures,
    assertComplete: () => {
      assert.deepEqual(failures, []);
      assert.equal(requestCount, 2);
      assert.equal(toolResultReceived, true);
    },
    close: () => new Promise<void>((resolveClose, rejectClose) => {
      server.closeAllConnections();
      server.close((error) => error ? rejectClose(error) : resolveClose());
    })
  };
}

function waitForExit(child: ReturnType<typeof spawn>): Promise<number | null> {
  return new Promise((resolveExit, rejectExit) => {
    const timeout = setTimeout(
      () => rejectExit(new Error("bridge did not exit cleanly")),
      BRIDGE_SMOKE_EXIT_TIMEOUT_MS
    );
    child.once("exit", (code) => {
      clearTimeout(timeout);
      resolveExit(code);
    });
  });
}

function terminateChild(child: ReturnType<typeof spawn>): Promise<void> {
  if (child.exitCode !== null || child.killed) {
    return Promise.resolve();
  }
  return new Promise((resolveTerminate) => {
    const timeout = setTimeout(resolveTerminate, 1000);
    child.once("exit", () => {
      clearTimeout(timeout);
      resolveTerminate();
    });
    child.kill();
  });
}

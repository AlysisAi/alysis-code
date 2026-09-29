#!/usr/bin/env node

const readline = require("node:readline");
const { createHash } = require("node:crypto");

const PROTOCOL_VERSION = "1";
const SCENARIO = process.env.ALYSIS_TEST_MOCK_SCENARIO || "";
const EXPECTED_HOST_ACTIONS = [
  "tasks.list",
  "tasks.run",
  "tasks.terminate",
  "tasks.status",
  "debug.list",
  "debug.start",
  "debug.stop",
  "debug.status"
];
const REQUIRED_METHODS = [
  "initialize",
  "health",
  "getCapabilities",
  "session.create",
  "session.list",
  "session.cancel",
  "session.setModel",
  "session.modelInfo",
  "chat.send",
  "run.start",
  "job.status",
  "session.getEvents",
  "artifact.list",
  "artifact.read",
  "approval.respond",
  "host.action.respond",
  "profile.presets",
  "profile.preset",
  "profile.list",
  "profile.use",
  "browser.start",
  "browser.navigate",
  "browser.snapshot",
  "browser.screenshot",
  "browser.artifact.read",
  "browser.diagnostics",
  "browser.click",
  "browser.type",
  "browser.status",
  "browser.list",
  "browser.close",
  "forge.plan",
  "forge.plan.start",
  "forge.plan.result",
  "forge.list",
  "forge.open",
  "forge.resume",
  "forge.status",
  "forge.executePreview",
  "forge.execute",
  "forge.cancel",
  "forge.swarm.start",
  "forge.swarm.resume",
  "forge.swarm.list",
  "forge.swarm.status",
  "forge.swarm.result",
  "forge.swarm.cancel",
  "forge.swarm.review",
  "forge.swarm.apply",
  "forge.swarm.discard",
  "diff.list",
  "diff.get",
  "tools.catalog",
  "tool.list",
  "tool.info",
  "skill.list",
  "skill.info",
  "mcp.status",
  "mcp.prompts.list",
  "hooks.list",
  "hooks.effective",
  "conventions.list",
  "conventions.render",
  "ext.list",
  "ext.info",
  "ext.search"
];

const MOCK_BROWSER_ID = "mock-browser-session-0001";
const MOCK_LOOPBACK_BROWSER_ID = "mock-loopback-session-0001";
const MOCK_BROWSER_ARTIFACT_ID = `browser:${MOCK_BROWSER_ID}:screenshot-0001-extension-host.png`;
// A valid 1x1 PNG followed by deterministic trailing bytes. The preview verifier deliberately
// validates the PNG signature, declared length, and SHA-256; the >1 MiB payload also forces the
// Extension Host path to exercise more than one browser.artifact.read chunk.
const MOCK_BROWSER_BASE_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64"
);
const MOCK_BROWSER_PNG = Buffer.concat([
  MOCK_BROWSER_BASE_PNG,
  Buffer.alloc((1024 * 1024) + 17, 0)
]);
const MOCK_BROWSER_PNG_SHA256 = createHash("sha256").update(MOCK_BROWSER_PNG).digest("hex");

const args = process.argv.slice(2);

if (args.length === 1 && args[0] === "--version") {
  if (SCENARIO === "version-failure") {
    process.stderr.write("mock alysis version failure\n");
    process.exit(2);
  }
  process.stdout.write("alysis 0.0.0-extension-host-test\n");
  process.exit(0);
}

if (args.join(" ") === "ide-bridge health") {
  if (SCENARIO === "health-broken") {
    process.stderr.write("mock bridge health failed before protocol startup\n");
    process.exit(3);
  }
  process.stdout.write(JSON.stringify(healthForScenario()) + "\n");
  process.exit(0);
}

if (args.join(" ") === "sandbox doctor --smoke") {
  if (SCENARIO === "doctor-failure") {
    process.stdout.write("doctor saw sk-test-secret-value while checking sandbox\n");
    process.stderr.write("authorization: Bearer abcdefghijklmnop\nmock sandbox doctor failed\n");
    process.exit(2);
  }
  process.stdout.write("sandbox doctor ok\n");
  process.exit(0);
}

if (args.join(" ") === "ide-bridge --stdio") {
  runStdioBridge();
} else {
  process.stderr.write(`unsupported mock alysis invocation: ${args.join(" ")}\n`);
  process.exit(2);
}

function runStdioBridge() {
  const sessions = new Map();
  const jobs = new Map();
  const browsers = new Map();
  const eventsBySession = new Map();
  const planInstructionBySession = new Map();
  let nextSequence = 1;
  let nextChatJobNumber = 1;
  let nextPlanJobNumber = 1;
  let lastExecuteJobId = null;
  const swarmState = {
    jobId: null,
    complete: false,
    cancelled: false,
    resumeCount: 0,
    revision: 0,
    applied: {},
    discarded: {}
  };
  const swarmDurableStatus = () => {
    const state = swarmState.cancelled ? "cancelled" : swarmState.complete ? "succeeded" : "running";
    return {
      job_id: swarmState.jobId,
      state,
      revision: swarmState.revision,
      attempts: swarmState.resumeCount + 1,
      resume_count: swarmState.resumeCount,
      created_at: 0,
      updated_at: 0,
      started_at: 0,
      terminal_at: swarmState.complete ? 0 : null,
      lease_expires_at: null,
      result_available: swarmState.complete,
      error_code: null,
      error_summary: null,
      usage: {
        calls: 0,
        input_tokens: 0,
        output_tokens: 0,
        cached_input_tokens: 0,
        total_tokens: 0
      },
      resumable: state === "interrupted"
    };
  };
  const browserStatus = (entry) => ({
    browser_session_id: entry.browserSessionId,
    product: "Mock Chromium",
    state: "running",
    created_at: 0,
    allow_local_destinations: entry.networkScope !== "public",
    network_scope: entry.networkScope,
    active_url: entry.activeUrl,
    artifact_count: entry.artifactCount
  });
  const ownedBrowser = (params) => {
    const entry = browsers.get(params.browser_session_id);
    return entry && entry.ownerSessionId === params.session_id ? entry : null;
  };
  const swarmReviewPayload = (sessionId) => {
    const stateFor = (taskId, base) =>
      swarmState.applied[taskId] ? "applied" : swarmState.discarded[taskId] ? "discarded" : base;
    const item = (taskId, title, base) => ({
      task_id: taskId,
      title,
      status: base === "failed" ? "failed" : base === "merged" ? "ready_for_merge" : base,
      state: stateFor(taskId, base),
      reviewable: base === "merged" && !swarmState.applied[taskId] && !swarmState.discarded[taskId],
      diff_available: base === "merged",
      diff_artifact_id: base === "merged" ? `forge_run_mock-plan:execution/harvest/${taskId}.diff` : null,
      untracked_files: taskId === "T03" ? ["scratch_note.txt"] : [],
      untracked_files_note: taskId === "T03" ? "untracked files created: scratch_note.txt" : null,
      worktree_present: base === "merged",
      applied: Boolean(swarmState.applied[taskId]),
      discarded: Boolean(swarmState.discarded[taskId]),
      recovery:
        base === "failed"
          ? { kind: "regenerate_subtree", start_method: "forge.plan.regenerate.start", suggested_params: { instruction: "Regenerate the plan subtree for task T02; the previous attempt ended failed.", focus: "src/b.py" } }
          : null
    });
    return {
      session_id: sessionId,
      plan_id: "mock-plan",
      items: [item("T01", "Wire retry/back-off", "merged"), item("T02", "Add cancel events", "failed"), item("T03", "Document swarm", "merged")],
      state_counts: { ready_for_merge: 2, failed: 1 },
      pending_review_task_ids: ["T01", "T03"].filter((id) => !swarmState.applied[id] && !swarmState.discarded[id]),
      working_tree_untouched_until_apply: true
    };
  };
  const emitEvent = (sequence, sessionId, jobId, type, payload) => {
    const event = {
      protocol_version: PROTOCOL_VERSION,
      session_id: sessionId,
      run_id: null,
      job_id: jobId,
      sequence,
      timestamp: new Date(0).toISOString(),
      type,
      payload
    };
    const events = eventsBySession.get(sessionId) || [];
    events.push(event);
    eventsBySession.set(sessionId, events);
    process.stdout.write(JSON.stringify(event) + "\n");
  };
  const rl = readline.createInterface({ input: process.stdin });
  rl.on("line", (line) => {
    if (!line.trim()) {
      return;
    }
    let request;
    try {
      request = JSON.parse(line);
    } catch {
      writeError(null, "invalid_json", "Request was not valid JSON.");
      return;
    }
    const params = request.params || {};
    switch (request.method) {
      case "initialize":
      case "health":
        writeResult(request.id, healthForScenario());
        return;
      case "getCapabilities":
        writeResult(request.id, healthForScenario().capabilities);
        return;
      case "profile.presets":
        writeResult(request.id, {
          presets: [{
            key: "mock-provider",
            label: "Mock Provider",
            base_url: "https://mock-provider.example/v1",
            base_url_host: "mock-provider.example",
            protocol_kind: "native",
            suggested_models: ["mock-profile-model"],
            key_env_var: "MOCK_PROVIDER_API_KEY"
          }],
          secret_values_included: false
        });
        return;
      case "profile.list":
        writeResult(request.id, {
          active_profile: "mock-profile",
          profiles: [{
            name: "mock-profile",
            base_url: "https://mock-provider.example/v1",
            base_url_host: "mock-provider.example",
            default_model: "mock-profile-model",
            protocol_kind: "native",
            api_key: { present: false, source: "fixture" },
            auth_provider: "",
            reasoning_effort: "",
            subscription_selection_ready: true
          }],
          secret_values_included: false
        });
        return;
      case "profile.preset":
        writeResult(request.id, { changed: true, profile: {}, secret_values_included: false });
        return;
      case "profile.use":
        writeResult(request.id, {
          active_profile: String(params.name || "mock-profile"),
          changed: true,
          profile: {},
          secret_values_included: false
        });
        return;
      case "session.create": {
        const sessionId = String(params.workspace || "").includes("-alysis-") ? `mock-worktree-${sessions.size}` : "mock-session";
        const session = mockSession(sessionId, params);
        const advertisedActions = params.host_capabilities?.actions;
        if (
          params.workspace_trusted !== true
          || params.host_capabilities?.protocol_version !== "1"
          || !Array.isArray(advertisedActions)
          || advertisedActions.length !== EXPECTED_HOST_ACTIONS.length
          || EXPECTED_HOST_ACTIONS.some((action, index) => advertisedActions[index] !== action)
        ) {
          writeError(request.id, "mock_host_capabilities_contract", "VS Code did not advertise the exact trusted host action set.");
          return;
        }
        const hostActions = {
          protocol_version: "1",
          actions: [...advertisedActions],
          workspace_fence: "wf_mock-extension-host",
          capability_fingerprint: "a".repeat(64),
          request_event: "host_action_requested",
          cancellation_event: "host_action_cancelled",
          session_closed_event: "session_closed",
          response_method: "host.action.respond",
          request_timeout_seconds: 30,
          max_argument_bytes: 8192,
          max_result_bytes: 65536
        };
        session.host_actions = hostActions;
        sessions.set(sessionId, session);
        writeResult(request.id, {
          session_id: session.session_id,
          workspace_root: session.workspace_root,
          mode: session.mode,
          host_actions: hostActions
        });
        return;
      }
      case "host.action.respond": {
        writeResult(request.id, {
          status: "applied",
          session_id: params.session_id,
          host_action_id: params.host_action_id,
          action: "tasks.list",
          outcome: params.ok ? "result" : "error"
        });
        return;
      }
      case "session.list":
        writeResult(request.id, { sessions: [...sessions.values()].map(sessionForList) });
        return;
      case "session.cancel": {
        const session = sessions.get(params.session_id);
        if (session) {
          session.closed = true;
          session.active_job = null;
        }
        for (const [browserId, browser] of browsers) {
          if (browser.ownerSessionId === params.session_id) {
            browsers.delete(browserId);
          }
        }
        writeResult(request.id, { session_id: params.session_id, status: "closed" });
        return;
      }
      case "session.setModel": {
        const sessionId = params.session_id || "mock-session";
        const session = sessions.get(sessionId) || mockSession(sessionId, params);
        session.model = typeof params.model === "string" && params.model.length > 0 ? params.model : session.model;
        sessions.set(sessionId, session);
        writeResult(request.id, sessionStatus(session));
        return;
      }
      case "session.modelInfo": {
        const sessionId = params.session_id || "mock-session";
        const session = sessions.get(sessionId) || mockSession(sessionId, params);
        sessions.set(sessionId, session);
        writeResult(request.id, sessionModelInfo(session, typeof params.model === "string" ? params.model : undefined));
        return;
      }
      case "chat.send":
      case "run.start": {
        // Mirror the real bridge: run.start on an EXISTING session accepts turn params only.
        // Session options belong to session.create; repeating them here is the contract
        // violation that once broke every first task against the real CLI while this mock
        // happily accepted it.
        if (request.method === "run.start" && typeof params.session_id === "string") {
          const sessionOptions = ["workspace", "mode", "model", "base_url", "temperature", "stream", "verify_cmd", "verify_commands", "subagents_enabled", "no_log", "yes", "max_steps", "active_workdir", "active_workdir_relpath", "workspace_trusted", "host_capabilities"]
            .filter((key) => key in params);
          if (sessionOptions.length > 0) {
            writeError(
              request.id,
              "unsupported_turn_option",
              "run.start with an existing session only accepts turn params; use session.setMode, session.setModel, session.setStream, or session.setActiveWorkdir for live session changes."
            );
            return;
          }
        }
        const sessionId = params.session_id || "mock-session";
        const job = {
          job_id: `mock-job-${nextChatJobNumber++}`,
          session_id: sessionId,
          status: "running",
          created_at: new Date(0).toISOString(),
          started_at: new Date(0).toISOString(),
          completed_at: null,
          exit_code: null,
          error: null
        };
        jobs.set(job.job_id, job);
        const session = sessions.get(sessionId);
        if (session) {
          session.active_job = job;
        }
        writeResult(request.id, { session_id: sessionId, job_id: job.job_id, status: "started" });
        const completeJob = () => {
          if (
            request.method === "run.start"
            && String(params.instruction || "").includes("Exercise the negotiated Extension Host task catalog")
            && session?.host_actions?.actions.includes("tasks.list")
          ) {
            const hostActions = session.host_actions;
            const hostActionId = `ha_${createHash("sha256").update(`extension-host:${sessionId}:${nextSequence}`).digest("hex").slice(0, 32)}`;
            emitEvent(nextSequence++, sessionId, job.job_id, "host_action_requested", {
              host_action_id: hostActionId,
              action: "tasks.list",
              arguments: {},
              workspace_root: session.workspace_root,
              workspace_fence: hostActions.workspace_fence,
              capability_fingerprint: hostActions.capability_fingerprint,
              expires_at: new Date(Date.now() + 30_000).toISOString(),
              protocol_version: "1",
              max_result_bytes: hostActions.max_result_bytes
            });
          }
          job.status = "completed";
          job.completed_at = new Date(0).toISOString();
          job.exit_code = 0;
          emitEvent(nextSequence++, sessionId, job.job_id, "message_end", { text: "mock response" });
        };
        if (String(params.message || params.instruction || "").includes("Fix the attached Extension Host diagnostic")) {
          setTimeout(completeJob, 750);
        } else {
          queueMicrotask(completeJob);
        }
        return;
      }
      case "job.status":
        writeResult(request.id, jobs.get(params.job_id) || {
          job_id: params.job_id || "mock-job",
          session_id: "mock-session",
          status: "completed",
          exit_code: 0,
          error: null
        });
        return;
      case "session.getEvents":
        const storedEvents = eventsBySession.get(params.session_id) || [];
        const afterSequence = typeof params.after_sequence === "number" ? params.after_sequence : 0;
        const maxEvents = Number(params.max_events || 500);
        const events = storedEvents.filter((event) => event.sequence > afterSequence).slice(0, maxEvents);
        writeResult(request.id, {
          session_id: params.session_id,
          events,
          truncated: false,
          lowest_retained_sequence: storedEvents.length > 0 ? storedEvents[0].sequence : null,
          highest_retained_sequence: storedEvents.length > 0 ? storedEvents[storedEvents.length - 1].sequence : null,
          max_events: maxEvents
        });
        return;
      case "artifact.list":
        const artifacts = [
          {
            artifact_id: "forge_mock:plan/plan.json",
            root: "forge_run",
            path: "plan/plan.json",
            size_bytes: 42
          }
        ];
        if (lastExecuteJobId) {
          artifacts.push({
            artifact_id: "forge_mock:execute/result.json",
            root: "forge_run",
            path: "execute/result.json",
            size_bytes: 64
          });
        }
        writeResult(request.id, {
          session_id: params.session_id,
          artifacts,
          truncated: false,
          max_items: 500,
          max_depth: 8
        });
        return;
      case "artifact.read":
        writeResult(request.id, {
          session_id: params.session_id,
          artifact_id: params.artifact_id,
          path: String(params.artifact_id || "forge_mock:plan/plan.json"),
          size_bytes: 42,
          truncated: false,
          max_bytes: 65536,
          encoding: "utf-8",
          content: JSON.stringify(
            String(params.artifact_id || "").includes("execute")
              ? { plan_id: "mock-plan", job_id: lastExecuteJobId, status: "completed", fixture: "extension-host" }
              : { plan_id: "mock-plan", fixture: "extension-host" },
            null,
            2
          )
        });
        return;
      case "approval.respond":
        writeResult(request.id, {
          session_id: params.session_id,
          approval_id: params.approval_id,
          status: "resolved",
          allow: params.allow === true,
          allow_for_session: false,
          allow_for_session_supported: false,
          allow_for_session_scope: null,
          allow_for_session_warning: null
        });
        queueMicrotask(() => {
          emitEvent(nextSequence++, params.session_id || "mock-session", null, "prompt_for_input", {
            kind: "approval_result",
            approval_id: params.approval_id,
            allow: params.allow === true,
            allow_for_session: params.allow_for_session === true
          });
        });
        return;
      case "browser.start": {
        const networkScope = String(params.network_scope || "");
        if (
          params.workspace_trusted !== true
          || !Object.prototype.hasOwnProperty.call(params, "network_scope")
          || Object.prototype.hasOwnProperty.call(params, "allow_local_destinations")
          || Object.prototype.hasOwnProperty.call(params, "executable_path")
          || !["public", "public_loopback"].includes(networkScope)
          || (networkScope === "public_loopback" && params.confirm !== true)
        ) {
          writeError(request.id, "mock_browser_contract", "Managed Browser must use an explicit trusted public or confirmed public-loopback scope without legacy local flags.");
          return;
        }
        const ownerSessionId = String(params.session_id || "");
        if (!sessions.has(ownerSessionId)) {
          writeError(request.id, "session_not_found", "Managed Browser owner session is unavailable.");
          return;
        }
        const entry = {
          browserSessionId: networkScope === "public_loopback" ? MOCK_LOOPBACK_BROWSER_ID : MOCK_BROWSER_ID,
          ownerSessionId,
          networkScope,
          activeUrl: null,
          artifactCount: 0
        };
        browsers.set(entry.browserSessionId, entry);
        writeResult(request.id, { session_id: ownerSessionId, ...browserStatus(entry) });
        return;
      }
      case "browser.navigate": {
        const entry = ownedBrowser(params);
        if (!entry || params.workspace_trusted !== true) {
          writeError(request.id, "browser_not_found", "Managed Browser navigation scope is invalid.");
          return;
        }
        const target = String(params.url || "https://example.test/");
        if (!mockBrowserDestinationAllowed(target, entry.networkScope)) {
          writeError(request.id, "browser_destination_denied", "Managed Browser destination is outside the selected network scope.");
          return;
        }
        entry.activeUrl = target;
        const data = { frame_id: "mock-main-frame", loader_id: "mock-loader" };
        writeResult(request.id, {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          url: entry.activeUrl,
          result: {
            data,
            truncated: false,
            size_bytes: Buffer.byteLength(JSON.stringify(data), "utf8")
          }
        });
        return;
      }
      case "browser.snapshot": {
        const entry = ownedBrowser(params);
        if (!entry) {
          writeError(request.id, "browser_not_found", "Managed Browser snapshot scope is invalid.");
          return;
        }
        const kind = params.kind || "text";
        const base = {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          kind,
          truncated: false
        };
        if (kind === "text") {
          const text = "Mock public page\nManaged Browser extension-host fixture";
          writeResult(request.id, { ...base, text, size_bytes: Buffer.byteLength(text, "utf8") });
          return;
        }
        const data = { role: "document", name: "Mock public page", children: [] };
        writeResult(request.id, {
          ...base,
          data,
          size_bytes: Buffer.byteLength(JSON.stringify(data), "utf8")
        });
        return;
      }
      case "browser.screenshot": {
        const entry = ownedBrowser(params);
        if (!entry) {
          writeError(request.id, "browser_not_found", "Managed Browser screenshot scope is invalid.");
          return;
        }
        entry.artifactCount += 1;
        writeResult(request.id, {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          artifact_id: MOCK_BROWSER_ARTIFACT_ID,
          media_type: "image/png",
          size_bytes: MOCK_BROWSER_PNG.length,
          sha256: MOCK_BROWSER_PNG_SHA256
        });
        return;
      }
      case "browser.artifact.read": {
        const entry = ownedBrowser(params);
        if (!entry || params.artifact_id !== MOCK_BROWSER_ARTIFACT_ID) {
          writeError(request.id, "artifact_not_found", "Managed Browser screenshot artifact is unavailable.");
          return;
        }
        const offset = Number.isSafeInteger(params.offset) && params.offset >= 0 ? params.offset : 0;
        const requested = Number.isSafeInteger(params.max_bytes) && params.max_bytes > 0
          ? params.max_bytes
          : 256 * 1024;
        const end = Math.min(MOCK_BROWSER_PNG.length, offset + requested);
        const chunk = MOCK_BROWSER_PNG.subarray(offset, end);
        writeResult(request.id, {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          artifact_id: MOCK_BROWSER_ARTIFACT_ID,
          media_type: "image/png",
          encoding: "base64",
          content: chunk.toString("base64"),
          offset,
          next_offset: end,
          size_bytes: MOCK_BROWSER_PNG.length,
          truncated: end < MOCK_BROWSER_PNG.length
        });
        return;
      }
      case "browser.diagnostics": {
        const entry = ownedBrowser(params);
        if (!entry) {
          writeError(request.id, "browser_not_found", "Managed Browser diagnostics scope is invalid.");
          return;
        }
        const allEvents = [
          {
            category: "console",
            method: "Runtime.consoleAPICalled",
            params: { data: { type: "log", text: "mock console event" }, truncated: false, size_bytes: 42 }
          },
          {
            category: "network",
            method: "Network.responseReceived",
            params: { data: { status: 200, host: "example.test" }, truncated: false, size_bytes: 36 }
          }
        ];
        const maxEvents = Number.isSafeInteger(params.max_events) && params.max_events >= 0
          ? params.max_events
          : 100;
        const events = allEvents.slice(0, maxEvents);
        writeResult(request.id, {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          events,
          truncated: events.length < allEvents.length,
          max_events: maxEvents
        });
        return;
      }
      case "browser.click": {
        const entry = ownedBrowser(params);
        if (!entry || params.workspace_trusted !== true) {
          writeError(request.id, "browser_not_found", "Managed Browser click scope is invalid.");
          return;
        }
        writeResult(request.id, {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          clicked: true
        });
        return;
      }
      case "browser.type": {
        const entry = ownedBrowser(params);
        if (!entry || params.workspace_trusted !== true) {
          writeError(request.id, "browser_not_found", "Managed Browser type scope is invalid.");
          return;
        }
        writeResult(request.id, {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          typed: true,
          character_count: String(params.text || "").length
        });
        return;
      }
      case "browser.status": {
        const entry = ownedBrowser(params);
        if (!entry) {
          writeError(request.id, "browser_not_found", "Managed Browser status scope is invalid.");
          return;
        }
        writeResult(request.id, { session_id: entry.ownerSessionId, ...browserStatus(entry) });
        return;
      }
      case "browser.list": {
        const ownerSessionId = String(params.session_id || "");
        const owned = [...browsers.values()].filter((entry) => entry.ownerSessionId === ownerSessionId);
        writeResult(request.id, {
          session_id: ownerSessionId,
          browsers: owned.map(browserStatus),
          count: owned.length
        });
        return;
      }
      case "browser.close": {
        const entry = ownedBrowser(params);
        if (params.delete_artifacts !== true || params.confirm !== true) {
          writeError(request.id, "confirmation_required", "Managed Browser cleanup requires explicit confirmation and artifact deletion.");
          return;
        }
        if (entry) {
          browsers.delete(entry.browserSessionId);
        }
        writeResult(request.id, {
          session_id: String(params.session_id || ""),
          browser_session_id: String(params.browser_session_id || ""),
          status: entry ? "closed" : "not_found"
        });
        return;
      }
      case "forge.plan.start": {
        const sessionId = params.session_id || "mock-session";
        const instruction = String(params.instruction || "");
        if (instruction.toLowerCase().includes("trigger bridge exit")) {
          process.nextTick(() => process.exit(7));
          return;
        }
        const failed = instruction.toLowerCase().includes("fail plan");
        const planJobNumber = nextPlanJobNumber++;
        const job = {
          job_id: failed ? `mock-plan-job-failed-${planJobNumber}` : `mock-plan-job-${planJobNumber}`,
          session_id: sessionId,
          status: failed ? "failed" : "completed",
          created_at: new Date(0).toISOString(),
          started_at: new Date(0).toISOString(),
          completed_at: new Date(0).toISOString(),
          exit_code: failed ? 1 : 0,
          error: failed ? "Mock Forge Plan failed for deterministic Cockpit coverage." : null,
          plan_result: failed ? null : forgePlanResult(sessionId, instruction || "Mock Forge plan")
        };
        if (!sessions.has(sessionId)) {
          sessions.set(sessionId, mockSession(sessionId, params));
        }
        jobs.set(job.job_id, job);
        const session = sessions.get(sessionId);
        if (session) {
          session.active_job = job;
        }
        planInstructionBySession.set(sessionId, instruction);
        writeResult(request.id, { session_id: sessionId, job_id: job.job_id, status: "started" });
        if (!failed) {
          queueMicrotask(() => {
            emitEvent(nextSequence++, sessionId, job.job_id, "plan_node_updated", {
              node_id: "T01",
              state: "planned",
              summary: "Mock Forge task"
            });
          });
        }
        return;
      }
      case "forge.plan.result": {
        const job = jobs.get(params.job_id);
        if (!job || !job.plan_result) {
          writeError(request.id, "mock_plan_result_missing", "Mock Forge Plan result is unavailable.");
          return;
        }
        writeResult(request.id, job.plan_result);
        return;
      }
      case "forge.plan": {
        const sessionId = params.session_id || "mock-session";
        const instruction = String(params.instruction || "");
        if (instruction.toLowerCase().includes("fail plan")) {
          writeError(request.id, "mock_plan_failure", "Mock Forge Plan failed for deterministic Cockpit coverage.");
          return;
        }
        if (instruction.toLowerCase().includes("trigger bridge exit")) {
          process.nextTick(() => process.exit(7));
          return;
        }
        if (!sessions.has(sessionId)) {
          sessions.set(sessionId, mockSession(sessionId, params));
        }
        const plan = forgePlanResult(sessionId, instruction || "Mock Forge plan");
        planInstructionBySession.set(sessionId, instruction);
        writeResult(request.id, plan);
        queueMicrotask(() => {
          emitEvent(nextSequence++, sessionId, null, "plan_node_updated", {
            node_id: "T01",
            state: "planned",
            summary: "Mock Forge task"
          });
        });
        return;
      }
      case "forge.status":
        writeResult(request.id, forgePlanResult(params.session_id || "mock-session", "Mock Forge plan"));
        return;
      case "forge.list":
        writeResult(request.id, {
          workspace_root: typeof params.workspace === "string" ? params.workspace : "",
          plans: [
            {
              plan_id: "mock-plan",
              session_id: null,
              workspace_root: typeof params.workspace === "string" ? params.workspace : "",
              status: "planned",
              source: "persisted",
              project_goal: "Mock Forge plan",
              summary: "Mock Forge plan summary",
              task_count: 1,
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
              plan_artifact_id: "forge_mock:plan/plan.json",
              plan_markdown_artifact_id: "forge_mock:plan/PLAN.md"
            }
          ],
          truncated: false,
          max_items: Number(params.max_items || 50)
        });
        return;
      case "forge.open":
      case "forge.resume": {
        const sessionId = params.session_id || "mock-session-opened";
        if (!sessions.has(sessionId)) {
          sessions.set(sessionId, mockSession(sessionId, { ...params, mode: "readonly" }));
        }
        writeResult(request.id, { ...forgePlanResult(sessionId, "Mock Forge plan"), source: "loaded_persisted", created_session: !params.session_id });
        return;
      }
      case "forge.executePreview":
        writeResult(request.id, forgeExecutePreviewResult(params, planInstructionBySession.get(params.session_id || "mock-session")));
        if (shouldEmitApprovalForPreview(planInstructionBySession.get(params.session_id || "mock-session"))) {
          queueMicrotask(() => {
            emitEvent(nextSequence++, params.session_id || "mock-session", "mock-preview-job", "prompt_for_input", {
              kind: "approval",
              approval_kind: "verify_run",
              approval_id: "mock-approval",
              prompt_id: "mock-approval",
              reason: "Mock verification command set requires approval.",
              preview: "npm test",
              command: "npm test",
              files: ["README.md"],
              allow_for_session_supported: true,
              allow_for_session_scope: { type: "exact_verify_command_set", command_count: 1 }
            });
          });
        }
        return;
      case "forge.execute":
        if (params.dry_run === true) {
          writeResult(request.id, forgeExecutePreviewResult(params, planInstructionBySession.get(params.session_id || "mock-session")));
          return;
        }
        if (shouldSupportRealExecute(planInstructionBySession.get(params.session_id || "mock-session"))) {
          const sessionId = params.session_id || "mock-session";
          const planId = params.plan_id || "mock-plan";
          const job = {
            job_id: "mock-execute-job",
            session_id: sessionId,
            kind: "forge_execute",
            plan_id: planId,
            status: "running",
            created_at: new Date(0).toISOString(),
            started_at: new Date(0).toISOString(),
            completed_at: null,
            exit_code: null,
            error: null
          };
          jobs.set(job.job_id, job);
          lastExecuteJobId = job.job_id;
          const session = sessions.get(sessionId);
          if (session) {
            session.active_job = job;
          }
          writeResult(request.id, {
            session_id: sessionId,
            plan_id: planId,
            job_id: job.job_id,
            status: "started"
          });
          queueMicrotask(() => {
            emitEvent(nextSequence++, sessionId, job.job_id, "verify_gate_result", {
              task_id: "T01",
              command: "npm test",
              success: true,
              summary: "mock verification passed"
            });
            job.status = "completed";
            job.completed_at = new Date(0).toISOString();
            job.exit_code = 0;
            if (session) {
              session.active_job = { ...job };
            }
            emitEvent(nextSequence++, sessionId, job.job_id, "review_gate_decision", {
              task_id: "T01",
              decision: "accepted",
              summary: "mock review accepted"
            });
            emitEvent(nextSequence++, sessionId, job.job_id, "status_update", {
              mode: "review",
              model: "mock execute completed"
            });
          });
          return;
        }
        writeError(request.id, "forge_execute_unsupported", "forge.execute unsupported");
        return;
      case "forge.cancel":
        writeResult(request.id, {
          session_id: params.session_id || "mock-session",
          plan_id: params.plan_id || "mock-plan",
          status: "no_active_job",
          state: "idle",
          job: null
        });
        return;
      // ---- SW6: deterministic swarm scenario --------------------------------
      // Three tasks: T01 + T02 are disjoint and run in parallel; T03 overlaps
      // and serializes. T01 raises a non-modal approval, T02 fails closed on a
      // scope violation, T03 completes clean and becomes reviewable.
      case "forge.swarm.start": {
        const sessionId = params.session_id || "mock-session";
        swarmState.jobId = "mock-swarm-job";
        swarmState.complete = false;
        swarmState.cancelled = false;
        swarmState.resumeCount = 0;
        swarmState.revision += 1;
        swarmState.applied = {};
        swarmState.discarded = {};
        if (!sessions.has(sessionId)) {
          sessions.set(sessionId, mockSession(sessionId, params));
        }
        writeResult(request.id, {
          session_id: sessionId,
          plan_id: params.plan_id || "mock-plan",
          job_id: swarmState.jobId,
          status: "started",
          parallel: params.parallel || 2
        });
        // Emit the parallel task lifecycle deterministically.
        queueMicrotask(() => {
          emitEvent(nextSequence++, sessionId, swarmState.jobId, "swarm_worker_state_changed", { worker_id: "T01", state: "scheduled", role: "forge_swarm" });
          emitEvent(nextSequence++, sessionId, swarmState.jobId, "swarm_worker_state_changed", { worker_id: "T02", state: "scheduled", role: "forge_swarm" });
          emitEvent(nextSequence++, sessionId, swarmState.jobId, "swarm_worker_state_changed", { worker_id: "T01", state: "started", role: "forge_swarm" });
          emitEvent(nextSequence++, sessionId, swarmState.jobId, "swarm_worker_state_changed", { worker_id: "T02", state: "started", role: "forge_swarm" });
          // T01 pauses on a non-modal task-attributed approval.
          emitEvent(nextSequence++, sessionId, swarmState.jobId, "swarm_worker_state_changed", { worker_id: "T01", state: "approval_pending", role: "forge_swarm" });
          emitEvent(nextSequence++, sessionId, swarmState.jobId, "prompt_for_input", {
            prompt_id: "mock-swarm-approval",
            approval_id: "mock-swarm-approval",
            kind: "approval",
            approval_kind: "shell_run",
            prompt_text: "npm run release:publish",
            command: "npm run release:publish",
            reason: "sensitive command requires approval",
            files: [],
            allow_for_session_supported: false,
            metadata: { forge_swarm_task_id: "T01", worker: "forge_swarm:T01" }
          });
          // T02 fails closed on a scope violation (one error card).
          emitEvent(nextSequence++, sessionId, swarmState.jobId, "swarm_worker_state_changed", { worker_id: "T02", state: "failed", role: "forge_swarm" });
          // T03 serializes after, runs, and becomes reviewable.
          emitEvent(nextSequence++, sessionId, swarmState.jobId, "swarm_worker_state_changed", { worker_id: "T03", state: "started", role: "forge_swarm" });
          emitEvent(nextSequence++, sessionId, swarmState.jobId, "swarm_worker_state_changed", { worker_id: "T03", state: "merged", role: "forge_swarm" });
          swarmState.complete = true;
        });
        return;
      }
      case "forge.swarm.resume": {
        const sessionId = params.session_id || "mock-session";
        swarmState.jobId = params.job_id || swarmState.jobId || "mock-swarm-job";
        swarmState.complete = false;
        swarmState.cancelled = false;
        swarmState.resumeCount += 1;
        swarmState.revision += 1;
        writeResult(request.id, {
          session_id: sessionId,
          plan_id: params.plan_id || "mock-plan",
          status: "resumed",
          ...swarmDurableStatus()
        });
        return;
      }
      case "forge.swarm.list": {
        const jobs = swarmState.jobId === null ? [] : [swarmDurableStatus()];
        writeResult(request.id, {
          session_id: params.session_id || "mock-session",
          jobs,
          count: jobs.length
        });
        return;
      }
      case "forge.swarm.status":
        writeResult(request.id, {
          job_id: params.job_id || swarmState.jobId,
          session_id: params.session_id || "mock-session",
          status: swarmState.complete ? "completed" : "running",
          state: swarmState.complete ? "completed" : "running",
          plan_id: "mock-plan",
          cancellation_requested: swarmState.cancelled,
          task_status_counts: { ready_for_merge: 1, failed: 1 }
        });
        return;
      case "forge.swarm.result": {
        if (!swarmState.complete && !swarmState.cancelled) {
          writeResult(request.id, {
            session_id: params.session_id || "mock-session",
            job_id: params.job_id || swarmState.jobId,
            plan_id: "mock-plan",
            status: "running",
            state: "running",
            complete: false
          });
          return;
        }
        writeResult(request.id, {
          job_id: params.job_id || swarmState.jobId,
          status: swarmState.cancelled ? "cancelled" : "completed",
          state: swarmState.cancelled ? "cancelled" : "completed",
          complete: true,
          exit_code: swarmState.cancelled ? 130 : 1,
          run_status: swarmState.cancelled ? "interrupted" : "incomplete",
          clean: false,
          interrupted: swarmState.cancelled,
          interrupted_task_ids: swarmState.cancelled ? ["T01", "T03"] : [],
          task_status_counts: { ready_for_merge: 1, failed: 1 }
        });
        return;
      }
      case "forge.swarm.cancel":
        swarmState.cancelled = true;
        swarmState.complete = true;
        writeResult(request.id, {
          session_id: params.session_id || "mock-session",
          plan_id: "mock-plan",
          job_id: params.job_id || swarmState.jobId,
          status: "cancellation_requested",
          state: "cancellation_requested"
        });
        return;
      case "forge.swarm.review":
        writeResult(request.id, swarmReviewPayload(params.session_id || "mock-session"));
        return;
      case "forge.swarm.apply": {
        const taskIds = Array.isArray(params.task_ids) ? params.task_ids : [];
        for (const id of taskIds) {
          swarmState.applied[String(id)] = true;
        }
        writeResult(request.id, {
          session_id: params.session_id || "mock-session",
          plan_id: "mock-plan",
          applied: taskIds.map((id) => ({ task_id: String(id), applied: true, untracked_files_not_applied: ["scratch_note.txt"] })),
          working_tree_committed: false
        });
        return;
      }
      case "forge.swarm.discard": {
        const taskIds = Array.isArray(params.task_ids) ? params.task_ids : [];
        for (const id of taskIds) {
          swarmState.discarded[String(id)] = true;
        }
        writeResult(request.id, {
          session_id: params.session_id || "mock-session",
          plan_id: "mock-plan",
          discarded: taskIds.map((id) => ({ task_id: String(id), discarded: true }))
        });
        return;
      }
      case "diff.list":
        writeResult(request.id, {
          diffs: [
            {
              diff_id: "mock-diff",
              session_id: params.session_id || "mock-session",
              plan_id: params.plan_id || "mock-plan",
              job_id: lastExecuteJobId,
              file_path: "README.md",
              status: "available",
              old_label: "Alysis Code original",
              new_label: "Alysis Code proposed",
              size_bytes: 12
            }
          ]
        });
        return;
      case "diff.get":
        if (!params.session_id || !params.plan_id) {
          writeError(request.id, "missing_field", "session_id and plan_id are required.");
          return;
        }
        writeResult(request.id, {
          diff_id: params.diff_id || "mock-diff",
          session_id: params.session_id,
          plan_id: params.plan_id,
          job_id: null,
          file_path: "README.md",
          old_text: "old\n",
          new_text: "new\n",
          old_artifact_id: null,
          new_artifact_id: null,
          unified_diff: "--- a/README.md\n+++ b/README.md\n",
          truncated: false,
          size_bytes: 12,
          max_bytes: 65536,
          redaction: "protocol_preview"
        });
        return;
      default:
        writeError(request.id, "method_not_found", `Unknown method: ${request.method}`);
    }
  });
}

function healthForScenario() {
  if (SCENARIO === "health-missing-methods") {
    const broken = health(REQUIRED_METHODS.filter((method) => method !== "artifact.read"));
    broken.capabilities.features.mock_scenario = SCENARIO;
    return broken;
  }
  const current = health(REQUIRED_METHODS);
  current.capabilities.features.mock_scenario = SCENARIO || "default";
  if (SCENARIO === "execute-supported") {
    current.capabilities.features.forge.execute.supported = true;
  }
  return current;
}

function mockBrowserDestinationAllowed(value, networkScope) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) {
    return false;
  }
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  const loopback = host === "localhost"
    || host.endsWith(".localhost")
    || host === "::1"
    || /^127(?:\.\d{1,3}){3}$/.test(host);
  const ipv4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  const octets = ipv4 ? ipv4.slice(1).map(Number) : [];
  const privateOrLinkLocal = host.endsWith(".local")
    || host === "0.0.0.0"
    || host === "::"
    || host.startsWith("fe8")
    || host.startsWith("fe9")
    || host.startsWith("fea")
    || host.startsWith("feb")
    || host.startsWith("fc")
    || host.startsWith("fd")
    || (octets.length === 4 && (
      octets[0] === 10
      || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31)
      || (octets[0] === 192 && octets[1] === 168)
      || (octets[0] === 169 && octets[1] === 254)
    ));
  if (loopback) {
    return networkScope === "public_loopback";
  }
  return !privateOrLinkLocal;
}

function health(methods) {
  return {
    ok: true,
    name: "alysis-ide-bridge",
    alysis_version: "0.0.0-extension-host-test",
    protocol_version: PROTOCOL_VERSION,
    capabilities: {
      protocol_version: PROTOCOL_VERSION,
      methods,
      events: ["message_delta", "message_end", "prompt_for_input", "host_action_requested", "host_action_cancelled", "session_closed"],
      modes: ["readonly", "review", "auto"],
      transport: "stdio-jsonl",
      features: {
        structured_surface_events: true,
        terminal_output_scraping: false,
        secret_redaction: true,
        host_actions: {
          protocol_version: "1",
          events: ["host_action_requested", "host_action_cancelled", "session_closed"]
        },
        managed_browser: {
          supported: true,
          owned_chromium_processes: true,
          loopback_cdp_only: true,
          private_profiles_outside_workspaces: true,
          guarded_navigation: true,
          redirect_and_subresource_interception: true,
          persistent_child_target_interception: true,
          validating_egress_proxy: true,
          dns_resolution_pinned_to_numeric_connect: true,
          no_direct_network_fallback: true,
          loopback_proxy_bypass_removed: true,
          non_proxied_udp_disabled: true,
          public_destinations_by_default: true,
          local_destinations_require_confirmation: true,
          direct_loopback_scope: true,
          direct_loopback_scope_agent_isolated: true,
          direct_loopback_scope_denies_lan_and_link_local: true,
          bounded_snapshots: true,
          chunked_screenshot_artifacts: true,
          bounded_diagnostics: true,
          session_cleanup: true
        },
        forge: {
          plan: { supported: true, schema: "ide_forge_plan_v1", terminal_output_scraping: false },
          list: { supported: true, durable: true },
          open: { supported: true, durable: true },
          resume: { supported: true, alias: "forge.open" },
          status: { supported: true },
          execute_preview: { supported: true, method: "forge.executePreview", mutates: false },
          execute: { supported: false, behavior: "fail_closed", dry_run_supported: true },
          cancel: {
            supported: true,
            callable: true,
            behavior: "cooperative_checkpoint_cancellation",
            interrupt_kind: "cooperative",
            hard_interrupt: false
          },
          swarm: {
            supported: true,
            workspace_trust_required: true,
            cancellation: "cooperative_checkpoint_cancellation",
            merge_behavior: "review_only_per_task_apply_discard",
            approvals: { yes_auto_approval: false }
          }
        },
        resumable_swarm: {
          supported: true,
          durable: true,
          explicit_resume: true,
          fresh_permission_fingerprint_required: true,
          fenced_worker_leases: true,
          restart_recovery: true,
          atomic_cancellation: true,
          exactly_once_usage_events: true
        },
        diffs: {
          supported: true,
          opaque_ids: true,
          arbitrary_file_reads: false
        },
        approvals: {
          host_managed: true,
          allow_for_session: "scoped",
          allow_for_session_scope_required: true
        }
      }
    }
  };
}

function forgePlanResult(sessionId, instruction) {
  return {
    plan_id: "mock-plan",
    session_id: sessionId,
    job_id: null,
    status: "planned",
    source: "active_memory",
    created_session: true,
    project_goal: String(instruction),
    summary: "Mock Forge plan summary",
    warnings: [],
    incomplete: false,
    tasks: [
      {
        task_id: "T01",
        title: "Mock Forge task",
        objective: String(instruction),
        file_scope: {
          estimated_files: ["README.md"],
          write_scope: []
        },
        acceptance_criteria: ["Plan is inspectable."],
        verification_commands: ["npm test"],
        risk_notes: [],
        dependencies: [],
        order: 1,
        scope_unknown_reason: "",
        warnings: [],
        status: "planned"
      }
    ],
    artifacts: [
      {
        kind: "forge_plan_json",
        artifact_id: "forge_mock:plan/plan.json",
        path: "plan/plan.json"
      }
    ],
    plan_artifact_id: "forge_mock:plan/plan.json",
    plan_markdown_artifact_id: "forge_mock:plan/PLAN.md"
  };
}

function forgeExecutePreviewResult(params, instruction) {
  const mode = params.mode === "auto" || params.mode === "review" || params.mode === "readonly"
    ? params.mode
    : "review";
  const realExecutionSupported = shouldSupportRealExecute(instruction);
  return {
    session_id: params.session_id || "mock-session",
    plan_id: params.plan_id || "mock-plan",
    selected_task_ids: Array.isArray(params.task_ids) && params.task_ids.length > 0 ? params.task_ids : ["T01"],
    execution_mode_requested: mode,
    workspace_trust_required: mode !== "readonly",
    workspace_trusted: typeof params.workspace_trusted === "boolean" ? params.workspace_trusted : null,
    estimated_file_scopes: [
      {
        task_id: "T01",
        title: "Mock Forge task",
        estimated_files: ["README.md"],
        write_scope: [],
        scope_unknown_reason: ""
      }
    ],
    verification_commands: [
      {
        task_id: "T01",
        commands: ["npm test"],
        source: "task",
        missing_reason: ""
      }
    ],
    required_approvals: mode === "readonly" ? [] : [
      {
        kind: "verify_run",
        task_id: "T01",
        reason: "Mock verification command set would require approval.",
        scope: { type: "exact_verify_command_set", command_count: 1 },
        allow_for_session_scope: { type: "exact_verify_command_set", command_count: 1 },
        allow_for_session_supported: true
      }
    ],
    runtime_approval_requirements: [],
    approval_scopes_safe: true,
    sandbox_profile: {
      requested: params.sandbox_profile || "default",
      supported: true,
      available: true,
      diagnostic: null
    },
    known_risks: ["Forge runtime cancellation uses cooperative checkpoints; hard interrupt is unavailable."],
    missing_prerequisites: [],
    preview_ready: true,
    real_execution_supported: realExecutionSupported,
    unsupported_reason: realExecutionSupported ? "" : "Forge runtime execution is not wired in the mock bridge.",
    active_cancellation_supported: true,
    cancellation: { supported: true, kind: "cooperative_checkpoint", hard_interrupt: false },
    next_recommended_action: realExecutionSupported ? "Confirm explicit review-mode execution." : "Review the preview.",
    status: "preview"
  };
}

function writeResult(id, result) {
  process.stdout.write(JSON.stringify({ protocol_version: PROTOCOL_VERSION, id, ok: true, result }) + "\n");
}

function writeError(id, code, message) {
  process.stdout.write(JSON.stringify({ protocol_version: PROTOCOL_VERSION, id, ok: false, error: { code, message } }) + "\n");
}

function shouldEmitApprovalForPreview(instruction) {
  return typeof instruction === "string" && instruction.toLowerCase().includes("approval cockpit");
}

function shouldSupportRealExecute(instruction) {
  return (
    SCENARIO === "execute-supported" ||
    (typeof instruction === "string" && instruction.toLowerCase().includes("execute review e2e"))
  );
}

function mockSession(sessionId, params) {
  return {
    session_id: sessionId,
    workspace_root: typeof params.workspace === "string" ? params.workspace : "",
    mode: params.mode === "auto" || params.mode === "review" ? params.mode : "readonly",
    model: typeof params.model === "string" && params.model.length > 0 ? params.model : "mock-profile-model",
    provider: "mock-provider",
    profile: "mock-profile",
    closed: false,
    active_job: null
  };
}

function sessionModelInfo(session, requestedModel) {
  return {
    session_id: session.session_id,
    model: requestedModel && requestedModel.length > 0 ? requestedModel : session.model,
    provider: session.provider,
    profile: session.profile,
    base_url: "https://mock-provider.example/v1",
    base_url_redacted: false,
    context_window: 128000,
    vision_support: false,
    tool_support: true,
    streaming_support: true,
    source: "profile",
    source_metadata: { fixture: "extension-host" },
    secret_values_included: false
  };
}

function sessionStatus(session) {
  return {
    session_id: session.session_id,
    workspace_root: session.workspace_root,
    mode: session.mode,
    closed: session.closed,
    active_job: session.active_job ? { ...session.active_job } : null,
    model: session.model,
    base_url: "https://mock-provider.example/v1",
    stream: true,
    max_steps: 20,
    no_log: false,
    yes: false,
    subagents_enabled: false,
    active_workdir: null
  };
}

function sessionForList(session) {
  return {
    ...session,
    active_job: session.active_job ? { ...session.active_job } : null
  };
}

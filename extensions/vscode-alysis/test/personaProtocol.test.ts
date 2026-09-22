import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_PERSONA_LIST_ENTRIES,
  ProtocolPayloadError,
  isValidPersonaName,
  parsePersonaChangedPayload,
  parseSessionPersonaSetResult,
  parseSessionPersonasListResult
} from "../src/client/AlysisProtocol";

test("parseSessionPersonasListResult accepts the wire contract shape", () => {
  const result = parseSessionPersonasListResult({
    enabled: true,
    active: "code",
    active_source: "user",
    personas: [
      {
        name: "code",
        description: "Full implementation persona.",
        default_exec_mode: "",
        model_role: "coding",
        source_scope: "builtin",
        allow_write_globs: []
      },
      {
        name: "architect",
        description: "Planning persona; writes markdown only.",
        default_exec_mode: "review",
        model_role: "planner",
        source_scope: "builtin",
        allow_write_globs: ["**/*.md"]
      }
    ]
  });

  assert.equal(result.enabled, true);
  assert.equal(result.active, "code");
  assert.equal(result.active_source, "user");
  assert.equal(result.personas.length, 2);
  assert.equal(result.personas[1].name, "architect");
  assert.equal(result.personas[1].default_exec_mode, "review");
  assert.deepEqual(result.personas[1].allow_write_globs, ["**/*.md"]);
});

test("parseSessionPersonasListResult treats enabled:false as a success with no personas", () => {
  const result = parseSessionPersonasListResult({
    enabled: false,
    active: "",
    active_source: "",
    personas: []
  });
  assert.equal(result.enabled, false);
  assert.deepEqual(result.personas, []);
  // A kill-switched CLI that still echoes entries must not smuggle them into the surface.
  const echoed = parseSessionPersonasListResult({ enabled: false, personas: [{ name: "code" }] });
  assert.deepEqual(echoed.personas, []);
});

test("parseSessionPersonasListResult fills contract-optional fields and bounds descriptions", () => {
  const result = parseSessionPersonasListResult({
    enabled: true,
    active: "code",
    active_source: "user",
    personas: [{ name: "code", description: "x".repeat(1000) }]
  });
  const persona = result.personas[0];
  assert.equal(persona.description.length, 280);
  assert.equal(persona.default_exec_mode, "");
  assert.equal(persona.model_role, "");
  assert.equal(persona.source_scope, "");
  assert.deepEqual(persona.allow_write_globs, []);
});

test("parseSessionPersonasListResult rejects unknown shapes", () => {
  assert.throws(() => parseSessionPersonasListResult(null), ProtocolPayloadError);
  assert.throws(() => parseSessionPersonasListResult([]), ProtocolPayloadError);
  assert.throws(() => parseSessionPersonasListResult({ active: "code" }), /enabled/);
  assert.throws(() => parseSessionPersonasListResult({ enabled: "yes" }), /enabled/);
  assert.throws(() => parseSessionPersonasListResult({ enabled: true, personas: "code" }), /array/);
  assert.throws(
    () => parseSessionPersonasListResult({ enabled: true, active: "../evil", personas: [] }),
    /active/
  );
  assert.throws(
    () => parseSessionPersonasListResult({ enabled: true, active_source: "x".repeat(33), personas: [] }),
    /active_source/
  );
  assert.throws(
    () => parseSessionPersonasListResult({ enabled: true, personas: [{ name: "has space" }] }),
    /name/
  );
  assert.throws(
    () => parseSessionPersonasListResult({ enabled: true, personas: [{ name: "code", description: 7 }] }),
    /description/
  );
  assert.throws(
    () => parseSessionPersonasListResult({ enabled: true, personas: [{ name: "code", default_exec_mode: "fullaccess" }] }),
    /default_exec_mode/
  );
  assert.throws(
    () => parseSessionPersonasListResult({ enabled: true, personas: [{ name: "code", allow_write_globs: "docs/**" }] }),
    /allow_write_globs/
  );
  assert.throws(
    () => parseSessionPersonasListResult({ enabled: true, personas: [{ name: "code", allow_write_globs: [42] }] }),
    /allow_write_globs/
  );
});

test("parseSessionPersonasListResult bounds persona names and rejects oversized lists", () => {
  const longest = `a${"b".repeat(63)}`;
  const parsed = parseSessionPersonasListResult({ enabled: true, personas: [{ name: longest }] });
  assert.equal(parsed.personas[0].name, longest);
  assert.throws(
    () => parseSessionPersonasListResult({ enabled: true, personas: [{ name: `a${"b".repeat(64)}` }] }),
    /name/
  );
  const oversized = Array.from({ length: MAX_PERSONA_LIST_ENTRIES + 1 }, (_, index) => ({ name: `p${index}` }));
  assert.throws(
    () => parseSessionPersonasListResult({ enabled: true, personas: oversized }),
    new RegExp(String(MAX_PERSONA_LIST_ENTRIES))
  );
  assert.throws(
    () => parseSessionPersonasListResult({
      enabled: true,
      personas: [{ name: "code", allow_write_globs: ["g".repeat(513)] }]
    }),
    /allow_write_globs/
  );
});

test("parseSessionPersonaSetResult accepts the wire contract and rejects drift", () => {
  const result = parseSessionPersonaSetResult({
    persona: "architect",
    effective_mode: "review",
    model_role: "planner",
    changed: true
  });
  assert.deepEqual(result, { persona: "architect", effective_mode: "review", model_role: "planner", changed: true });

  assert.throws(() => parseSessionPersonaSetResult(null), ProtocolPayloadError);
  assert.throws(() => parseSessionPersonaSetResult({ persona: "architect", effective_mode: "review" }), /changed/);
  assert.throws(
    () => parseSessionPersonaSetResult({ persona: "architect", effective_mode: "", changed: true }),
    /effective_mode/
  );
  assert.throws(
    () => parseSessionPersonaSetResult({ persona: "architect", effective_mode: "fullaccess", changed: true }),
    /effective_mode/
  );
  assert.throws(
    () => parseSessionPersonaSetResult({ persona: "not a name", effective_mode: "review", changed: true }),
    /persona/
  );
});

test("parsePersonaChangedPayload accepts the event contract and rejects drift", () => {
  const payload = parsePersonaChangedPayload({ persona: "architect", effective_mode: "review", source: "user" });
  assert.deepEqual(payload, { persona: "architect", effective_mode: "review", source: "user" });
  // Non-user sources (e.g. a model-proposed switch approved in the CLI) are valid.
  assert.equal(parsePersonaChangedPayload({ persona: "debug", effective_mode: "auto", source: "model" }).source, "model");

  assert.throws(() => parsePersonaChangedPayload("persona"), ProtocolPayloadError);
  assert.throws(() => parsePersonaChangedPayload({ persona: "", effective_mode: "review", source: "user" }), /persona/);
  assert.throws(() => parsePersonaChangedPayload({ persona: "architect", effective_mode: "off", source: "user" }), /effective_mode/);
  assert.throws(() => parsePersonaChangedPayload({ persona: "architect", effective_mode: "review", source: 4 }), /source/);
});

test("isValidPersonaName bounds names to the CLI profile-like pattern", () => {
  assert.equal(isValidPersonaName("code"), true);
  assert.equal(isValidPersonaName("My.Custom_persona-2"), true);
  assert.equal(isValidPersonaName(""), false);
  assert.equal(isValidPersonaName(".hidden"), false);
  assert.equal(isValidPersonaName("has space"), false);
  assert.equal(isValidPersonaName("../traversal"), false);
  assert.equal(isValidPersonaName(`a${"b".repeat(64)}`), false);
  assert.equal(isValidPersonaName(42), false);
});

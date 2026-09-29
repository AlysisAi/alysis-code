from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from alysis_code.agent.tools_assembly import _BUILTIN_MODEL_DESCRIPTIONS, ToolDef
from alysis_code.context.tool_schema_budgeter import compact_custom_mcp_tool_parameters
from alysis_code.tools.registry import require_builtin_tool_metadata


def _wire(schema: dict[str, Any], *, family: str = "builtin", compact: bool = True) -> dict:
    def never_run(_arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("Serialization must not dispatch a tool")

    tool = ToolDef(
        name="example",
        description="  Short calling contract.  ",
        parameters=schema,
        run=never_run,
        metadata={"tool_type": family, "compact_parameters_for_model": compact},
    )
    # Exercise the delivered JSON shape rather than relying on Python identity.
    return json.loads(json.dumps(tool.as_openai_tool()))["function"]


def test_builtin_parameters_keep_exact_meaning_and_validation_without_ui_annotations() -> None:
    description = 'Use the literal "two  spaces"; paths are case-sensitive.\nΌχι μετατροπή.'
    schema = {
        "type": "object",
        "title": "Editor label",
        "$comment": "Internal annotation",
        "properties": {
            "query": {
                "type": "string",
                "description": description,
                "markdownDescription": "Redundant editor prose",
                "examples": ["example"],
                "minLength": 1,
                "maxLength": 4000,
                "pattern": "^[^\\x00]+$",
            },
            "window": {
                "type": "array",
                "description": "Inclusive one-based start and end lines.",
                "minItems": 2,
                "maxItems": 2,
                "items": {"type": "integer", "minimum": 1, "description": "Line number."},
            },
            "timeout_s": {
                "type": "number",
                "description": "Seconds to wait, not a command deadline; zero polls immediately.",
                "minimum": 0,
                "maximum": 3600,
                "default": 30,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    original = copy.deepcopy(schema)
    expected = copy.deepcopy(schema)
    del expected["title"], expected["$comment"]
    del expected["properties"]["query"]["markdownDescription"]
    del expected["properties"]["query"]["examples"]

    emitted = _wire(schema)

    assert emitted["parameters"] == expected
    assert emitted["parameters"]["properties"]["query"]["description"] == description
    assert emitted["description"] == "Short calling contract."
    assert schema == original


def test_builtin_compaction_keeps_property_names_refs_dependencies_and_literal_payloads() -> None:
    literal = {"description": "payload", "title": "payload", "examples": [{"default": 1}]}
    schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "User-supplied description."},
            "title": {"type": "string", "default": "Untitled"},
            "examples": {"type": "array", "items": {"$ref": "#/$defs/description"}},
            "default": {"const": literal},
            "enum": {"enum": [literal], "default": literal},
        },
        "$defs": {"description": {"type": "object", "description": "An example."}},
        "patternProperties": {"^title$": {"type": "string", "description": "A title."}},
        "dependentRequired": {"description": ["title"], "title": ["examples"]},
        "dependentSchemas": {"description": {"required": ["title"]}},
        "dependencies": {"title": ["description"], "description": {"required": ["examples"]}},
        "required": ["description", "title"],
        "allOf": [{"if": {"required": ["enum"]}, "then": {"required": ["default"]}}],
    }
    original = copy.deepcopy(schema)

    assert _wire(schema)["parameters"] == original
    assert schema == original


@pytest.mark.parametrize("family", ["custom_tool", "custom", "mcp", "mcp_tool"])
def test_external_tool_compaction_keeps_its_existing_policy(family: str) -> None:
    schema = {
        "type": "object",
        "description": "External root description",
        "properties": {
            "description": {
                "type": "string",
                "description": "External parameter prose",
                "default": "external default",
            },
            "mode": {"type": "string", "enum": ["fast", "safe"]},
        },
        "required": ["description"],
    }
    original = copy.deepcopy(schema)

    emitted = _wire(schema, family=family)["parameters"]

    assert emitted == compact_custom_mcp_tool_parameters(original)
    assert emitted["properties"]["description"] == {"type": "string"}
    assert emitted["properties"]["mode"]["enum"] == ["fast", "safe"]
    assert schema == original


def test_uncompacted_tool_schema_is_unchanged() -> None:
    schema = {"title": "Title", "description": "Description", "default": {"title": "data"}}
    assert _wire(schema, compact=False)["parameters"] == schema


@pytest.mark.parametrize(
    "name", ["subagent_wait", "subagent_resume", "subagent_send", "subagent_apply"]
)
def test_builtin_registry_calling_details_reach_serialized_schema(name: str) -> None:
    spec = require_builtin_tool_metadata(name)
    emitted = _wire(spec.parameters)["parameters"]
    described_fields = {
        key: field["description"]
        for key, field in spec.parameters["properties"].items()
        if "description" in field
    }
    assert described_fields, "This regression needs actual authored calling semantics"
    for key, description in described_fields.items():
        assert emitted["properties"][key]["description"] == description
    assert emitted.get("required") == spec.parameters.get("required")
    assert _wire(spec.parameters)["parameters"] == emitted


def test_delivered_lifecycle_hints_describe_retrieval_and_linked_continuation() -> None:
    def never_run(_arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("Serialization must not dispatch a tool")

    emitted = {}
    for name in ("subagent_wait", "subagent_resume"):
        spec = require_builtin_tool_metadata(name)
        emitted[name] = ToolDef(
            name=name,
            description=spec.description,
            parameters=spec.parameters,
            run=never_run,
            metadata={
                "tool_type": "builtin",
                "compact_parameters_for_model": True,
                "model_description": _BUILTIN_MODEL_DESCRIPTIONS[name],
            },
        ).as_openai_tool()["function"]

    wait = emitted["subagent_wait"]
    assert "retrieve its full result" in wait["description"]
    assert "Bounded completed reports" in wait["description"]
    assert "full_result locator" in wait["description"]
    assert wait["parameters"]["properties"]["run_id"]["default"] == "all"
    assert wait["parameters"]["properties"]["timeout_s"]["minimum"] == 0

    resume = emitted["subagent_resume"]
    assert "new linked background run" in resume["description"]
    assert "returned latest run_id" in resume["description"]
    assert "each source continues once" in resume["description"]
    assert "follow-up task" in resume["description"]
    assert resume["parameters"]["properties"]["reattach_workspace"]["default"] is True

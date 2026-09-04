"""Gemini's ``Schema`` message is an OpenAPI subset; JSON Schema keywords 400.

Regression for the live failure seen on 2026-09-02 with gemini-3.8-flash:
``Unknown name "additionalProperties" at 'tools[0].function_declarations[4]
.parameters'`` and ``Unknown name "uniqueItems" ...``. Every tool the agent
registers is plain JSON Schema, so the native client must project it.
"""

from __future__ import annotations

from typing import Any

from alysis_code.llm.gemini_generate_content import (
    _GEMINI_SCHEMA_FIELDS,
    _gemini_function_declaration_from_chat_tool,
    _gemini_response_format,
    _gemini_schema,
)


def _assert_only_gemini_fields(schema: Any, path: str = "$") -> None:
    assert isinstance(schema, dict), path
    for key, value in schema.items():
        assert key in _GEMINI_SCHEMA_FIELDS, f"{path}.{key}"
        if key == "properties":
            for name, child in value.items():
                _assert_only_gemini_fields(child, f"{path}.properties.{name}")
        elif key == "items":
            _assert_only_gemini_fields(value, f"{path}.items")
        elif key == "anyOf":
            for index, child in enumerate(value):
                _assert_only_gemini_fields(child, f"{path}.anyOf[{index}]")


def test_strips_additional_properties_and_unique_items_everywhere() -> None:
    raw = {
        "type": "object",
        "additionalProperties": False,
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "properties": {
            "paths": {
                "type": "array",
                "uniqueItems": True,
                "minItems": 1,
                "items": {
                    "anyOf": [
                        {"type": "string", "additionalProperties": False},
                        {"type": "object", "additionalProperties": {"type": "string"}},
                    ]
                },
            },
            "mode": {"type": "string", "enum": ["fast", "safe"], "examples": ["fast"]},
        },
        "required": ["paths", "mode", "not_declared"],
    }

    cleaned = _gemini_schema(raw)

    _assert_only_gemini_fields(cleaned)
    assert cleaned["type"] == "object"
    assert cleaned["required"] == ["paths", "mode"]
    assert cleaned["properties"]["paths"]["minItems"] == 1
    assert "uniqueItems" not in cleaned["properties"]["paths"]
    branches = cleaned["properties"]["paths"]["items"]["anyOf"]
    assert branches == [{"type": "string"}, {"type": "object"}]
    assert cleaned["properties"]["mode"] == {"type": "string", "enum": ["fast", "safe"]}


def test_union_types_and_null_branches_become_nullable() -> None:
    cleaned = _gemini_schema(
        {
            "type": "object",
            "properties": {
                "limit": {"type": ["integer", "null"], "minimum": 0},
                "target": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "either": {"type": ["string", "integer"]},
            },
        }
    )

    _assert_only_gemini_fields(cleaned)
    assert cleaned["properties"]["limit"] == {"type": "integer", "nullable": True, "minimum": 0}
    assert cleaned["properties"]["target"] == {"type": "string", "nullable": True}
    assert cleaned["properties"]["either"]["anyOf"] == [{"type": "string"}, {"type": "integer"}]


def test_refs_const_one_of_and_formats_are_rewritten() -> None:
    cleaned = _gemini_schema(
        {
            "type": "object",
            "$defs": {"Point": {"type": "object", "properties": {"x": {"type": "number"}}}},
            "properties": {
                "origin": {"$ref": "#/$defs/Point", "description": "start"},
                "kind": {"const": "move"},
                "size": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
                "url": {"type": "string", "format": "uri"},
                "when": {"type": "string", "format": "date-time"},
                "count": {"type": "integer", "enum": [1, 2, 3]},
                "tags": {"type": "array"},
            },
        }
    )

    _assert_only_gemini_fields(cleaned)
    props = cleaned["properties"]
    assert props["origin"]["properties"]["x"] == {"type": "number"}
    assert props["origin"]["description"] == "start"
    assert props["kind"] == {"type": "string", "enum": ["move"]}
    assert props["size"]["anyOf"] == [{"type": "integer"}, {"type": "string"}]
    assert "format" not in props["url"]
    assert props["when"]["format"] == "date-time"
    # Non-string enums are not validated by Gemini; the values survive as a hint.
    assert props["count"]["type"] == "integer"
    assert "enum" not in props["count"]
    assert "Allowed values: 1, 2, 3" in props["count"]["description"]
    assert props["tags"] == {"type": "array", "items": {}}


def test_recursive_refs_terminate() -> None:
    schema = {
        "type": "object",
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/$defs/Node"}},
            }
        },
        "properties": {"root": {"$ref": "#/$defs/Node"}},
    }

    cleaned = _gemini_schema(schema)

    _assert_only_gemini_fields(cleaned)
    assert cleaned["properties"]["root"]["type"] == "object"


def test_function_declaration_and_response_schema_use_the_projection() -> None:
    declaration = _gemini_function_declaration_from_chat_tool(
        {
            "type": "function",
            "function": {
                "name": "fs_edit",
                "description": "Edit a file",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    )
    assert declaration is not None
    assert declaration["parameters"] == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    # A tool with no parameters still declares an object schema.
    bare = _gemini_function_declaration_from_chat_tool(
        {"type": "function", "function": {"name": "noop"}}
    )
    assert bare is not None
    assert bare["parameters"] == {"type": "object"}

    response = _gemini_response_format(
        {
            "type": "json_schema",
            "json_schema": {
                "name": "verdict",
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"ok": {"type": "boolean"}},
                },
            },
        }
    )
    assert response["responseSchema"] == {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
    }

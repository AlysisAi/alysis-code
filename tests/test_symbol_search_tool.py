from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code.agent_loop import build_tools
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
from alysis_code.tools.symbols import SymbolSearchError, symbol_search


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="symbol-search-test",
        cwd=str(root),
        repo_root=str(root),
    )


def _build_tools(tmp_path: Path):
    return build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO(), force_terminal=False),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        non_interactive=True,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_symbol_search_finds_python_symbols(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "demo.py",
        (
            "DEFAULT_TIMEOUT = 30\n"
            "\n"
            "class Parser(BaseParser):\n"
            "    def run(self, payload: str) -> str:\n"
            "        return payload\n"
            "\n"
            "async def load_config(name: str) -> dict[str, str]:\n"
            '    return {"name": name}\n'
        ),
    )

    class_result = symbol_search(root=tmp_path, query="Parser", kind="class", exact=True)
    assert class_result["backend"] == "python_ast"
    assert class_result["parsed_files"] == 1
    assert class_result["matches"] == [
        {
            "path": "src/demo.py",
            "line": 3,
            "kind": "class",
            "name": "Parser",
            "signature": "class Parser(BaseParser)",
        }
    ]

    method_result = symbol_search(root=tmp_path, query="run", kind="method", exact=True)
    assert method_result["matches"] == [
        {
            "path": "src/demo.py",
            "line": 4,
            "kind": "method",
            "name": "Parser.run",
            "signature": "def Parser.run(self, payload: str) -> str",
        }
    ]

    function_result = symbol_search(root=tmp_path, query="load", kind="function")
    assert function_result["matches"] == [
        {
            "path": "src/demo.py",
            "line": 7,
            "kind": "function",
            "name": "load_config",
            "signature": "async def load_config(name: str) -> dict[str, str]",
        }
    ]

    constant_result = symbol_search(root=tmp_path, query="DEFAULT", kind="constant")
    assert constant_result["matches"] == [
        {
            "path": "src/demo.py",
            "line": 1,
            "kind": "constant",
            "name": "DEFAULT_TIMEOUT",
            "signature": "DEFAULT_TIMEOUT = 30",
        }
    ]


def test_symbol_search_can_include_details_snippets_and_references(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "demo.py",
        (
            "class Parser:\n"
            "    def run(self, payload: str) -> str:\n"
            "        return payload\n"
            "\n"
            "def caller(parser: Parser) -> str:\n"
            "    return parser.run('ok')\n"
        ),
    )

    result = symbol_search(
        root=tmp_path,
        query="run",
        kind="method",
        exact=True,
        include_details=True,
        include_snippet=True,
        include_references=True,
    )

    assert result["include_details"] is True
    assert result["include_snippet"] is True
    assert result["include_references"] is True
    assert result["matches"] == [
        {
            "path": "src/demo.py",
            "line": 2,
            "kind": "method",
            "name": "Parser.run",
            "signature": "def Parser.run(self, payload: str) -> str",
            "end_line": 3,
            "parent": "Parser",
            "snippet": {
                "start_line": 2,
                "end_line": 3,
                "truncated": False,
                "lines": [
                    {"line": 2, "text": "def run(self, payload: str) -> str:"},
                    {"line": 3, "text": "return payload"},
                ],
            },
            "references": [
                {
                    "path": "src/demo.py",
                    "line": 6,
                    "text": "return parser.run('ok')",
                }
            ],
        }
    ]


def test_symbol_search_finds_js_ts_symbols(tmp_path: Path) -> None:
    _write(
        tmp_path / "web" / "app.ts",
        (
            "export async function fetchDoc(url: string) {\n"
            "  return url;\n"
            "}\n"
            "\n"
            "export default function buildClient() {\n"
            "  return {};\n"
            "}\n"
            "\n"
            "export class Parser {\n"
            "  run(payload: string): string {\n"
            "    return payload;\n"
            "  }\n"
            "}\n"
            "\n"
            "export const handler = (req: Request) => req;\n"
            "const buildThing = function(name: string) {\n"
            "  return name;\n"
            "};\n"
            "const DEFAULT_TIMEOUT = 30;\n"
            "const plainValue = createValue();\n"
        ),
    )

    class_result = symbol_search(root=tmp_path, query="Parser", kind="class", exact=True)
    assert class_result["backend"] == "js_ts_heuristic"
    assert class_result["parsed_files"] == 1
    assert class_result["matches"] == [
        {
            "path": "web/app.ts",
            "line": 9,
            "kind": "class",
            "name": "Parser",
            "signature": "export class Parser {",
        }
    ]

    method_result = symbol_search(root=tmp_path, query="run", kind="method", exact=True)
    assert method_result["matches"] == [
        {
            "path": "web/app.ts",
            "line": 10,
            "kind": "method",
            "name": "Parser.run",
            "signature": "run(payload: string): string {",
        }
    ]

    function_result = symbol_search(root=tmp_path, query="handler", kind="function", exact=True)
    assert function_result["matches"] == [
        {
            "path": "web/app.ts",
            "line": 15,
            "kind": "function",
            "name": "handler",
            "signature": "export const handler = (req: Request) => req;",
        }
    ]

    constant_result = symbol_search(root=tmp_path, query="plainValue", kind="constant", exact=True)
    assert constant_result["matches"] == [
        {
            "path": "web/app.ts",
            "line": 20,
            "kind": "constant",
            "name": "plainValue",
            "signature": "const plainValue = createValue();",
        }
    ]


def test_symbol_search_js_ts_exact_and_kind_filters(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "handlers.ts",
        (
            "export default function buildClient() {\n"
            "  return {};\n"
            "}\n"
            "export const buildHandler = (req: Request) => req;\n"
            "const buildCount = 3;\n"
        ),
    )

    exact_result = symbol_search(root=tmp_path, query="build", kind="function", exact=True)
    assert exact_result["matches"] == []

    filtered_result = symbol_search(root=tmp_path, query="build", kind="function")
    assert [match["name"] for match in filtered_result["matches"]] == [
        "buildClient",
        "buildHandler",
    ]

    constant_result = symbol_search(root=tmp_path, query="build", kind="constant")
    assert [match["name"] for match in constant_result["matches"]] == ["buildCount"]


def test_symbol_search_finds_typed_top_level_const_arrow_function(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "typed.ts",
        "const buildThing: (name: string) => string = (name) => name;\n",
    )

    result = symbol_search(root=tmp_path, query="buildThing", kind="function", exact=True)

    assert result["matches"] == [
        {
            "path": "ui/typed.ts",
            "line": 1,
            "kind": "function",
            "name": "buildThing",
            "signature": "const buildThing: (name: string) => string = (name) => name;",
        }
    ]


def test_symbol_search_finds_typed_multiline_top_level_const_arrow_function(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "typed_multiline.ts",
        ("const buildThing: (name: string) => string =\n  (name) => name;\n"),
    )

    result = symbol_search(root=tmp_path, query="buildThing", kind="function", exact=True)

    assert result["matches"] == [
        {
            "path": "ui/typed_multiline.ts",
            "line": 1,
            "kind": "function",
            "name": "buildThing",
            "signature": "const buildThing: (name: string) => string = (name) => name;",
        }
    ]


def test_symbol_search_finds_typed_top_level_const_function_expression(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "typed_function.ts",
        "const buildThing: (name: string) => string = function(name) { return name; };\n",
    )

    result = symbol_search(root=tmp_path, query="buildThing", kind="function", exact=True)

    assert result["matches"] == [
        {
            "path": "ui/typed_function.ts",
            "line": 1,
            "kind": "function",
            "name": "buildThing",
            "signature": (
                "const buildThing: (name: string) => string = function(name) { return name; };"
            ),
        }
    ]


def test_symbol_search_finds_typed_class_field_arrow_as_method(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "view_typed.tsx",
        ("class View {\n  private renderItem: (item: Item) => Item =\n    (item) => item;\n}\n"),
    )

    result = symbol_search(root=tmp_path, query="renderItem", kind="method", exact=True)

    assert result["matches"] == [
        {
            "path": "ui/view_typed.tsx",
            "line": 2,
            "kind": "method",
            "name": "View.renderItem",
            "signature": "private renderItem: (item: Item) => Item = (item) => item;",
        }
    ]


def test_symbol_search_finds_optional_typed_class_field_arrow_as_method(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "view_optional.tsx",
        ("class View {\n  onClick?: () => void = () => {};\n}\n"),
    )

    result = symbol_search(root=tmp_path, query="onClick", kind="method", exact=True)

    assert result["matches"] == [
        {
            "path": "ui/view_optional.tsx",
            "line": 2,
            "kind": "method",
            "name": "View.onClick",
            "signature": "onClick?: () => void = () => {};",
        }
    ]


def test_symbol_search_applies_exact_and_kind_filters_to_typed_ts_assignment_patterns(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "ui" / "typed_filters.tsx",
        (
            "export const Table: <T>(rows: T[]) => number = <T>(rows) => rows.length;\n"
            "const TableValue = createValue();\n"
            "class View {\n"
            "  onClick?: () => void = () => {};\n"
            "}\n"
        ),
    )

    function_result = symbol_search(root=tmp_path, query="Table", kind="function", exact=True)
    method_result = symbol_search(root=tmp_path, query="onClick", kind="method", exact=True)
    constant_result = symbol_search(root=tmp_path, query="Table", kind="constant", exact=True)

    assert [match["name"] for match in function_result["matches"]] == ["Table"]
    assert [match["name"] for match in method_result["matches"]] == ["View.onClick"]
    assert constant_result["matches"] == []


def test_symbol_search_finds_generic_arrow_with_nested_constraints(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "nested_generic.ts",
        "const Table = <T extends Record<string, number>>(rows: T[]) => rows.length;\n",
    )

    result = symbol_search(root=tmp_path, query="Table", kind="function", exact=True)

    assert result["matches"] == [
        {
            "path": "ui/nested_generic.ts",
            "line": 1,
            "kind": "function",
            "name": "Table",
            "signature": "const Table = <T extends Record<string, number>>(rows: T[]) => rows.length;",
        }
    ]


def test_symbol_search_finds_exported_generic_arrow_with_nested_constraints(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "nested_export.ts",
        "export const Table = <T extends Foo<Bar<Baz>>>(rows: T[]) => rows.length;\n",
    )

    result = symbol_search(root=tmp_path, query="Table", kind="function", exact=True)

    assert result["matches"] == [
        {
            "path": "ui/nested_export.ts",
            "line": 1,
            "kind": "function",
            "name": "Table",
            "signature": "export const Table = <T extends Foo<Bar<Baz>>>(rows: T[]) => rows.length;",
        }
    ]


def test_symbol_search_finds_typed_generic_arrow_with_nested_constraints(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "nested_typed.ts",
        (
            "const Table: <T extends Record<string, number>>(rows: T[]) => number = "
            "<T extends Record<string, number>>(rows) => rows.length;\n"
        ),
    )

    result = symbol_search(root=tmp_path, query="Table", kind="function", exact=True)

    assert result["matches"] == [
        {
            "path": "ui/nested_typed.ts",
            "line": 1,
            "kind": "function",
            "name": "Table",
            "signature": (
                "const Table: <T extends Record<string, number>>(rows: T[]) => number = "
                "<T extends Record<string, number>>(rows) => rows.length;"
            ),
        }
    ]


def test_symbol_search_finds_multiline_generic_arrow_with_nested_constraints(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "ui" / "nested_multiline.ts",
        ("const Table =\n  <T extends Foo<Bar<Baz>>>(rows: T[]) => rows.length;\n"),
    )

    result = symbol_search(root=tmp_path, query="Table", kind="function", exact=True)

    assert result["matches"] == [
        {
            "path": "ui/nested_multiline.ts",
            "line": 1,
            "kind": "function",
            "name": "Table",
            "signature": "const Table = <T extends Foo<Bar<Baz>>>(rows: T[]) => rows.length;",
        }
    ]


def test_symbol_search_finds_class_field_generic_arrow_with_nested_constraints(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "ui" / "nested_method.tsx",
        (
            "class View {\n"
            "  render = <T extends Record<string, number>>(rows: T[]) => rows.length;\n"
            "}\n"
        ),
    )

    result = symbol_search(root=tmp_path, query="render", kind="method", exact=True)

    assert result["matches"] == [
        {
            "path": "ui/nested_method.tsx",
            "line": 2,
            "kind": "method",
            "name": "View.render",
            "signature": "render = <T extends Record<string, number>>(rows: T[]) => rows.length;",
        }
    ]


def test_symbol_search_finds_typed_class_field_generic_arrow_with_nested_constraints(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "ui" / "nested_typed_method.tsx",
        (
            "class View {\n"
            "  private render: <T extends Foo<Bar<Baz>>>(rows: T[]) => number =\n"
            "    <T extends Foo<Bar<Baz>>>(rows) => rows.length;\n"
            "}\n"
        ),
    )

    result = symbol_search(root=tmp_path, query="render", kind="method", exact=True)

    assert result["matches"] == [
        {
            "path": "ui/nested_typed_method.tsx",
            "line": 2,
            "kind": "method",
            "name": "View.render",
            "signature": (
                "private render: <T extends Foo<Bar<Baz>>>(rows: T[]) => number = "
                "<T extends Foo<Bar<Baz>>>(rows) => rows.length;"
            ),
        }
    ]


def test_symbol_search_applies_exact_and_kind_filters_to_nested_generic_arrow_patterns(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "ui" / "nested_filters.tsx",
        (
            "export const Table = <T extends Record<string, number>>(rows: T[]) => rows.length;\n"
            "const TableValue = createValue();\n"
            "class View {\n"
            "  render = <T extends Foo<Bar<Baz>>>(rows: T[]) => rows.length;\n"
            "}\n"
        ),
    )

    function_result = symbol_search(root=tmp_path, query="Table", kind="function", exact=True)
    method_result = symbol_search(root=tmp_path, query="render", kind="method", exact=True)
    constant_result = symbol_search(root=tmp_path, query="Table", kind="constant", exact=True)

    assert [match["name"] for match in function_result["matches"]] == ["Table"]
    assert [match["name"] for match in method_result["matches"]] == ["View.render"]
    assert constant_result["matches"] == []


def test_symbol_search_finds_multiline_top_level_arrow_function(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "app.tsx",
        ("export const App = (\n  props: Props,\n) => {\n  return props;\n};\n"),
    )

    result = symbol_search(root=tmp_path, query="App", kind="function", exact=True)

    assert result["backend"] == "js_ts_heuristic"
    assert result["matches"] == [
        {
            "path": "ui/app.tsx",
            "line": 1,
            "kind": "function",
            "name": "App",
            "signature": "export const App = ( props: Props, ) => {",
        }
    ]


def test_symbol_search_finds_multiline_top_level_function_expression_assigned_to_const(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "ui" / "builders.ts",
        ("const buildThing =\n  function(\n    name: string,\n  ) {\n    return name;\n  };\n"),
    )

    result = symbol_search(root=tmp_path, query="buildThing", kind="function", exact=True)

    assert result["matches"] == [
        {
            "path": "ui/builders.ts",
            "line": 1,
            "kind": "function",
            "name": "buildThing",
            "signature": "const buildThing = function( name: string, ) {",
        }
    ]


def test_symbol_search_finds_generic_ts_arrow_functions(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "table.tsx",
        (
            "export const Table = <T,>({ rows }: Props<T>) => rows.length;\n"
            "const mapValues = <T>(items: T[]) => items;\n"
        ),
    )

    table_result = symbol_search(root=tmp_path, query="Table", kind="function", exact=True)
    map_values_result = symbol_search(root=tmp_path, query="mapValues", kind="function", exact=True)

    assert [match["name"] for match in table_result["matches"]] == ["Table"]
    assert [match["name"] for match in map_values_result["matches"]] == ["mapValues"]
    assert table_result["matches"][0]["signature"] == (
        "export const Table = <T,>({ rows }: Props<T>) => rows.length;"
    )
    assert map_values_result["matches"][0]["signature"] == (
        "const mapValues = <T>(items: T[]) => items;"
    )


def test_symbol_search_finds_class_field_function_patterns_as_methods(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "view.tsx",
        (
            "export class View {\n"
            "  handleClick = () => {\n"
            "    return true;\n"
            "  };\n"
            "\n"
            "  private renderItem = function(\n"
            "    item: Item,\n"
            "  ) {\n"
            "    return item;\n"
            "  };\n"
            "}\n"
        ),
    )

    result = symbol_search(root=tmp_path, query="View.", kind="method")

    assert result["matches"] == [
        {
            "path": "ui/view.tsx",
            "line": 2,
            "kind": "method",
            "name": "View.handleClick",
            "signature": "handleClick = () => {",
        },
        {
            "path": "ui/view.tsx",
            "line": 6,
            "kind": "method",
            "name": "View.renderItem",
            "signature": "private renderItem = function( item: Item, ) {",
        },
    ]


def test_symbol_search_applies_exact_and_kind_filters_to_new_js_ts_patterns(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "mixed.tsx",
        (
            "export const Table = <T,>({ rows }: Props<T>) => rows.length;\n"
            "const TableValue = createValue();\n"
            "class View {\n"
            "  handleClick = () => true;\n"
            "}\n"
        ),
    )

    function_result = symbol_search(root=tmp_path, query="Table", kind="function", exact=True)
    method_result = symbol_search(root=tmp_path, query="handleClick", kind="method", exact=True)
    constant_result = symbol_search(root=tmp_path, query="Table", kind="constant", exact=True)

    assert [match["name"] for match in function_result["matches"]] == ["Table"]
    assert [match["name"] for match in method_result["matches"]] == ["View.handleClick"]
    assert constant_result["matches"] == []


def test_symbol_search_reports_mixed_static_backend(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "demo.py", "def build_tools() -> None:\n    pass\n")
    _write(tmp_path / "web" / "demo.ts", "export function buildTools() {}\n")

    result = symbol_search(root=tmp_path, query="build")

    assert [match["name"] for match in result["matches"]] == ["build_tools", "buildTools"]
    assert result["backend"] == "mixed_static"
    assert result["parsed_files"] == 2


def test_symbol_search_finds_java_symbols(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "main" / "java" / "com" / "example" / "CSVParser.java",
        (
            "package com.example;\n"
            "\n"
            "import java.io.IOException;\n"
            "\n"
            "@Deprecated\n"
            "public final class CSVParser<T> {\n"
            "  public CSVParser() {}\n"
            "  public static <R> R parseLine(String input) throws IOException { return null; }\n"
            "  private final void reset() {}\n"
            "}\n"
            "\n"
            "interface Loader<T> {\n"
            "  T load(String name) throws IOException;\n"
            "}\n"
            "\n"
            "enum Mode {\n"
            "  FAST, SLOW;\n"
            "  public boolean enabled() { return true; }\n"
            "}\n"
            "\n"
            "record UserRecord(String id) {\n"
            "  public String label() { return id; }\n"
            "}\n"
        ),
    )

    class_result = symbol_search(root=tmp_path, query="CSVParser", kind="class", exact=True)
    constructor_result = symbol_search(root=tmp_path, query="CSVParser", kind="method", exact=True)
    method_result = symbol_search(root=tmp_path, query="parseLine", kind="method", exact=True)
    interface_result = symbol_search(root=tmp_path, query="Loader", kind="class", exact=True)
    enum_result = symbol_search(root=tmp_path, query="Mode", kind="class", exact=True)
    record_result = symbol_search(root=tmp_path, query="UserRecord", kind="class", exact=True)

    assert class_result["backend"] == "java_heuristic"
    assert class_result["parsed_files"] == 1
    assert [match["name"] for match in class_result["matches"]] == ["CSVParser"]
    assert [match["name"] for match in constructor_result["matches"]] == ["CSVParser.CSVParser"]
    assert [match["name"] for match in method_result["matches"]] == ["CSVParser.parseLine"]
    assert [match["name"] for match in interface_result["matches"]] == ["Loader"]
    assert [match["name"] for match in enum_result["matches"]] == ["Mode"]
    assert [match["name"] for match in record_result["matches"]] == ["UserRecord"]
    assert method_result["matches"][0]["signature"] == (
        "public static <R> R parseLine(String input) throws IOException { return null; }"
    )


def test_symbol_search_respects_scope_globs_exact_and_max_results(tmp_path: Path) -> None:
    _write(
        tmp_path / "pkg" / "alpha.py",
        "def build_index() -> None:\n    pass\n",
    )
    _write(
        tmp_path / "pkg" / "beta.py",
        "def build_graph() -> None:\n    pass\n",
    )
    _write(
        tmp_path / "pkg" / "nested" / "gamma.py",
        "def build_service() -> None:\n    pass\n",
    )

    exact_result = symbol_search(
        root=tmp_path,
        query="build_graph",
        root_path="pkg",
        globs=["pkg/*.py"],
        exact=True,
    )
    assert [match["name"] for match in exact_result["matches"]] == ["build_graph"]

    limited = symbol_search(
        root=tmp_path,
        query="build",
        root_path="pkg",
        max_results=2,
    )
    assert len(limited["matches"]) == 2
    assert limited["truncated"] is True


def test_symbol_search_reports_only_truly_unsupported_files(tmp_path: Path) -> None:
    _write(tmp_path / "web" / "app.rb", "def handler\n  :ok\nend\n")

    result = symbol_search(root=tmp_path, query="handler", root_path="web/app.rb")

    assert result["matches"] == []
    assert result["parsed_files"] == 0
    assert result["notes"] == ["Skipped unsupported file(s): web/app.rb"]


def test_symbol_search_rejects_invalid_inputs(tmp_path: Path) -> None:
    with pytest.raises(SymbolSearchError, match="query must be a non-empty string"):
        symbol_search(root=tmp_path, query="")

    with pytest.raises(SymbolSearchError, match="Unsupported kind"):
        symbol_search(root=tmp_path, query="x", kind="variable")

    with pytest.raises(SymbolSearchError, match="root_path escapes root"):
        symbol_search(root=tmp_path, query="x", root_path="../outside.py")


def test_build_tools_registers_symbol_search(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    assert "symbol_search" in tools
    schema = tools["symbol_search"].as_openai_tool()["function"]["parameters"]
    assert schema["required"] == ["query"]
    assert schema["properties"]["kind"]["enum"] == [
        "function",
        "class",
        "method",
        "constant",
    ]
    assert schema["properties"]["max_results"]["default"] == 100
    assert schema["properties"]["exact"]["default"] is False
    assert schema["properties"]["include_details"]["default"] is False
    assert schema["properties"]["include_snippet"]["default"] is False
    assert schema["properties"]["include_references"]["default"] is False

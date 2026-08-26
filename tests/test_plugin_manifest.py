from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from alysis_code.extensions.manifest import (
    MANIFEST_FILENAME,
    MAX_MANIFEST_BYTES,
    PluginManifest,
    PluginManifestError,
    load_manifest,
)


def _write_manifest(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / MANIFEST_FILENAME
    manifest_path.write_text(dedent(text).lstrip(), encoding="utf-8")
    return manifest_path


def _write_file(root: Path, relative_path: str, text: str = "# fixture\n") -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_skill(root: Path, relative_path: str) -> Path:
    skill_root = root / relative_path
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    return skill_root


def _manifest_text(
    *,
    plugin_id: str = "acme.demo",
    plugin_extra: str = "",
    compatibility_extra: str = "",
    extra: str = "",
) -> str:
    sections = [
        "schema_version = 1",
        "",
        "[plugin]",
        f'id = "{plugin_id}"',
        'name = "Demo Plugin"',
        'version = "1.2.3"',
        'description = "Demo plugin for tests"',
        'author = "Acme Inc"',
        'license = "MIT"',
    ]
    plugin_suffix = dedent(plugin_extra).strip()
    if plugin_suffix:
        sections.append(plugin_suffix)
    sections.extend(
        [
            "",
            "[compatibility]",
            'alysis = ">=0.1"',
        ]
    )
    compatibility_suffix = dedent(compatibility_extra).strip()
    if compatibility_suffix:
        sections.append(compatibility_suffix)
    extra_suffix = dedent(extra).strip()
    if extra_suffix:
        sections.extend(["", extra_suffix])
    return "\n".join(sections) + "\n"


def test_minimal_valid_manifest(tmp_path: Path) -> None:
    _write_manifest(tmp_path, _manifest_text())

    with pytest.warns(UserWarning, match="manifest declares no components"):
        manifest = load_manifest(tmp_path)

    assert isinstance(manifest, PluginManifest)
    assert manifest.schema_version == 1
    assert manifest.plugin.id == "acme.demo"
    assert manifest.components.skill == []
    assert manifest.components.tool == []
    assert manifest.components.mcp_server == []
    assert manifest.components.hook == []


def test_full_manifest_round_trip(tmp_path: Path) -> None:
    _write_skill(tmp_path, "skills/review")
    _write_file(tmp_path, "tools/review.py", "def run():\n    return {'ok': True}\n")
    _write_file(tmp_path, "hooks/post_write.py", "print('post-write')\n")
    _write_manifest(
        tmp_path,
        _manifest_text(
            plugin_extra="""
            keywords = ["coding", "automation"]
            homepage = "https://example.com/plugin"
            repository = "https://github.com/acme/demo-plugin"
            """,
            compatibility_extra='platforms = ["linux", "darwin"]',
            extra="""
            [[components.skill]]
            path = "skills/review"
            id = "review_skill"

            [[components.tool]]
            path = "tools/review.py"
            id = "review_tool"
            description = "Review repository changes"
            required_env = ["OPENAI_API_KEY"]
            network = true
            filesystem = "read"
            timeout_sec = 90

            [[components.mcp_server]]
            id = "review_mcp"
            transport = "stdio"
            command = ["python", "-m", "demo_server"]
            env = ["MCP_TOKEN"]
            scopes = ["tools/call", "resources/read"]
            oauth = { provider = "example", audience = "demo" }

            [[components.hook]]
            event = "PostWrite"
            path = "hooks/post_write.py"
            id = "post_write_hook"

            [security]
            contact = "security@example.com"
            policy_url = "https://example.com/security"
            disclosure_days = 30
            """,
        ),
    )

    manifest = load_manifest(tmp_path)

    assert manifest.plugin.keywords == ["coding", "automation"]
    assert manifest.compatibility.platforms == ["linux", "darwin"]
    assert manifest.components.skill[0].path == "skills/review"
    assert manifest.components.tool[0].required_env == ["OPENAI_API_KEY"]
    assert manifest.components.mcp_server[0].command == ["python", "-m", "demo_server"]
    assert manifest.components.mcp_server[0].oauth == {"provider": "example", "audience": "demo"}
    assert manifest.components.hook[0].event == "PostWrite"
    assert manifest.security is not None
    assert manifest.security.contact == "security@example.com"


@pytest.mark.parametrize(
    "bad_id",
    [
        "Acme.demo",
        "acmedemo",
        "acme.demo.extra",
        "1acme.demo",
        "acme.!demo",
        f"{'a' * 32}.{'b' * 32}",
    ],
)
def test_invalid_id_regex(tmp_path: Path, bad_id: str) -> None:
    _write_manifest(tmp_path, _manifest_text(plugin_id=bad_id))

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "plugin.id" in str(excinfo.value)


def test_unknown_schema_version_rejected(tmp_path: Path) -> None:
    _write_manifest(tmp_path, _manifest_text().replace("schema_version = 1", "schema_version = 2"))

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "schema_version" in str(excinfo.value)


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    _write_manifest(tmp_path, _manifest_text(extra="bogus = 1"))

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "bogus" in str(excinfo.value)


def test_unknown_component_field_rejected(tmp_path: Path) -> None:
    _write_file(tmp_path, "tools/echo.py")
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra="""
            [[components.tool]]
            path = "tools/echo.py"
            description = "Echo tool"
            network = false
            filesystem = "read"
            bogus = true
            """,
        ),
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "components.tool[0].bogus" in str(excinfo.value)


def test_path_escapes_plugin_root(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra="""
            [[components.tool]]
            path = "../escape.py"
            description = "Escape attempt"
            network = false
            filesystem = "read"
            """,
        ),
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "outside plugin root" in str(excinfo.value)


def test_absolute_path_rejected(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra="""
            [[components.tool]]
            path = "C:\\temp\\tool.py"
            description = "Absolute path"
            network = false
            filesystem = "read"
            """,
        ),
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "absolute paths are not allowed" in str(excinfo.value)


def test_path_does_not_exist(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra="""
            [[components.tool]]
            path = "tools/missing.py"
            description = "Missing tool"
            network = false
            filesystem = "read"
            """,
        ),
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "tools/missing.py" in str(excinfo.value)


def test_duplicate_effective_id_within_type(tmp_path: Path) -> None:
    _write_skill(tmp_path, "skills/foo")
    _write_skill(tmp_path, "nested/foo")
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra="""
            [[components.skill]]
            path = "skills/foo"

            [[components.skill]]
            path = "nested/foo"
            """,
        ),
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "duplicate effective id 'foo'" in str(excinfo.value)


def test_duplicate_id_across_types_allowed(tmp_path: Path) -> None:
    _write_skill(tmp_path, "skills/review")
    _write_file(tmp_path, "tools/review.py")
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra="""
            [[components.skill]]
            path = "skills/review"
            id = "shared"

            [[components.tool]]
            path = "tools/review.py"
            id = "shared"
            description = "Review tool"
            network = false
            filesystem = "read"
            """,
        ),
    )

    manifest = load_manifest(tmp_path)

    assert manifest.components.skill[0].id == "shared"
    assert manifest.components.tool[0].id == "shared"


def test_component_cap_at_32(tmp_path: Path) -> None:
    for index in range(33):
        _write_skill(tmp_path, f"skills/s{index}")

    first_32 = "\n\n".join(f'[[components.skill]]\npath = "skills/s{index}"' for index in range(32))
    _write_manifest(tmp_path, _manifest_text(extra=first_32))
    manifest = load_manifest(tmp_path)
    assert len(manifest.components.skill) == 32

    all_33 = "\n\n".join(f'[[components.skill]]\npath = "skills/s{index}"' for index in range(33))
    _write_manifest(tmp_path, _manifest_text(extra=all_33))

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "total component count 33" in str(excinfo.value)


def test_manifest_file_size_cap(tmp_path: Path) -> None:
    manifest_path = tmp_path / MANIFEST_FILENAME
    manifest_path.write_text("x" * (MAX_MANIFEST_BYTES + 1), encoding="utf-8")

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    message = str(excinfo.value)
    assert str(MAX_MANIFEST_BYTES + 1) in message
    assert "byte limit" in message


def test_toml_syntax_error_wrapped(tmp_path: Path) -> None:
    _write_manifest(tmp_path, 'schema_version = 1\n[plugin\nid = "broken"\n')

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "toml parse error" in str(excinfo.value)


def test_alysis_version_specifier_invalid(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        _manifest_text().replace('alysis = ">=0.1"', 'alysis = "not-a-spec"'),
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "compatibility.alysis" in str(excinfo.value)


@pytest.mark.parametrize(
    ("url", "should_pass"),
    [
        ("http://example.com", False),
        ("http://localhost:1234", True),
        ("http://127.0.0.1:1234", True),
        ("https://example.com", True),
    ],
)
def test_mcp_http_must_be_https_or_localhost(
    tmp_path: Path,
    url: str,
    should_pass: bool,
) -> None:
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra=f'''
            [[components.mcp_server]]
            id = "remote"
            transport = "http"
            url = "{url}"
            scopes = ["tools/call"]
            ''',
        ),
    )

    if should_pass:
        manifest = load_manifest(tmp_path)
        assert manifest.components.mcp_server[0].url is not None
    else:
        with pytest.raises(PluginManifestError) as excinfo:
            load_manifest(tmp_path)
        assert "https unless host is localhost or 127.0.0.1" in str(excinfo.value)


def test_mcp_stdio_requires_command(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra="""
            [[components.mcp_server]]
            id = "stdio"
            transport = "stdio"
            scopes = ["tools/call"]
            """,
        ),
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "command is required" in str(excinfo.value)


def test_mcp_http_requires_url(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra="""
            [[components.mcp_server]]
            id = "http"
            transport = "http"
            scopes = ["tools/call"]
            """,
        ),
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "url is required" in str(excinfo.value)


def test_mcp_http_rejects_command(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra="""
            [[components.mcp_server]]
            id = "http"
            transport = "http"
            url = "https://example.com"
            command = ["python", "-m", "server"]
            scopes = ["tools/call"]
            """,
        ),
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "command is not allowed" in str(excinfo.value)


def test_mcp_stdio_rejects_url(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra="""
            [[components.mcp_server]]
            id = "stdio"
            transport = "stdio"
            command = ["python", "-m", "server"]
            url = "https://example.com"
            scopes = ["tools/call"]
            """,
        ),
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "url is not allowed" in str(excinfo.value)


def test_required_env_regex(tmp_path: Path) -> None:
    _write_file(tmp_path, "tools/env.py")
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra="""
            [[components.tool]]
            path = "tools/env.py"
            description = "Env tool"
            required_env = ["openai_api_key"]
            network = false
            filesystem = "read"
            """,
        ),
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "required_env[0]" in str(excinfo.value)


def test_security_contact_must_be_email_or_https(tmp_path: Path) -> None:
    _write_skill(tmp_path, "skills/review")
    _write_manifest(
        tmp_path,
        _manifest_text(
            extra="""
            [[components.skill]]
            path = "skills/review"

            [security]
            contact = "ftp://example.com/security"
            """,
        ),
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "security.contact" in str(excinfo.value)


def test_uncommon_license_warns_but_loads(tmp_path: Path) -> None:
    _write_skill(tmp_path, "skills/review")
    _write_manifest(
        tmp_path,
        """
        schema_version = 1

        [plugin]
        id = "acme.demo"
        name = "Demo Plugin"
        version = "1.2.3"
        description = "Demo plugin for tests"
        author = "Acme Inc"
        license = "Custom-Internal"

        [compatibility]
        alysis = ">=0.1"

        [[components.skill]]
        path = "skills/review"
        """,
    )

    with pytest.warns(UserWarning, match="plugin.license"):
        manifest = load_manifest(tmp_path)

    assert manifest.plugin.license == "Custom-Internal"


def test_errors_are_aggregated(tmp_path: Path) -> None:
    _write_file(tmp_path, "tools/bad.py")
    _write_manifest(
        tmp_path,
        """
        schema_version = 1

        [plugin]
        id = "Acme.demo"
        name = "Demo Plugin"
        version = "1.2.3"
        description = "Demo plugin for tests"
        author = "Acme Inc"
        license = "MIT"

        [compatibility]
        alysis = "not-a-spec"

        [[components.tool]]
        path = "tools/bad.py"
        description = "Bad tool"
        required_env = ["lowercase_env"]
        network = false
        filesystem = "read"
        """,
    )

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    message = str(excinfo.value)
    assert message.count("\n  - ") == 3
    assert "plugin.id" in message
    assert "compatibility.alysis" in message
    assert "required_env[0]" in message


def test_missing_manifest_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "manifest file not found" in str(excinfo.value)


def test_non_utf8_manifest_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / MANIFEST_FILENAME
    manifest_path.write_bytes(b"\xff\xfe\xfd")

    with pytest.raises(PluginManifestError) as excinfo:
        load_manifest(tmp_path)

    assert "not valid UTF-8" in str(excinfo.value)

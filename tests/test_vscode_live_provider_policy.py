from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.qa.validate_vscode_live_provider_policy import (
    LiveProviderPolicyError,
    authorize,
    load_policy,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / ".github" / "release-policy" / "vscode-live-provider-origins.json"


def test_committed_policy_is_closed_sorted_and_authorizes_only_reviewed_origins() -> None:
    entries, digest = load_policy(POLICY.resolve())
    assert entries == {
        "deepseek": ("https://api.deepseek.com",),
        "openai": ("https://api.openai.com",),
        "openai-responses": ("https://api.openai.com",),
    }
    assert len(digest) == 64
    assert (
        authorize(POLICY.resolve(), provider="deepseek", origin="https://api.deepseek.com")
        == digest
    )
    assert (
        authorize(
            POLICY.resolve(),
            provider="openai-responses",
            origin="https://api.openai.com",
        )
        == digest
    )


@pytest.mark.parametrize(
    ("provider", "origin"),
    (
        ("custom", "https://api.openai.com"),
        ("deepseek", "https://api.openai.com"),
        ("deepseek", "https://api.deepseek.com.evil.example"),
        ("openai-responses", "https://attacker.example"),
        ("openai-responses", "https://api.openai.com.evil.example"),
        ("openai-responses", "https://api.openai.com:443"),
        ("openai-responses", "https://api.openai.com/v1"),
    ),
)
def test_policy_rejects_provider_or_destination_substitution(provider: str, origin: str) -> None:
    with pytest.raises(LiveProviderPolicyError):
        authorize(POLICY.resolve(), provider=provider, origin=origin)


@pytest.mark.parametrize(
    "mutation",
    (
        {"extra": True},
        {"entries": []},
        {"entries": [{"provider": "openai-responses", "origins": ["https://localhost"]}]},
        {
            "entries": [
                {
                    "provider": "openai-responses",
                    "origins": [
                        "https://z.example",
                        "https://a.example",
                    ],
                }
            ]
        },
    ),
)
def test_policy_schema_and_order_fail_closed(tmp_path: Path, mutation: dict[str, object]) -> None:
    payload = json.loads(POLICY.read_text(encoding="utf-8"))
    payload.update(mutation)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LiveProviderPolicyError):
        load_policy(path.resolve())


def test_policy_refuses_symlinks(tmp_path: Path) -> None:
    link = tmp_path / "policy.json"
    try:
        link.symlink_to(POLICY)
    except OSError:
        pytest.skip("Symlinks are unavailable to this test user")
    with pytest.raises(LiveProviderPolicyError):
        load_policy(link.absolute())

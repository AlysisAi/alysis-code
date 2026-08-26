from __future__ import annotations

WEB_SEARCH_POLICY_OFF = "off"
WEB_SEARCH_POLICY_AUTO = "auto"
VALID_WEB_SEARCH_POLICIES: frozenset[str] = frozenset(
    {
        WEB_SEARCH_POLICY_OFF,
        WEB_SEARCH_POLICY_AUTO,
    }
)


def normalize_web_search_policy(raw: object) -> str:
    """Normalize the legacy access switch used by existing configuration files.

    Search intent is decided by the active model from the registered tool contract.
    The former ``always`` classifier mode is accepted as a compatibility alias for
    model-led ``auto`` behavior.
    """

    value = str(raw or "").strip().lower()
    if not value or value == "always":
        return WEB_SEARCH_POLICY_AUTO
    if value in VALID_WEB_SEARCH_POLICIES:
        return value
    allowed = ", ".join(sorted(VALID_WEB_SEARCH_POLICIES))
    raise ValueError(f"web_search_policy must be one of: {allowed}")

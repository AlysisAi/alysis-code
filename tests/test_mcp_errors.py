from __future__ import annotations

from alysis_code.config import ConfigError as AppConfigError
from alysis_code.mcp.client import (
    McpClientError,
    McpClientProtocolError,
    McpClientRemoteError,
)
from alysis_code.mcp.errors import (
    McpAuthError,
    McpCancellationError,
    McpConfigError,
    McpError,
    McpNotSupportedError,
    McpOAuthTokenStoreError,
    McpPolicyError,
    McpProcessError,
    McpProtocolError,
    McpRemoteError,
    McpResourceLimitError,
    McpSerializationError,
    McpTimeoutError,
    McpTokenStoreError,
    McpTokenStoreUnavailableError,
    McpTransportError,
    is_retryable_mcp_error,
)
from alysis_code.mcp.jsonrpc import JsonRpcProtocolError
from alysis_code.mcp.oauth import (
    McpOAuthAuthRequiredError,
    McpOAuthCallbackError,
    McpOAuthConfigError,
    McpOAuthDiscoveryError,
    McpOAuthError,
    McpOAuthInsufficientScopeError,
    McpOAuthReLoginRequired,
    McpOAuthTokenExchangeError,
)
from alysis_code.mcp.oauth_store import (
    McpOAuthTokenStoreError as FacadeMcpOAuthTokenStoreError,
)
from alysis_code.mcp.prompts import McpPromptNormalizationError
from alysis_code.mcp.resources import McpResourceNormalizationError
from alysis_code.mcp.server_requests import (
    McpInvalidServerRequestParamsError,
    McpServerRequestHandlerError,
    McpUnsupportedServerRequestError,
)
from alysis_code.mcp.transport_http import (
    McpHttpTransportAuthRequiredError,
    McpHttpTransportError,
    McpHttpTransportProtocolError,
    McpHttpTransportRemoteError,
    McpHttpTransportSessionExpiredError,
    McpHttpTransportTimeoutError,
)
from alysis_code.mcp.transport_stdio import (
    McpStdioTransportError,
    McpStdioTransportProtocolError,
    McpStdioTransportTimeoutError,
)


def test_mcp_error_root_carries_context_without_rewriting_message() -> None:
    cause = RuntimeError("native failure")
    error = McpError("safe message", server_id="alpha", method="tools/list", cause=cause)

    assert str(error) == "safe message"
    assert error.server_id == "alpha"
    assert error.method == "tools/list"
    assert error.cause is cause
    assert error.error_code == "mcp_error"


def test_mcp_config_error_is_app_config_error_with_mcp_context() -> None:
    error = McpConfigError("bad", server_id="alpha", method="mcp/config")

    assert issubclass(McpConfigError, McpError)
    assert issubclass(McpConfigError, AppConfigError)
    assert isinstance(error, AppConfigError)
    assert str(error) == "bad"
    assert error.server_id == "alpha"
    assert error.method == "mcp/config"
    assert error.cause is None
    assert error.error_code == "mcp_config_error"
    assert McpPolicyError("blocked").error_code == "mcp_policy_error"


def test_token_store_errors_are_auth_mcp_errors_and_facade_compatible() -> None:
    assert issubclass(McpTokenStoreError, McpAuthError)
    assert issubclass(McpOAuthTokenStoreError, McpTokenStoreError)
    assert issubclass(McpOAuthTokenStoreError, McpError)
    assert FacadeMcpOAuthTokenStoreError is McpOAuthTokenStoreError
    assert isinstance(McpTokenStoreUnavailableError("missing key"), McpAuthError)


def test_existing_public_mcp_error_classes_share_mcp_root() -> None:
    public_classes = (
        McpClientError,
        McpClientProtocolError,
        McpClientRemoteError,
        McpHttpTransportError,
        McpHttpTransportTimeoutError,
        McpHttpTransportProtocolError,
        McpHttpTransportRemoteError,
        McpHttpTransportAuthRequiredError,
        McpHttpTransportSessionExpiredError,
        McpStdioTransportError,
        McpStdioTransportTimeoutError,
        McpStdioTransportProtocolError,
        McpOAuthError,
        McpOAuthAuthRequiredError,
        McpOAuthCallbackError,
        McpOAuthConfigError,
        McpOAuthDiscoveryError,
        McpOAuthInsufficientScopeError,
        McpOAuthReLoginRequired,
        McpOAuthTokenExchangeError,
        JsonRpcProtocolError,
        McpPromptNormalizationError,
        McpResourceNormalizationError,
        McpServerRequestHandlerError,
        McpUnsupportedServerRequestError,
        McpInvalidServerRequestParamsError,
        McpSerializationError,
        McpPolicyError,
        McpCancellationError,
        McpResourceLimitError,
    )

    for error_cls in public_classes:
        assert issubclass(error_cls, McpError), error_cls


def test_mcp_error_categories_for_existing_public_classes() -> None:
    assert issubclass(McpHttpTransportTimeoutError, McpTimeoutError)
    assert issubclass(McpStdioTransportTimeoutError, McpTimeoutError)
    assert issubclass(McpStdioTransportError, McpProcessError)
    assert issubclass(McpHttpTransportAuthRequiredError, McpAuthError)
    assert issubclass(McpOAuthError, McpAuthError)
    assert issubclass(McpOAuthAuthRequiredError, McpAuthError)
    assert issubclass(McpOAuthReLoginRequired, McpAuthError)
    assert issubclass(McpOAuthInsufficientScopeError, McpAuthError)
    assert issubclass(McpOAuthCallbackError, McpAuthError)
    assert issubclass(McpOAuthConfigError, McpConfigError)
    assert issubclass(JsonRpcProtocolError, McpProtocolError)
    assert issubclass(McpPromptNormalizationError, McpProtocolError)
    assert issubclass(McpResourceNormalizationError, McpProtocolError)
    assert issubclass(McpClientProtocolError, McpProtocolError)
    assert issubclass(McpClientRemoteError, McpRemoteError)
    assert issubclass(McpHttpTransportProtocolError, McpProtocolError)
    assert issubclass(McpHttpTransportRemoteError, McpRemoteError)
    assert issubclass(McpStdioTransportProtocolError, McpProtocolError)
    assert issubclass(McpUnsupportedServerRequestError, McpNotSupportedError)


def test_mcp_normalization_errors_keep_value_error_compatibility_and_context() -> None:
    prompt_error = McpPromptNormalizationError(
        "bad prompt", server_id="alpha", method="prompts/list"
    )
    resource_error = McpResourceNormalizationError(
        "bad resource", server_id="beta", method="resources/read"
    )

    assert isinstance(prompt_error, ValueError)
    assert isinstance(prompt_error, McpError)
    assert prompt_error.server_id == "alpha"
    assert prompt_error.method == "prompts/list"
    assert prompt_error.cause is None
    assert prompt_error.error_code == "mcp_protocol_error"
    assert str(prompt_error) == "bad prompt"
    assert isinstance(resource_error, ValueError)
    assert isinstance(resource_error, McpError)
    assert resource_error.server_id == "beta"
    assert resource_error.method == "resources/read"
    assert resource_error.cause is None
    assert resource_error.error_code == "mcp_protocol_error"
    assert str(resource_error) == "bad resource"


def test_concrete_error_codes_match_specific_failure_class() -> None:
    assert (
        McpHttpTransportProtocolError("bad framing").error_code
        == "mcp_http_transport_protocol_error"
    )
    assert (
        McpHttpTransportRemoteError("remote failed").error_code == "mcp_http_transport_remote_error"
    )
    assert (
        McpHttpTransportAuthRequiredError("login required").error_code
        == "mcp_http_transport_auth_required"
    )
    assert (
        McpHttpTransportSessionExpiredError("expired").error_code
        == "mcp_http_transport_session_expired"
    )
    assert McpStdioTransportTimeoutError("timeout").error_code == "mcp_stdio_transport_timeout"
    assert (
        McpStdioTransportProtocolError("bad json").error_code
        == "mcp_stdio_transport_protocol_error"
    )
    assert (
        McpOAuthConfigError("bad config", server_id="alpha").error_code == "mcp_oauth_config_error"
    )


def test_is_retryable_mcp_error_honors_category_and_side_effect_safety() -> None:
    assert is_retryable_mcp_error(RuntimeError("plain"), side_effect_free=True) is False
    assert is_retryable_mcp_error(McpTransportError("timeout"), side_effect_free=False) is False
    assert is_retryable_mcp_error(McpTransportError("timeout"), side_effect_free=True) is True
    assert is_retryable_mcp_error(McpHttpTransportTimeoutError("timeout"), side_effect_free=True)
    assert is_retryable_mcp_error(McpConfigError("bad config"), side_effect_free=True) is False
    assert is_retryable_mcp_error(McpPolicyError("blocked"), side_effect_free=True) is False
    assert is_retryable_mcp_error(McpProtocolError("bad json"), side_effect_free=True) is False
    assert is_retryable_mcp_error(McpAuthError("login required"), side_effect_free=True) is False
    assert is_retryable_mcp_error(McpCancellationError("cancelled"), side_effect_free=True) is False
    assert (
        is_retryable_mcp_error(McpResourceLimitError("too large"), side_effect_free=True) is False
    )
    assert (
        is_retryable_mcp_error(McpNotSupportedError("unsupported"), side_effect_free=True) is False
    )
    assert (
        is_retryable_mcp_error(
            McpHttpTransportSessionExpiredError("expired"), side_effect_free=True
        )
        is False
    )

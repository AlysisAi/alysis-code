from __future__ import annotations

from ..config import ConfigError as AppConfigError


class McpError(RuntimeError):
    error_code = "mcp_error"
    retryable = False

    def __init__(
        self,
        message: str = "",
        *,
        server_id: str | None = None,
        method: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.server_id = server_id
        self.method = method
        self.cause = cause


class McpConfigError(McpError, AppConfigError):
    error_code = "mcp_config_error"


class McpPolicyError(McpError):
    error_code = "mcp_policy_error"


class McpTransportError(McpError):
    error_code = "mcp_transport_error"
    retryable = True


class McpTimeoutError(McpTransportError):
    error_code = "mcp_timeout_error"


class McpProcessError(McpTransportError):
    error_code = "mcp_process_error"


class McpProtocolError(McpError):
    error_code = "mcp_protocol_error"


class McpSerializationError(McpProtocolError):
    error_code = "mcp_serialization_error"


class McpRemoteError(McpError):
    error_code = "mcp_remote_error"


class McpAuthError(McpError):
    error_code = "mcp_auth_error"


class McpTokenStoreError(McpAuthError):
    error_code = "mcp_token_store_error"


class McpOAuthTokenStoreError(McpTokenStoreError):
    error_code = "mcp_oauth_token_store_error"


class McpTokenStoreUnavailableError(McpOAuthTokenStoreError):
    error_code = "mcp_token_store_unavailable"


class McpTokenStoreCorruptError(McpOAuthTokenStoreError):
    error_code = "mcp_token_store_corrupt"


class McpTokenStoreMigrationError(McpOAuthTokenStoreError):
    error_code = "mcp_token_store_migration"


class McpTokenStoreVersionError(McpOAuthTokenStoreError):
    error_code = "mcp_token_store_version"


class McpCancellationError(McpError):
    error_code = "mcp_cancellation_error"


class McpResourceLimitError(McpError):
    error_code = "mcp_resource_limit_error"


class McpNotSupportedError(McpError):
    error_code = "mcp_not_supported_error"


def is_retryable_mcp_error(error: BaseException, *, side_effect_free: bool) -> bool:
    if not side_effect_free or not isinstance(error, McpError):
        return False
    if isinstance(
        error,
        (
            McpConfigError,
            McpPolicyError,
            McpProtocolError,
            McpAuthError,
            McpCancellationError,
            McpResourceLimitError,
            McpNotSupportedError,
        ),
    ):
        return False
    return bool(getattr(error, "retryable", False))

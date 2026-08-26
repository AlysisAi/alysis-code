from collections.abc import Callable

from .settings import ServerSettings

_LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost"}


def require_token(settings: ServerSettings) -> Callable[..., None]:
    from fastapi import HTTPException, Request, status

    async def _dependency(request: Request) -> None:
        client_host = (request.client.host if request.client else "") or ""
        if settings.token:
            auth_value = request.headers.get("authorization") or ""
            if not auth_value.lower().startswith("bearer "):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Missing Bearer token.",
                )
            token = auth_value[7:].strip()
            if token != settings.token:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid token.",
                )
            return

        if client_host not in _LOCAL_CLIENTS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Server token is not set; only localhost clients are allowed.",
            )

    return _dependency

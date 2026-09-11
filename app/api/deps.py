import secrets

from fastapi import Header, HTTPException, Request, status

from app.factory import PMEngine


def get_engine(request: Request) -> PMEngine:
    engine = request.app.state.engine
    if not isinstance(engine, PMEngine):
        raise TypeError(f"app.state.engine is not a PMEngine: {type(engine)!r}")
    return engine


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """Validate ``Authorization: Bearer <token>`` against ``PM_PLATFORM_API_TOKEN``.

    Applied globally to every router (see ``app.api.main``). ``GET /health`` is the
    only unauthenticated endpoint — it is declared directly on the app, outside any
    router, so it does not inherit this dependency.

    Behaviour:
      - Server token unset → ``503`` (fail closed: the service refuses to serve an
        unprotected write surface if it was started without a token provisioned).
      - Missing / malformed / mismatched bearer token → ``401``.

    The expected token is read from the server's own environment, not the request.
    """
    from config import settings

    expected = settings.PM_PLATFORM_API_TOKEN
    if not expected:
        # The token is provisioned in the service's launchd/.env environment.
        # Refuse to operate without it rather than silently allow open access.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server authentication is not configured (PM_PLATFORM_API_TOKEN unset)",
        )

    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )

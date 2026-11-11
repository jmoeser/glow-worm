import contextvars
import logging
from datetime import UTC, datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from app.audit import AuditActor, reset_current_actor, set_current_actor
from app.auth import hash_api_key
from app.database import SessionLocal
from app.models import ApiKey, User

logger = logging.getLogger(__name__)

# Contextvar to propagate the authenticated user to sub-applications (e.g. MCP).
_current_user_ctx: contextvars.ContextVar[User | None] = contextvars.ContextVar(
    "current_user", default=None
)


def get_current_user_context() -> User | None:
    """Return the authenticated user for the current request context, or None."""
    return _current_user_ctx.get()


def _freeze_actor(
    user: User, auth_method: str, api_key: ApiKey | None = None
) -> AuditActor:
    """Copy identity while the auth session is still open."""
    return AuditActor(
        username=user.username,
        user_id=user.id,
        auth_method=auth_method,
        api_key_id=None if api_key is None else api_key.id,
        api_key_name=None if api_key is None else api_key.name,
    )


async def _call_with_actor(actor: AuditActor, user: User, call_next, request: Request):
    ctx_token = _current_user_ctx.set(user)
    actor_token = set_current_actor(actor)
    try:
        return await call_next(request)
    finally:
        reset_current_actor(actor_token)
        _current_user_ctx.reset(ctx_token)


class AuthenticationMiddleware(BaseHTTPMiddleware):
    PUBLIC_PATHS = ("/login",)
    PUBLIC_PREFIXES = ("/static/",)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in self.PUBLIC_PATHS or any(
            path.startswith(p) for p in self.PUBLIC_PREFIXES
        ):
            return await call_next(request)

        # Try Bearer token auth first (for API and MCP endpoints)
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            return await self._authenticate_bearer(request, call_next, token)

        # Fall back to session auth
        user_id = request.session.get("user_id")
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)

            # Verify session_version — reject stale sessions after password change
            stored_version = request.session.get("session_version", 0)
            if stored_version != user.session_version:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)

            actor = _freeze_actor(user, "session")
            request.state.user = user
            request.state.auth_method = "session"
            request.state.actor = actor
        finally:
            db.close()

        return await _call_with_actor(actor, user, call_next, request)

    async def _authenticate_bearer(self, request, call_next, token: str):
        db = SessionLocal()
        try:
            key_hash = hash_api_key(token)
            api_key = (
                db.query(ApiKey)
                .filter(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
                .first()
            )

            if not api_key:
                logger.warning(
                    "Invalid or revoked API key used from %s", request.client.host
                )
                return JSONResponse(
                    {"detail": "Invalid or revoked API key"},
                    status_code=401,
                )

            user = db.query(User).filter(User.id == api_key.user_id).first()
            if not user:
                return JSONResponse(
                    {"detail": "User not found"},
                    status_code=401,
                )

            # Copy the key name before commit expires the row.
            actor = _freeze_actor(user, "api_key", api_key)
            # Update last_used_at
            api_key.last_used_at = datetime.now(UTC)
            db.commit()

            request.state.user = user
            request.state.auth_method = "api_key"
            request.state.api_key_id = api_key.id
            request.state.actor = actor
        finally:
            db.close()

        return await _call_with_actor(actor, user, call_next, request)


def get_current_user(request: Request) -> User:
    return request.state.user

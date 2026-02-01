from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.database import SessionLocal
from app.models import User


class AuthenticationMiddleware(BaseHTTPMiddleware):
    PUBLIC_PATHS = ("/login",)
    PUBLIC_PREFIXES = ("/static/",)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in self.PUBLIC_PATHS or any(path.startswith(p) for p in self.PUBLIC_PREFIXES):
            return await call_next(request)

        user_id = request.session.get("user_id")
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            request.state.user = user
        finally:
            db.close()

        return await call_next(request)


def get_current_user(request: Request) -> User:
    return request.state.user

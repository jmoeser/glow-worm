import threading
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import verify_password
from app.database import get_db
from app.models import User
from app.templating import templates

router = APIRouter()

# In-memory login rate limiter: ip -> [timestamp, ...]
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 300  # 5-minute sliding window
_LOCKOUT_SECONDS = 900  # 15-minute lockout
_fail_times: dict[str, list[float]] = defaultdict(list)
_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _lock:
        times = _fail_times[ip]
        # Drop attempts outside the window
        times[:] = [t for t in times if now - t < _WINDOW_SECONDS]
        return len(times) >= _MAX_ATTEMPTS


def _record_failure(ip: str) -> None:
    now = time.monotonic()
    with _lock:
        _fail_times[ip].append(now)


def _clear_failures(ip: str) -> None:
    with _lock:
        _fail_times.pop(ip, None)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user_id = request.session.get("user_id")
    if user_id:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, db: Session = Depends(get_db)):
    ip = _client_ip(request)

    if _is_rate_limited(ip):
        return HTMLResponse(
            '<p class="text-red-600 text-sm mb-4">Too many failed attempts. Please try again later.</p>'
        )

    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")

    if not username or not password:
        _record_failure(ip)
        return HTMLResponse(
            '<p class="text-red-600 text-sm mb-4">Invalid credentials.</p>'
        )

    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        _record_failure(ip)
        return HTMLResponse(
            '<p class="text-red-600 text-sm mb-4">Invalid credentials.</p>'
        )

    _clear_failures(ip)
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["session_version"] = user.session_version
    response = HTMLResponse("")
    response.headers["HX-Redirect"] = "/"
    return response


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    if request.headers.get("hx-request"):
        response = HTMLResponse("")
        response.headers["HX-Redirect"] = "/login"
        return response
    return RedirectResponse(url="/login", status_code=303)

import os
import re
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette_csrf import CSRFMiddleware

from app.middleware import AuthenticationMiddleware
from app.routes.api_keys import router as api_keys_router
from app.routes.auth import router as auth_router
from app.routes.bills import router as bills_router
from app.routes.budgets import router as budgets_router
from app.routes.dashboard import router as dashboard_router
from app.routes.income import router as income_router
from app.routes.sinking_funds import router as sinking_funds_router
from app.routes.transactions import router as transactions_router
from app.routes.users import router as users_router

load_dotenv()

_INSECURE_DEFAULT = "change-me-to-a-random-string-of-at-least-32-characters"
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY or SECRET_KEY == _INSECURE_DEFAULT:
    raise RuntimeError(
        "SECRET_KEY is not set or is using the insecure default value. "
        "Generate a random secret and add it to your .env file. Example:\n"
        "  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )

SECURE_COOKIES = os.getenv("SECURE_COOKIES", "true").lower() == "true"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.scheduler import start_scheduler, stop_scheduler

    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Glow-worm", lifespan=lifespan)

# Middleware is added in reverse execution order.
# The last added middleware runs outermost (first on request, last on response).

# 1. Auth middleware — runs closest to the route handler
app.add_middleware(AuthenticationMiddleware)

# 2. CSRF middleware — validates tokens on unsafe methods
#    Exempt /login, /api/* (Bearer-token auth), and /mcp/* (MCP protocol)
app.add_middleware(
    CSRFMiddleware,
    secret=SECRET_KEY,
    exempt_urls=[
        re.compile(r"^/login$"),
        re.compile(r"^/logout$"),
        re.compile(r"^/api/"),
        re.compile(r"^/mcp"),
    ],
)

# 3. Session middleware — manages session cookies
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=604800,  # 7 days
    https_only=SECURE_COOKIES,
    same_site="lax",
)

# 4. CORS middleware — runs outermost (only active when ALLOWED_ORIGINS is set)
if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-CSRFToken"],
    )

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(bills_router)
app.include_router(budgets_router)
app.include_router(income_router)
app.include_router(sinking_funds_router)
app.include_router(transactions_router)
app.include_router(users_router)
app.include_router(api_keys_router)

# Mount MCP server at /mcp using SSE transport
from app.mcp_server import mcp  # noqa: E402

mcp_app = mcp.http_app(transport="sse")
app.mount("/mcp", mcp_app)

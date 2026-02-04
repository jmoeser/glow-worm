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
from app.routes.auth import router as auth_router
from app.routes.bills import router as bills_router
from app.routes.budgets import router as budgets_router
from app.routes.dashboard import router as dashboard_router
from app.routes.income import router as income_router
from app.routes.sinking_funds import router as sinking_funds_router
from app.routes.transactions import router as transactions_router
from app.routes.users import router as users_router

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-to-a-random-string-of-at-least-32-characters")


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
app.add_middleware(
    CSRFMiddleware,
    secret=SECRET_KEY,
    exempt_urls=[re.compile(r"^/login$")],
)

# 3. Session middleware — manages session cookies
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=604800,  # 7 days
)

# 4. CORS middleware — runs outermost
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
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

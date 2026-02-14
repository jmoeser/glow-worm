import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.auth import generate_api_key, hash_api_key
from app.database import get_db
from app.middleware import get_current_user
from app.models import ApiKey
from app.schemas import ApiKeyCreate, ApiKeyCreatedResponse, ApiKeyResponse
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_KEYS_PER_USER = 5


def _all_keys(db: Session, user_id: int) -> list[ApiKey]:
    return (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id)
        .order_by(ApiKey.created_at.desc())
        .all()
    )


def _check_rate_limits(db: Session, user_id: int) -> str | None:
    """Return an error message string if rate-limited, else None."""
    active_count = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
        .count()
    )
    if active_count >= MAX_KEYS_PER_USER:
        return f"Maximum of {MAX_KEYS_PER_USER} active API keys allowed. Revoke an existing key first."

    one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    recent_count = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id, ApiKey.created_at >= one_day_ago)
        .count()
    )
    if recent_count >= 1:
        return "Only 1 API key can be created per 24-hour period."

    return None


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------


@router.get("/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request)
    keys = _all_keys(db, user.id)
    return templates.TemplateResponse(
        request,
        "api_keys.html",
        {"username": user.username, "keys": keys},
    )


@router.post("/api-keys", response_class=HTMLResponse)
async def api_keys_create(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request)

    error = _check_rate_limits(db, user.id)
    if error:
        return HTMLResponse(
            f'<p class="text-red-600 text-sm">{error}</p>'
        )

    form = await request.form()
    name = (form.get("name") or "").strip() or "default"

    plain_key = generate_api_key()
    key_hash = hash_api_key(plain_key)

    api_key = ApiKey(
        user_id=user.id,
        key_hash=key_hash,
        name=name,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    logger.info("API key created: id=%d user=%s name=%s", api_key.id, user.username, name)

    keys = _all_keys(db, user.id)
    return templates.TemplateResponse(
        request,
        "api_keys.html",
        {"keys": keys, "plain_key": plain_key, "fragment": "key_created"},
    )


@router.delete("/api-keys/{key_id}", response_class=HTMLResponse)
async def api_keys_revoke(request: Request, key_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request)

    api_key = (
        db.query(ApiKey)
        .filter(ApiKey.id == key_id, ApiKey.user_id == user.id)
        .first()
    )
    if not api_key:
        return HTMLResponse("Not found", status_code=404)

    if api_key.revoked_at is not None:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">API key already revoked.</p>',
            status_code=400,
        )

    api_key.revoked_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("API key revoked: id=%d user=%s", key_id, user.username)

    return HTMLResponse("")


# ---------------------------------------------------------------------------
# JSON API routes
# ---------------------------------------------------------------------------


@router.post("/api/keys")
async def create_api_key(
    request: Request,
    db: Session = Depends(get_db),
):
    """Generate a new API key for the authenticated user.

    Rate limits:
    - Maximum 5 active (non-revoked) keys per user.
    - Maximum 1 new key per 24-hour period.
    """
    user = get_current_user(request)

    error = _check_rate_limits(db, user.id)
    if error:
        return JSONResponse({"detail": error}, status_code=429)

    # Parse optional name from body
    try:
        body = await request.json()
        data = ApiKeyCreate(**body)
    except Exception:
        data = ApiKeyCreate()

    # Generate and store key
    plain_key = generate_api_key()
    key_hash = hash_api_key(plain_key)

    api_key = ApiKey(
        user_id=user.id,
        key_hash=key_hash,
        name=data.name,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    logger.info("API key created: id=%d user=%s name=%s", api_key.id, user.username, data.name)

    response = ApiKeyCreatedResponse(
        key=plain_key,
        api_key=ApiKeyResponse.model_validate(api_key),
    )
    return JSONResponse(response.model_dump(mode="json"), status_code=201)


@router.get("/api/keys")
async def list_api_keys(
    request: Request,
    db: Session = Depends(get_db),
):
    """List all API keys for the authenticated user (active and revoked)."""
    user = get_current_user(request)
    keys = _all_keys(db, user.id)
    return [ApiKeyResponse.model_validate(k).model_dump(mode="json") for k in keys]


@router.delete("/api/keys/{key_id}")
async def revoke_api_key(
    request: Request,
    key_id: int,
    db: Session = Depends(get_db),
):
    """Revoke an API key. Only the owning user can revoke their keys."""
    user = get_current_user(request)

    api_key = (
        db.query(ApiKey)
        .filter(ApiKey.id == key_id, ApiKey.user_id == user.id)
        .first()
    )
    if not api_key:
        return JSONResponse({"detail": "API key not found"}, status_code=404)

    if api_key.revoked_at is not None:
        return JSONResponse({"detail": "API key already revoked"}, status_code=400)

    api_key.revoked_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("API key revoked: id=%d user=%s", key_id, user.username)

    return JSONResponse({"detail": "API key revoked"}, status_code=200)

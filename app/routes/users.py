from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.database import get_db
from app.middleware import get_current_user
from app.models import User
from app.schemas import UserCreate, UserResponse, UserUpdate
from app.templating import templates

router = APIRouter()


def _all_users(db: Session) -> list[User]:
    return db.query(User).order_by(User.username).all()


def _render_table_body(request: Request, db: Session) -> str:
    users = _all_users(db)
    current_user = get_current_user(request)
    return templates.TemplateResponse(
        request,
        "users.html",
        {"users": users, "current_user_id": current_user.id, "fragment": "table_body"},
    ).body.decode()


def _render_user_row(request: Request, user: User) -> str:
    current_user = get_current_user(request)
    return templates.TemplateResponse(
        request,
        "users.html",
        {"user": user, "current_user_id": current_user.id, "fragment": "user_row"},
    ).body.decode()


def _render_edit_row(request: Request, user: User) -> str:
    return templates.TemplateResponse(
        request,
        "users.html",
        {"user": user, "fragment": "edit_row"},
    ).body.decode()


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request)
    users = _all_users(db)
    return templates.TemplateResponse(
        request,
        "users.html",
        {"username": user.username, "users": users, "current_user_id": user.id},
    )


@router.post("/users", response_class=HTMLResponse)
async def users_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    email = (form.get("email") or "").strip() or None

    if not username:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Username is required.</p>'
        )

    if len(password) < 8:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Password must be at least 8 characters.</p>'
        )

    existing = db.query(User).filter(User.username == username).first()
    if existing:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Username already exists.</p>'
        )

    user = User(
        username=username,
        password_hash=hash_password(password),
        email=email,
    )
    db.add(user)
    db.commit()

    return HTMLResponse(_render_table_body(request, db))


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def users_edit_form(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(_render_edit_row(request, user))


@router.post("/users/{user_id}", response_class=HTMLResponse)
async def users_update(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return HTMLResponse("Not found", status_code=404)

    form = await request.form()

    username = (form.get("username") or "").strip()
    if username and username != user.username:
        existing = db.query(User).filter(User.username == username, User.id != user_id).first()
        if existing:
            return HTMLResponse(
                '<p class="text-red-600 text-sm">Username already exists.</p>'
            )
        user.username = username

    email = form.get("email")
    if email is not None:
        user.email = email.strip() or None

    password = form.get("password") or ""
    if password:
        if len(password) < 8:
            return HTMLResponse(
                '<p class="text-red-600 text-sm">Password must be at least 8 characters.</p>'
            )
        user.password_hash = hash_password(password)
        user.session_version += 1

    db.commit()
    db.refresh(user)

    return HTMLResponse(_render_user_row(request, user))


@router.delete("/users/{user_id}", response_class=HTMLResponse)
async def users_delete(request: Request, user_id: int, db: Session = Depends(get_db)):
    current_user = get_current_user(request)
    if current_user.id == user_id:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Cannot delete your own account.</p>',
            status_code=400,
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return HTMLResponse("Not found", status_code=404)
    db.delete(user)
    db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# JSON API routes
# ---------------------------------------------------------------------------


@router.get("/api/users")
async def api_list_users(request: Request, db: Session = Depends(get_db)):
    users = _all_users(db)
    return [UserResponse.model_validate(u).model_dump(mode="json") for u in users]


@router.post("/api/users")
async def api_create_user(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        data = UserCreate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    existing = db.query(User).filter(User.username == data.username).first()
    if existing:
        return JSONResponse(
            {"detail": "Username already exists."},
            status_code=409,
        )

    user = User(
        username=data.username,
        password_hash=hash_password(data.password),
        email=data.email,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    response = UserResponse.model_validate(user)
    return JSONResponse(response.model_dump(mode="json"), status_code=201)


@router.get("/api/users/{user_id}")
async def api_get_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse({"detail": "User not found"}, status_code=404)
    response = UserResponse.model_validate(user)
    return JSONResponse(response.model_dump(mode="json"), status_code=200)


@router.put("/api/users/{user_id}")
async def api_update_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse({"detail": "User not found"}, status_code=404)

    try:
        body = await request.json()
        data = UserUpdate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    updates = data.model_dump(exclude_unset=True)

    if "username" in updates and updates["username"] is not None:
        existing = db.query(User).filter(
            User.username == updates["username"], User.id != user_id
        ).first()
        if existing:
            return JSONResponse(
                {"detail": "Username already exists."},
                status_code=409,
            )
        user.username = updates["username"]

    if "email" in updates:
        user.email = updates["email"]

    if "password" in updates and updates["password"] is not None:
        user.password_hash = hash_password(updates["password"])
        user.session_version += 1

    db.commit()
    db.refresh(user)

    response = UserResponse.model_validate(user)
    return JSONResponse(response.model_dump(mode="json"), status_code=200)


@router.delete("/api/users/{user_id}")
async def api_delete_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    current_user = get_current_user(request)
    if current_user.id == user_id:
        return JSONResponse(
            {"detail": "Cannot delete your own account."},
            status_code=400,
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse({"detail": "User not found"}, status_code=404)
    db.delete(user)
    db.commit()
    return JSONResponse({"detail": "User deleted"}, status_code=200)

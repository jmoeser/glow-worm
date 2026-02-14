from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import verify_password
from app.database import get_db
from app.models import User
from app.templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user_id = request.session.get("user_id")
    if user_id:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "")

    if not username or not password:
        return HTMLResponse(
            '<p class="text-red-600 text-sm mb-4">Invalid credentials.</p>'
        )

    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return HTMLResponse(
            '<p class="text-red-600 text-sm mb-4">Invalid credentials.</p>'
        )

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

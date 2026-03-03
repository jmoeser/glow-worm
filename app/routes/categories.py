import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware import get_current_user
from app.models import Category
from app.schemas import CategoryCreate, CategoryResponse, CategoryUpdate
from app.templating import templates

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

router = APIRouter()


def _active_categories(db: Session):
    return (
        db.query(Category)
        .filter(Category.is_deleted == False)  # noqa: E712
        .order_by(Category.name)
        .all()
    )


def _render_table_body(request: Request, db: Session) -> str:
    categories = _active_categories(db)
    return bytes(
        templates.TemplateResponse(
            request,
            "categories.html",
            {"categories": categories, "fragment": "table_body"},
        ).body
    ).decode()


def _render_category_row(request: Request, category: Category) -> str:
    return bytes(
        templates.TemplateResponse(
            request,
            "categories.html",
            {"cat": category, "fragment": "category_row"},
        ).body
    ).decode()


def _render_edit_row(request: Request, category: Category) -> str:
    return bytes(
        templates.TemplateResponse(
            request,
            "categories.html",
            {"cat": category, "fragment": "edit_row"},
        ).body
    ).decode()


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------


@router.get("/categories", response_class=HTMLResponse)
async def categories_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request)
    categories = _active_categories(db)
    return templates.TemplateResponse(
        request,
        "categories.html",
        {"username": user.username, "categories": categories},
    )


@router.post("/categories", response_class=HTMLResponse)
async def categories_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    name = str(form.get("name") or "").strip()
    cat_type = str(form.get("type") or "").strip()
    color = str(form.get("color") or "").strip()
    is_budget_category = form.get("is_budget_category") in ("on", "true", "1", True)

    if not name:
        return HTMLResponse('<p class="text-red-600 text-sm">Name is required.</p>')

    if cat_type not in ("income", "expense", "transfer"):
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Type must be income, expense, or transfer.</p>'
        )

    if not color or not _COLOR_RE.match(color):
        return HTMLResponse(
            '<p class="text-red-600 text-sm">A valid hex color (#RRGGBB) is required.</p>'
        )

    category = Category(
        name=name,
        type=cat_type,
        color=color,
        is_budget_category=is_budget_category,
    )
    db.add(category)
    db.commit()

    return HTMLResponse(_render_table_body(request, db))


@router.get("/categories/{cat_id}/edit", response_class=HTMLResponse)
async def categories_edit_form(
    request: Request, cat_id: int, db: Session = Depends(get_db)
):
    category = db.query(Category).filter(Category.id == cat_id).first()
    if not category:
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(_render_edit_row(request, category))


@router.post("/categories/{cat_id}", response_class=HTMLResponse)
async def categories_update(
    request: Request, cat_id: int, db: Session = Depends(get_db)
):
    category = db.query(Category).filter(Category.id == cat_id).first()
    if not category:
        return HTMLResponse("Not found", status_code=404)

    form = await request.form()

    name = str(form.get("name") or "").strip()
    cat_type = str(form.get("type") or "").strip()
    color = str(form.get("color") or "").strip()
    is_budget_category = form.get("is_budget_category") in ("on", "true", "1", True)

    if name:
        category.name = name
    if cat_type in ("income", "expense", "transfer"):
        category.type = cat_type
    if color and _COLOR_RE.match(color):
        category.color = color
    category.is_budget_category = is_budget_category

    db.commit()
    db.refresh(category)

    return HTMLResponse(_render_category_row(request, category))


@router.delete("/categories/{cat_id}", response_class=HTMLResponse)
async def categories_delete(
    request: Request, cat_id: int, db: Session = Depends(get_db)
):
    category = db.query(Category).filter(Category.id == cat_id).first()
    if not category:
        return HTMLResponse("Not found", status_code=404)
    if category.is_system:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">System categories cannot be deleted.</p>',
            status_code=400,
        )
    category.is_deleted = True
    db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# JSON API routes
# ---------------------------------------------------------------------------


@router.get("/api/categories")
async def api_list_categories(request: Request, db: Session = Depends(get_db)):
    categories = _active_categories(db)
    return [
        CategoryResponse.model_validate(c).model_dump(mode="json") for c in categories
    ]


@router.post("/api/categories")
async def api_create_category(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        data = CategoryCreate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    category = Category(
        name=data.name,
        type=data.type.value,
        color=data.color,
        is_budget_category=data.is_budget_category,
    )
    db.add(category)
    db.commit()
    db.refresh(category)

    response = CategoryResponse.model_validate(category)
    return JSONResponse(response.model_dump(mode="json"), status_code=201)


@router.get("/api/categories/{cat_id}")
async def api_get_category(
    request: Request, cat_id: int, db: Session = Depends(get_db)
):
    category = db.query(Category).filter(Category.id == cat_id).first()
    if not category:
        return JSONResponse({"detail": "Category not found"}, status_code=404)
    response = CategoryResponse.model_validate(category)
    return JSONResponse(response.model_dump(mode="json"), status_code=200)


@router.put("/api/categories/{cat_id}")
async def api_update_category(
    request: Request, cat_id: int, db: Session = Depends(get_db)
):
    category = db.query(Category).filter(Category.id == cat_id).first()
    if not category:
        return JSONResponse({"detail": "Category not found"}, status_code=404)

    try:
        body = await request.json()
        data = CategoryUpdate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    for field, value in data.model_dump(exclude_unset=True).items():
        if field == "type" and value is not None:
            setattr(category, field, value.value if hasattr(value, "value") else value)
        else:
            setattr(category, field, value)

    db.commit()
    db.refresh(category)

    response = CategoryResponse.model_validate(category)
    return JSONResponse(response.model_dump(mode="json"), status_code=200)


@router.delete("/api/categories/{cat_id}")
async def api_delete_category(
    request: Request, cat_id: int, db: Session = Depends(get_db)
):
    category = db.query(Category).filter(Category.id == cat_id).first()
    if not category:
        return JSONResponse({"detail": "Category not found"}, status_code=404)
    if category.is_system:
        return JSONResponse(
            {"detail": "System categories cannot be deleted."}, status_code=400
        )
    category.is_deleted = True
    db.commit()
    return JSONResponse({"detail": "Category deleted"}, status_code=200)

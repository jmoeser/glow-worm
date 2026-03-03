import pytest

from app.models import Category


@pytest.fixture
def sample_categories(db_session):
    cats = [
        Category(
            name="Groceries", type="expense", color="#22C55E", is_budget_category=True
        ),
        Category(
            name="Salary", type="income", color="#3B82F6", is_budget_category=False
        ),
    ]
    db_session.add_all(cats)
    db_session.commit()
    for c in cats:
        db_session.refresh(c)
    return cats


@pytest.fixture
def system_category(db_session):
    cat = Category(
        name="Transfer",
        type="transfer",
        color="#6B7280",
        is_budget_category=False,
        is_system=True,
    )
    db_session.add(cat)
    db_session.commit()
    db_session.refresh(cat)
    return cat


class TestCategoriesPageGet:
    def test_renders_page(self, authed_client):
        response = authed_client.get("/categories")
        assert response.status_code == 200
        assert "Categories" in response.text

    def test_lists_categories(self, authed_client, sample_categories):
        response = authed_client.get("/categories")
        assert response.status_code == 200
        assert "Groceries" in response.text
        assert "Salary" in response.text

    def test_excludes_deleted_categories(
        self, authed_client, db_session, sample_categories
    ):
        sample_categories[0].is_deleted = True
        db_session.commit()
        response = authed_client.get("/categories")
        assert f"cat-row-{sample_categories[0].id}" not in response.text
        assert f"cat-row-{sample_categories[1].id}" in response.text

    def test_shows_add_form(self, authed_client):
        response = authed_client.get("/categories")
        assert "Add New Category" in response.text
        assert 'name="name"' in response.text
        assert 'name="type"' in response.text
        assert 'name="color"' in response.text

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/categories", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestCategoriesPagePost:
    def test_creates_category(self, authed_client, db_session):
        response = authed_client.post(
            "/categories",
            data={"name": "Entertainment", "type": "expense", "color": "#FF5733"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        cat = (
            db_session.query(Category).filter(Category.name == "Entertainment").first()
        )
        assert cat is not None
        assert cat.type == "expense"
        assert cat.color == "#FF5733"
        assert cat.is_budget_category is False

    def test_creates_budget_category(self, authed_client, db_session):
        authed_client.post(
            "/categories",
            data={
                "name": "Housing",
                "type": "expense",
                "color": "#FF0000",
                "is_budget_category": "on",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        cat = db_session.query(Category).filter(Category.name == "Housing").first()
        assert cat is not None
        assert cat.is_budget_category is True

    def test_returns_updated_table_body(self, authed_client, sample_categories):
        response = authed_client.post(
            "/categories",
            data={"name": "Transport", "type": "expense", "color": "#00AAFF"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Transport" in response.text
        assert "Groceries" in response.text

    def test_error_on_missing_name(self, authed_client):
        response = authed_client.post(
            "/categories",
            data={"name": "", "type": "expense", "color": "#FF0000"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "required" in response.text.lower()

    def test_error_on_invalid_type(self, authed_client):
        response = authed_client.post(
            "/categories",
            data={"name": "Test", "type": "invalid", "color": "#FF0000"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "income" in response.text.lower()
        assert "expense" in response.text.lower()
        assert "transfer" in response.text.lower()

    def test_error_on_missing_color(self, authed_client):
        response = authed_client.post(
            "/categories",
            data={"name": "Test", "type": "expense", "color": ""},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "required" in response.text.lower()

    def test_error_on_invalid_color(self, authed_client):
        response = authed_client.post(
            "/categories",
            data={"name": "Test", "type": "expense", "color": "notacolor"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "hex" in response.text.lower() or "required" in response.text.lower()

    def test_403_without_csrf(self, authed_client):
        response = authed_client.post(
            "/categories",
            data={"name": "Test", "type": "expense", "color": "#FF0000"},
        )
        assert response.status_code == 403


class TestCategoriesEdit:
    def test_returns_edit_row(self, authed_client, sample_categories):
        cat = sample_categories[0]
        response = authed_client.get(f"/categories/{cat.id}/edit")
        assert response.status_code == 200
        assert f'value="{cat.name}"' in response.text
        assert 'name="type"' in response.text
        assert 'name="color"' in response.text

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.get("/categories/99999/edit")
        assert response.status_code == 404

    def test_update_saves_and_returns_display_row(
        self, authed_client, db_session, sample_categories
    ):
        cat = sample_categories[0]
        response = authed_client.post(
            f"/categories/{cat.id}",
            data={"name": "Food", "type": "expense", "color": "#AABBCC"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Food" in response.text
        db_session.refresh(cat)
        assert cat.name == "Food"
        assert cat.color == "#AABBCC"

    def test_update_is_budget_category(
        self, authed_client, db_session, sample_categories
    ):
        cat = sample_categories[1]  # Salary, is_budget_category=False
        authed_client.post(
            f"/categories/{cat.id}",
            data={
                "name": cat.name,
                "type": cat.type,
                "color": cat.color,
                "is_budget_category": "on",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        db_session.refresh(cat)
        assert cat.is_budget_category is True

    def test_update_unsets_budget_category_when_unchecked(
        self, authed_client, db_session, sample_categories
    ):
        cat = sample_categories[0]  # Groceries, is_budget_category=True
        authed_client.post(
            f"/categories/{cat.id}",
            data={"name": cat.name, "type": cat.type, "color": cat.color},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        db_session.refresh(cat)
        assert cat.is_budget_category is False

    def test_update_404_for_nonexistent(self, authed_client):
        response = authed_client.post(
            "/categories/99999",
            data={"name": "X"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404


class TestCategoriesDelete:
    def test_soft_deletes(self, authed_client, db_session, sample_categories):
        cat = sample_categories[0]
        response = authed_client.delete(
            f"/categories/{cat.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.refresh(cat)
        assert cat.is_deleted is True

    def test_returns_empty_response(self, authed_client, sample_categories):
        cat = sample_categories[0]
        response = authed_client.delete(
            f"/categories/{cat.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.text == ""

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.delete(
            "/categories/99999",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404

    def test_403_without_csrf(self, authed_client, sample_categories):
        cat = sample_categories[0]
        response = authed_client.delete(f"/categories/{cat.id}")
        assert response.status_code == 403


class TestApiCategories:
    def test_list_returns_json(self, authed_client, sample_categories):
        response = authed_client.get("/api/categories")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2
        names = {c["name"] for c in data}
        assert "Groceries" in names
        assert "Salary" in names

    def test_list_excludes_deleted(self, authed_client, db_session, sample_categories):
        sample_categories[0].is_deleted = True
        db_session.commit()
        response = authed_client.get("/api/categories")
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "Salary"

    def test_create_returns_201(self, authed_client):
        response = authed_client.post(
            "/api/categories",
            json={"name": "Dining", "type": "expense", "color": "#FF5733"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Dining"
        assert data["type"] == "expense"
        assert data["is_deleted"] is False

    def test_create_422_on_bad_data(self, authed_client):
        response = authed_client.post(
            "/api/categories",
            json={"name": ""},
        )
        assert response.status_code == 422

    def test_create_422_on_invalid_color(self, authed_client):
        response = authed_client.post(
            "/api/categories",
            json={"name": "Test", "type": "expense", "color": "bad"},
        )
        assert response.status_code == 422

    def test_api_csrf_exempt(self, authed_client):
        """API routes are CSRF-exempt."""
        response = authed_client.post(
            "/api/categories",
            json={"name": "Test", "type": "expense", "color": "#FF0000"},
        )
        assert response.status_code == 201

    def test_get_returns_single(self, authed_client, sample_categories):
        cat = sample_categories[0]
        response = authed_client.get(f"/api/categories/{cat.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Groceries"
        assert data["id"] == cat.id

    def test_get_404_for_nonexistent(self, authed_client):
        response = authed_client.get("/api/categories/99999")
        assert response.status_code == 404

    def test_update_returns_200(self, authed_client, db_session, sample_categories):
        cat = sample_categories[0]
        response = authed_client.put(
            f"/api/categories/{cat.id}",
            json={"name": "Updated", "color": "#AABBCC"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated"
        assert data["color"] == "#AABBCC"

    def test_update_404_for_nonexistent(self, authed_client):
        response = authed_client.put(
            "/api/categories/99999",
            json={"name": "X"},
        )
        assert response.status_code == 404

    def test_update_422_on_bad_data(self, authed_client, sample_categories):
        cat = sample_categories[0]
        response = authed_client.put(
            f"/api/categories/{cat.id}",
            json={"color": "notvalid"},
        )
        assert response.status_code == 422

    def test_delete_returns_200(self, authed_client, db_session, sample_categories):
        cat = sample_categories[0]
        response = authed_client.delete(f"/api/categories/{cat.id}")
        assert response.status_code == 200
        db_session.refresh(cat)
        assert cat.is_deleted is True

    def test_delete_404_for_nonexistent(self, authed_client):
        response = authed_client.delete("/api/categories/99999")
        assert response.status_code == 404

    def test_unauthenticated_redirects(self, client):
        response = client.get("/api/categories", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_response_includes_is_system(self, authed_client, system_category):
        response = authed_client.get(f"/api/categories/{system_category.id}")
        assert response.status_code == 200
        assert response.json()["is_system"] is True

    def test_create_transfer_type(self, authed_client):
        response = authed_client.post(
            "/api/categories",
            json={"name": "Internal", "type": "transfer", "color": "#6B7280"},
        )
        assert response.status_code == 201
        assert response.json()["type"] == "transfer"

    def test_delete_system_category_returns_400(self, authed_client, system_category):
        response = authed_client.delete(f"/api/categories/{system_category.id}")
        assert response.status_code == 400
        assert "System" in response.json()["detail"]


class TestSystemCategoryProtection:
    def test_system_category_shows_lock_not_delete(
        self, authed_client, system_category
    ):
        response = authed_client.get("/categories")
        assert response.status_code == 200
        # Lock indicator present, delete button absent for system category
        assert "🔒" in response.text
        assert f'hx-delete="/categories/{system_category.id}"' not in response.text

    def test_non_system_category_shows_delete_button(
        self, authed_client, sample_categories
    ):
        cat = sample_categories[0]
        response = authed_client.get("/categories")
        assert f'hx-delete="/categories/{cat.id}"' in response.text

    def test_html_delete_system_returns_400(self, authed_client, system_category):
        response = authed_client.delete(
            f"/categories/{system_category.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 400
        assert "System" in response.text

    def test_system_category_not_soft_deleted(
        self, authed_client, db_session, system_category
    ):
        authed_client.delete(
            f"/categories/{system_category.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        db_session.refresh(system_category)
        assert system_category.is_deleted is False

    def test_create_transfer_type_via_form(self, authed_client, db_session):
        response = authed_client.post(
            "/categories",
            data={"name": "Internal Transfers", "type": "transfer", "color": "#6B7280"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        cat = (
            db_session.query(Category)
            .filter(Category.name == "Internal Transfers")
            .first()
        )
        assert cat is not None
        assert cat.type == "transfer"

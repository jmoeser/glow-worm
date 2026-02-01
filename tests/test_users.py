import json

from app.auth import verify_password
from app.models import User


class TestUsersPageGet:
    def test_renders_page_with_table(self, authed_client):
        response = authed_client.get("/users")
        assert response.status_code == 200
        assert "Users" in response.text
        assert "Username" in response.text
        assert "Email" in response.text
        assert "Actions" in response.text

    def test_lists_existing_user(self, authed_client, test_user):
        response = authed_client.get("/users")
        assert response.status_code == 200
        assert "alice" in response.text

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/users", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_no_delete_button_for_self(self, authed_client, test_user):
        response = authed_client.get("/users")
        assert response.status_code == 200
        # The logged-in user row should have Edit but not Delete
        assert "Edit" in response.text
        # There's only one user (alice), so no delete buttons should appear
        assert "Delete this user?" not in response.text


class TestUsersPageCreate:
    def test_creates_user(self, authed_client, db_session):
        response = authed_client.post(
            "/users",
            data={
                "username": "bob",
                "password": "Password123!",
                "email": "bob@example.com",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "bob" in response.text

        user = db_session.query(User).filter(User.username == "bob").first()
        assert user is not None
        assert user.email == "bob@example.com"

    def test_creates_user_without_email(self, authed_client, db_session):
        response = authed_client.post(
            "/users",
            data={"username": "carol", "password": "Password123!"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "carol" in response.text

    def test_rejects_empty_username(self, authed_client):
        response = authed_client.post(
            "/users",
            data={"username": "", "password": "Password123!"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Username is required" in response.text

    def test_rejects_short_password(self, authed_client):
        response = authed_client.post(
            "/users",
            data={"username": "dave", "password": "short"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "at least 8 characters" in response.text

    def test_rejects_duplicate_username(self, authed_client, test_user):
        response = authed_client.post(
            "/users",
            data={"username": "alice", "password": "Password123!"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "already exists" in response.text

    def test_password_is_hashed(self, authed_client, db_session):
        authed_client.post(
            "/users",
            data={"username": "eve", "password": "MySecret99"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        user = db_session.query(User).filter(User.username == "eve").first()
        assert user is not None
        assert user.password_hash != "MySecret99"
        assert verify_password("MySecret99", user.password_hash)


class TestUsersPageEditForm:
    def test_returns_edit_row(self, authed_client, test_user):
        response = authed_client.get(f"/users/{test_user.id}/edit")
        assert response.status_code == 200
        assert 'name="username"' in response.text
        assert 'name="password"' in response.text
        assert "alice" in response.text

    def test_not_found(self, authed_client):
        response = authed_client.get("/users/99999/edit")
        assert response.status_code == 404


class TestUsersPageUpdate:
    def test_updates_username(self, authed_client, test_user, db_session):
        response = authed_client.post(
            f"/users/{test_user.id}",
            data={"username": "alice_updated", "email": "alice@example.com"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "alice_updated" in response.text

        db_session.refresh(test_user)
        assert test_user.username == "alice_updated"

    def test_updates_email(self, authed_client, test_user, db_session):
        response = authed_client.post(
            f"/users/{test_user.id}",
            data={"username": "alice", "email": "newemail@example.com"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200

        db_session.refresh(test_user)
        assert test_user.email == "newemail@example.com"

    def test_updates_password(self, authed_client, test_user, db_session):
        response = authed_client.post(
            f"/users/{test_user.id}",
            data={"username": "alice", "password": "NewPassword456!"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200

        db_session.refresh(test_user)
        assert verify_password("NewPassword456!", test_user.password_hash)

    def test_blank_password_keeps_existing(self, authed_client, test_user, db_session):
        old_hash = test_user.password_hash
        authed_client.post(
            f"/users/{test_user.id}",
            data={"username": "alice", "password": ""},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        db_session.refresh(test_user)
        assert test_user.password_hash == old_hash

    def test_rejects_short_password_on_update(self, authed_client, test_user):
        response = authed_client.post(
            f"/users/{test_user.id}",
            data={"username": "alice", "password": "short"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "at least 8 characters" in response.text

    def test_rejects_duplicate_username_on_update(self, authed_client, test_user, db_session):
        other = User(username="bob", password_hash="x" * 60, email=None)
        db_session.add(other)
        db_session.commit()

        response = authed_client.post(
            f"/users/{test_user.id}",
            data={"username": "bob"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "already exists" in response.text

    def test_not_found(self, authed_client):
        response = authed_client.post(
            "/users/99999",
            data={"username": "ghost"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404


class TestUsersPageDelete:
    def test_deletes_other_user(self, authed_client, test_user, db_session):
        other = User(username="bob", password_hash="x" * 60, email=None)
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)

        response = authed_client.delete(
            f"/users/{other.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert db_session.query(User).filter(User.id == other.id).first() is None

    def test_cannot_delete_self(self, authed_client, test_user):
        response = authed_client.delete(
            f"/users/{test_user.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 400
        assert "Cannot delete your own account" in response.text

    def test_not_found(self, authed_client):
        response = authed_client.delete(
            "/users/99999",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404


class TestApiUsers:
    def test_list_users(self, authed_client, test_user):
        response = authed_client.get("/api/users")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert any(u["username"] == "alice" for u in data)

    def test_create_user(self, authed_client, db_session):
        response = authed_client.post(
            "/api/users",
            content=json.dumps({
                "username": "frank",
                "password": "Secure1234",
                "email": "frank@example.com",
            }),
            headers={
                "Content-Type": "application/json",
                "x-csrftoken": authed_client.csrf_token,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["username"] == "frank"
        assert data["email"] == "frank@example.com"
        assert "password" not in data
        assert "password_hash" not in data

    def test_create_user_duplicate(self, authed_client, test_user):
        response = authed_client.post(
            "/api/users",
            content=json.dumps({"username": "alice", "password": "Secure1234"}),
            headers={
                "Content-Type": "application/json",
                "x-csrftoken": authed_client.csrf_token,
            },
        )
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    def test_create_user_validation_error(self, authed_client):
        response = authed_client.post(
            "/api/users",
            content=json.dumps({"username": "x", "password": "short"}),
            headers={
                "Content-Type": "application/json",
                "x-csrftoken": authed_client.csrf_token,
            },
        )
        assert response.status_code == 422

    def test_get_user(self, authed_client, test_user):
        response = authed_client.get(f"/api/users/{test_user.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "alice"

    def test_get_user_not_found(self, authed_client):
        response = authed_client.get("/api/users/99999")
        assert response.status_code == 404

    def test_update_user(self, authed_client, test_user, db_session):
        response = authed_client.put(
            f"/api/users/{test_user.id}",
            content=json.dumps({"username": "alice_v2"}),
            headers={
                "Content-Type": "application/json",
                "x-csrftoken": authed_client.csrf_token,
            },
        )
        assert response.status_code == 200
        assert response.json()["username"] == "alice_v2"

    def test_update_user_password(self, authed_client, test_user, db_session):
        response = authed_client.put(
            f"/api/users/{test_user.id}",
            content=json.dumps({"password": "BrandNew99!"}),
            headers={
                "Content-Type": "application/json",
                "x-csrftoken": authed_client.csrf_token,
            },
        )
        assert response.status_code == 200

        db_session.refresh(test_user)
        assert verify_password("BrandNew99!", test_user.password_hash)

    def test_update_user_duplicate_username(self, authed_client, test_user, db_session):
        other = User(username="bob", password_hash="x" * 60, email=None)
        db_session.add(other)
        db_session.commit()

        response = authed_client.put(
            f"/api/users/{test_user.id}",
            content=json.dumps({"username": "bob"}),
            headers={
                "Content-Type": "application/json",
                "x-csrftoken": authed_client.csrf_token,
            },
        )
        assert response.status_code == 409

    def test_update_user_not_found(self, authed_client):
        response = authed_client.put(
            "/api/users/99999",
            content=json.dumps({"username": "ghost"}),
            headers={
                "Content-Type": "application/json",
                "x-csrftoken": authed_client.csrf_token,
            },
        )
        assert response.status_code == 404

    def test_delete_user(self, authed_client, test_user, db_session):
        other = User(username="bob", password_hash="x" * 60, email=None)
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)

        response = authed_client.delete(
            f"/api/users/{other.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "deleted" in response.json()["detail"]

    def test_delete_self_blocked(self, authed_client, test_user):
        response = authed_client.delete(
            f"/api/users/{test_user.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 400
        assert "Cannot delete your own account" in response.json()["detail"]

    def test_delete_user_not_found(self, authed_client):
        response = authed_client.delete(
            "/api/users/99999",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/api/users", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

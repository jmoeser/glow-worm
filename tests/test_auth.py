class TestLoginPage:
    def test_renders_login_form(self, client):
        response = client.get("/login")
        assert response.status_code == 200
        assert "Sign in" in response.text
        assert 'name="username"' in response.text
        assert 'name="password"' in response.text

    def test_redirects_if_already_authenticated(self, client, test_user):
        # Log in first
        client.post(
            "/login",
            data={"username": "alice", "password": "SecurePass123!"},
        )
        # Visit login page again
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"


class TestLoginPost:
    def test_successful_login_returns_hx_redirect(self, client, test_user):
        response = client.post(
            "/login",
            data={"username": "alice", "password": "SecurePass123!"},
        )
        assert response.status_code == 200
        assert response.headers.get("hx-redirect") == "/"

    def test_invalid_password(self, client, test_user):
        response = client.post(
            "/login",
            data={"username": "alice", "password": "wrongpassword"},
        )
        assert response.status_code == 200
        assert "Invalid credentials" in response.text

    def test_nonexistent_user(self, client):
        response = client.post(
            "/login",
            data={"username": "nobody", "password": "somepassword"},
        )
        assert response.status_code == 200
        assert "Invalid credentials" in response.text

    def test_empty_fields(self, client):
        response = client.post(
            "/login",
            data={"username": "", "password": ""},
        )
        assert response.status_code == 200
        assert "Invalid credentials" in response.text

    def test_no_username_enumeration(self, client, test_user):
        """Error message is the same whether user exists or not."""
        resp_bad_pass = client.post(
            "/login",
            data={"username": "alice", "password": "wrongpassword"},
        )
        resp_no_user = client.post(
            "/login",
            data={"username": "nobody", "password": "somepassword"},
        )
        assert resp_bad_pass.text == resp_no_user.text


class TestLogout:
    def test_clears_session_and_redirects(self, client, test_user):
        # Log in
        client.post(
            "/login",
            data={"username": "alice", "password": "SecurePass123!"},
        )
        # Logout
        response = client.get("/logout", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_cannot_access_protected_after_logout(self, client, test_user):
        # Log in
        client.post(
            "/login",
            data={"username": "alice", "password": "SecurePass123!"},
        )
        # Logout
        client.get("/logout", follow_redirects=False)
        # Try to access protected route
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestProtectedRoutes:
    def test_unauthenticated_redirect(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_static_files_accessible_without_auth(self, client):
        response = client.get("/static/styles.css")
        assert response.status_code == 200

    def test_session_persistence(self, client, test_user):
        # Log in
        client.post(
            "/login",
            data={"username": "alice", "password": "SecurePass123!"},
        )
        # Access login page should redirect (session persists)
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"

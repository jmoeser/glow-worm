"""Tests for API key management endpoints."""

import time
from datetime import datetime, timedelta, timezone

from app.auth import hash_api_key
from app.models import ApiKey


class TestCreateApiKey:
    def test_create_api_key(self, authed_client, test_user):
        resp = authed_client.post(
            "/api/keys",
            json={"name": "my-key"},
            headers={"X-CSRF-Token": authed_client.csrf_token},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "key" in data
        assert data["api_key"]["name"] == "my-key"
        assert data["api_key"]["user_id"] == test_user.id
        assert data["api_key"]["revoked_at"] is None
        # The plain key should be a urlsafe base64 string
        assert len(data["key"]) > 20

    def test_create_api_key_default_name(self, authed_client):
        resp = authed_client.post(
            "/api/keys",
            json={},
            headers={"X-CSRF-Token": authed_client.csrf_token},
        )
        assert resp.status_code == 201
        assert resp.json()["api_key"]["name"] == "default"

    def test_create_api_key_no_body(self, authed_client):
        """Should work even without a JSON body (falls back to defaults)."""
        resp = authed_client.post(
            "/api/keys",
            headers={"X-CSRF-Token": authed_client.csrf_token},
        )
        assert resp.status_code == 201

    def test_max_keys_limit(self, authed_client, db_session, test_user):
        # Pre-create 5 keys directly in DB
        for i in range(5):
            db_session.add(ApiKey(
                user_id=test_user.id,
                key_hash=f"fakehash{i}",
                name=f"key-{i}",
            ))
        db_session.commit()

        resp = authed_client.post(
            "/api/keys",
            json={"name": "one-too-many"},
            headers={"X-CSRF-Token": authed_client.csrf_token},
        )
        assert resp.status_code == 429
        assert "Maximum" in resp.json()["detail"]

    def test_rate_limit_one_per_day(self, authed_client, db_session, test_user):
        # Pre-create a key created "now"
        db_session.add(ApiKey(
            user_id=test_user.id,
            key_hash="recenthash",
            name="recent",
            created_at=datetime.now(timezone.utc),
        ))
        db_session.commit()

        resp = authed_client.post(
            "/api/keys",
            json={"name": "second-today"},
            headers={"X-CSRF-Token": authed_client.csrf_token},
        )
        assert resp.status_code == 429
        assert "24-hour" in resp.json()["detail"]

    def test_revoked_keys_dont_count_toward_limit(self, authed_client, db_session, test_user):
        # Create 5 revoked keys — they shouldn't block new key creation
        for i in range(5):
            db_session.add(ApiKey(
                user_id=test_user.id,
                key_hash=f"revokedhash{i}",
                name=f"revoked-{i}",
                revoked_at=datetime.now(timezone.utc),
                # Older than 1 day so rate limit doesn't trigger
                created_at=datetime.now(timezone.utc) - timedelta(days=2),
            ))
        db_session.commit()

        resp = authed_client.post(
            "/api/keys",
            json={"name": "fresh"},
            headers={"X-CSRF-Token": authed_client.csrf_token},
        )
        assert resp.status_code == 201


class TestListApiKeys:
    def test_list_keys(self, authed_client, db_session, test_user):
        db_session.add(ApiKey(user_id=test_user.id, key_hash="hash1", name="key-1"))
        db_session.add(ApiKey(user_id=test_user.id, key_hash="hash2", name="key-2"))
        db_session.commit()

        resp = authed_client.get("/api/keys")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        names = {k["name"] for k in data}
        assert names == {"key-1", "key-2"}

    def test_list_keys_empty(self, authed_client):
        resp = authed_client.get("/api/keys")
        assert resp.status_code == 200
        assert resp.json() == []


class TestRevokeApiKey:
    def test_revoke_key(self, authed_client, db_session, test_user):
        api_key = ApiKey(user_id=test_user.id, key_hash="todel", name="del-me")
        db_session.add(api_key)
        db_session.commit()
        db_session.refresh(api_key)

        resp = authed_client.delete(
            f"/api/keys/{api_key.id}",
            headers={"X-CSRF-Token": authed_client.csrf_token},
        )
        assert resp.status_code == 200
        assert "revoked" in resp.json()["detail"].lower()

        db_session.refresh(api_key)
        assert api_key.revoked_at is not None

    def test_revoke_nonexistent(self, authed_client):
        resp = authed_client.delete(
            "/api/keys/9999",
            headers={"X-CSRF-Token": authed_client.csrf_token},
        )
        assert resp.status_code == 404

    def test_revoke_already_revoked(self, authed_client, db_session, test_user):
        api_key = ApiKey(
            user_id=test_user.id,
            key_hash="revhash",
            name="already-revoked",
            revoked_at=datetime.now(timezone.utc),
        )
        db_session.add(api_key)
        db_session.commit()
        db_session.refresh(api_key)

        resp = authed_client.delete(
            f"/api/keys/{api_key.id}",
            headers={"X-CSRF-Token": authed_client.csrf_token},
        )
        assert resp.status_code == 400
        assert "already revoked" in resp.json()["detail"].lower()


class TestApiKeysHtmlPage:
    def test_get_api_keys_page(self, authed_client):
        resp = authed_client.get("/api-keys")
        assert resp.status_code == 200
        assert "API Keys" in resp.text

    def test_get_api_keys_page_lists_keys(self, authed_client, db_session, test_user):
        db_session.add(ApiKey(user_id=test_user.id, key_hash="hash1", name="my-integration"))
        db_session.commit()

        resp = authed_client.get("/api-keys")
        assert resp.status_code == 200
        assert "my-integration" in resp.text

    def test_create_key_via_form(self, authed_client, test_user):
        resp = authed_client.post(
            "/api-keys",
            data={"name": "form-key"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert resp.status_code == 200
        # Should contain the plaintext key for copying
        assert "API key created" in resp.text
        # Should contain the key name in the updated table
        assert "form-key" in resp.text

    def test_create_key_default_name(self, authed_client, test_user):
        resp = authed_client.post(
            "/api-keys",
            data={},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert resp.status_code == 200
        assert "default" in resp.text

    def test_revoke_key_via_html(self, authed_client, db_session, test_user):
        api_key = ApiKey(user_id=test_user.id, key_hash="htmldel", name="html-revoke")
        db_session.add(api_key)
        db_session.commit()
        db_session.refresh(api_key)

        resp = authed_client.delete(
            f"/api-keys/{api_key.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert resp.status_code == 200
        assert resp.text == ""

        db_session.refresh(api_key)
        assert api_key.revoked_at is not None

    def test_rate_limit_html_error(self, authed_client, db_session, test_user):
        db_session.add(ApiKey(
            user_id=test_user.id,
            key_hash="recenthtml",
            name="recent",
            created_at=datetime.now(timezone.utc),
        ))
        db_session.commit()

        resp = authed_client.post(
            "/api-keys",
            data={"name": "too-soon"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert resp.status_code == 200
        assert "24-hour" in resp.text

    def test_max_keys_html_error(self, authed_client, db_session, test_user):
        for i in range(5):
            db_session.add(ApiKey(
                user_id=test_user.id,
                key_hash=f"htmlmax{i}",
                name=f"key-{i}",
            ))
        db_session.commit()

        resp = authed_client.post(
            "/api-keys",
            data={"name": "one-too-many"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert resp.status_code == 200
        assert "Maximum" in resp.text


class TestBearerTokenAuth:
    def test_api_access_with_bearer_token(self, client, db_session, test_user):
        """An API key should grant access to API endpoints."""
        from app.auth import generate_api_key, hash_api_key

        plain_key = generate_api_key()
        db_session.add(ApiKey(
            user_id=test_user.id,
            key_hash=hash_api_key(plain_key),
            name="test-bearer",
        ))
        db_session.commit()

        resp = client.get(
            "/api/transactions",
            headers={"Authorization": f"Bearer {plain_key}"},
        )
        assert resp.status_code == 200

    def test_invalid_bearer_token(self, client):
        resp = client.get(
            "/api/transactions",
            headers={"Authorization": "Bearer invalid-token-here"},
        )
        assert resp.status_code == 401

    def test_revoked_token_rejected(self, client, db_session, test_user):
        from app.auth import generate_api_key, hash_api_key

        plain_key = generate_api_key()
        db_session.add(ApiKey(
            user_id=test_user.id,
            key_hash=hash_api_key(plain_key),
            name="revoked-bearer",
            revoked_at=datetime.now(timezone.utc),
        ))
        db_session.commit()

        resp = client.get(
            "/api/transactions",
            headers={"Authorization": f"Bearer {plain_key}"},
        )
        assert resp.status_code == 401

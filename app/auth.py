import hashlib
import hmac
import secrets

import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(
        plain_password.encode("utf-8"), hashed_password.encode("utf-8")
    )


def generate_api_key() -> str:
    """Generate a cryptographically secure random API key."""
    return secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    """Hash an API key using SHA-256 for fast lookup.

    We use SHA-256 instead of bcrypt because API keys are high-entropy
    random tokens (not user-chosen passwords), so brute-force is not a
    practical attack vector. SHA-256 allows O(1) lookup by hash.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_api_key(plain_key: str, key_hash: str) -> bool:
    """Verify an API key against its stored hash (constant-time)."""
    computed = hashlib.sha256(plain_key.encode("utf-8")).hexdigest()
    return hmac.compare_digest(computed, key_hash)

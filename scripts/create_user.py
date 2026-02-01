"""Interactive CLI script to create the initial user."""

import getpass
import sys

from sqlalchemy import select

from app.auth import hash_password
from app.database import Base, SessionLocal, engine
from app.models import User


def create_user() -> None:
    Base.metadata.create_all(bind=engine)

    username = input("Username: ").strip()
    if not username:
        print("Error: username cannot be empty.")
        sys.exit(1)

    password = getpass.getpass("Password (min 8 characters): ")
    if len(password) < 8:
        print("Error: password must be at least 8 characters.")
        sys.exit(1)

    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Error: passwords do not match.")
        sys.exit(1)

    email = input("Email (optional, press Enter to skip): ").strip() or None

    db = SessionLocal()
    try:
        existing = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if existing:
            print(f"Error: user '{username}' already exists.")
            sys.exit(1)

        user = User(
            username=username,
            password_hash=hash_password(password),
            email=email,
        )
        db.add(user)
        db.commit()
        print(f"User '{username}' created successfully.")
    finally:
        db.close()


if __name__ == "__main__":
    create_user()

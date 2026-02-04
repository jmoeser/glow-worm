import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Override DATABASE_URL before any app imports
os.environ["DATABASE_URL"] = "sqlite:///./test-glow-worm.db"

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Budget, Category, RecurringBill, SinkingFund, Transaction, User  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test-glow-worm.db"

engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture(autouse=True)
def setup_database():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    from starlette.testclient import TestClient

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture
def test_user(db_session):
    user = User(
        username="alice",
        password_hash=hash_password("SecurePass123!"),
        email="alice@example.com",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def authed_client(client, test_user):
    # Log in
    client.post("/login", data={"username": "alice", "password": "SecurePass123!"})
    # GET a page to receive the CSRF cookie
    client.get("/income")
    # Extract CSRF token from cookie
    csrf_token = None
    for cookie in client.cookies.jar:
        if cookie.name == "csrftoken":
            csrf_token = cookie.value
            break
    client.csrf_token = csrf_token
    return client


@pytest.fixture
def sample_sinking_funds(db_session):
    funds = [
        SinkingFund(name="Bills", color="#FF0000", monthly_allocation=0, current_balance=0),
        SinkingFund(name="Savings", color="#00FF00", monthly_allocation=0, current_balance=0),
    ]
    db_session.add_all(funds)
    db_session.commit()
    for f in funds:
        db_session.refresh(f)
    return funds


@pytest.fixture
def sample_category(db_session):
    cat = Category(
        name="Bills", type="expense", color="#FF0000", is_budget_category=False
    )
    db_session.add(cat)
    db_session.commit()
    db_session.refresh(cat)
    return cat


@pytest.fixture
def sample_bills(db_session, sample_category):
    bills = [
        RecurringBill(
            name="Rent",
            amount=2400,
            debtor_provider="Landlord",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-02-01",
        ),
        RecurringBill(
            name="Internet",
            amount=89,
            debtor_provider="ISP",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-02-01",
        ),
    ]
    db_session.add_all(bills)
    db_session.commit()
    for b in bills:
        db_session.refresh(b)
    return bills


@pytest.fixture
def sample_budget_categories(db_session):
    cats = [
        Category(name="Groceries", type="expense", color="#22C55E", is_budget_category=True),
        Category(name="Transport", type="expense", color="#3B82F6", is_budget_category=True),
        Category(name="Entertainment", type="expense", color="#F59E0B", is_budget_category=True),
    ]
    db_session.add_all(cats)
    db_session.commit()
    for c in cats:
        db_session.refresh(c)
    return cats


@pytest.fixture
def sample_income_category(db_session):
    cat = Category(
        name="Salary", type="income", color="#00FF00", is_budget_category=False
    )
    db_session.add(cat)
    db_session.commit()
    db_session.refresh(cat)
    return cat


@pytest.fixture
def sample_transactions(db_session, sample_category, sample_income_category):
    txns = [
        Transaction(
            date="2026-01-15",
            description="Groceries shopping",
            amount=75.50,
            category_id=sample_category.id,
            type="expense",
            transaction_type="regular",
        ),
        Transaction(
            date="2026-01-01",
            description="Monthly income",
            amount=5000.00,
            category_id=sample_income_category.id,
            type="income",
            transaction_type="income",
        ),
    ]
    db_session.add_all(txns)
    db_session.commit()
    for t in txns:
        db_session.refresh(t)
    return txns


@pytest.fixture
def sample_budgets(db_session, sample_budget_categories):
    from datetime import datetime

    import pytz

    now = datetime.now(pytz.timezone("Australia/Brisbane"))
    month, year = now.month, now.year

    budgets = [
        Budget(
            category_id=sample_budget_categories[0].id,  # Groceries
            month=month,
            year=year,
            allocated_amount=600,
            spent_amount=150,
            fund_balance=0,
        ),
        Budget(
            category_id=sample_budget_categories[1].id,  # Transport
            month=month,
            year=year,
            allocated_amount=200,
            spent_amount=80,
            fund_balance=0,
        ),
    ]
    db_session.add_all(budgets)
    db_session.commit()
    for b in budgets:
        db_session.refresh(b)
    return budgets

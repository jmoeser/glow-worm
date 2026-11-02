"""Payment method create, edit, archive, delete, and card-number checks."""

import logging

import pytest
from sqlalchemy.orm import Session

from app.mcp_server import create_payment_method
from app.models import PaymentMethod, RecurringBill

PAN = "4111111111111111"
PAN_VARIANTS = [
    PAN,
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "4111.1111.1111.1111",
    "123-456",
]


def _post(client, url, data, *, follow=False):
    return client.post(
        url,
        data=data,
        headers={"x-csrftoken": client.csrf_token},
        follow_redirects=follow,
    )


def _bill(db: Session, category_id: int, **kwargs) -> RecurringBill:
    values = {
        "name": "Netflix",
        "amount": 15,
        "debtor_provider": "Netflix",
        "start_date": "2026-01-01",
        "frequency": "monthly",
        "category_id": category_id,
        "next_due_date": "2026-11-05",
        "is_active": True,
    }
    values.update(kwargs)
    bill = RecurringBill(**values)
    db.add(bill)
    db.commit()
    db.refresh(bill)
    return bill


def _card_form(**kwargs):
    data = {
        "nickname": "Everyday Visa",
        "kind": "card",
        "last4": "4321",
        "expiry": "2027-12",
        "owner": "Jordan",
    }
    data.update(kwargs)
    return data


class TestPaymentMethodCrud:
    def test_html_and_api_create_edit_and_strip(self, authed_client, db_session):
        created = _post(
            authed_client,
            "/payment-methods",
            _card_form(nickname="  Everyday Visa  ", owner="  Jordan  "),
        )
        assert created.status_code == 303
        method = db_session.query(PaymentMethod).one()
        assert method.nickname == "Everyday Visa"
        assert method.owner == "Jordan"
        assert method.last4 == "4321"
        assert method.expiry_month == 12
        assert method.expiry_year == 2027

        page = authed_client.get("/payment-methods")
        assert page.status_code == 200
        assert "Payment method added." in page.text
        assert "Everyday Visa ending 4321" in page.text
        assert 'id="owner-options"' in page.text
        assert 'value="Jordan"' in page.text
        assert 'value="alice"' in page.text
        assert "Only the last 4 digits. Never the full number." in page.text

        api = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Offset",
                "kind": "bank",
                "last4": "0042",
                "owner": "Jordan",
            },
        )
        assert api.status_code == 201
        body = api.json()
        assert body["last4"] == "0042"
        assert body["expiry_month"] is None

        edited = authed_client.put(
            f"/api/payment-methods/{body['id']}",
            json={"nickname": "  Joint account  "},
        )
        assert edited.status_code == 200
        assert edited.json()["nickname"] == "Joint account"
        assert edited.json()["last4"] == "0042"

    def test_archived_hidden_until_asked_and_unarchive(self, authed_client, db_session):
        made = authed_client.post(
            "/api/payment-methods",
            json={"nickname": "Old card", "kind": "manual", "owner": "Jordan"},
        )
        method_id = made.json()["id"]
        archived = authed_client.post(f"/api/payment-methods/{method_id}/archive")
        assert archived.status_code == 200
        assert archived.json()["is_archived"] is True

        hidden = authed_client.get("/api/payment-methods")
        assert hidden.json() == []
        shown = authed_client.get("/api/payment-methods?include_archived=true")
        assert len(shown.json()) == 1

        page = authed_client.get("/payment-methods")
        assert "Old card" not in page.text
        archived_page = authed_client.get("/payment-methods?archived=1")
        assert "Old card" in archived_page.text

        restored = authed_client.post(f"/api/payment-methods/{method_id}/unarchive")
        assert restored.json()["is_archived"] is False
        assert authed_client.get("/api/payment-methods").json()[0]["id"] == method_id

    def test_owner_datalist_cuts_long_username(
        self, authed_client, db_session, test_user
    ):
        test_user.username = "u" * 80
        db_session.commit()
        page = authed_client.get("/payment-methods")
        start = page.text.index('id="owner-options"')
        datalist = page.text[start : page.text.index("</datalist>", start)]
        assert "u" * 60 in datalist
        assert "u" * 80 not in datalist


class TestCardNumberRules:
    @pytest.mark.parametrize("last4", ["123", "12345", "12 34", "12ab"])
    def test_bad_last4_rejected_without_echo(self, authed_client, last4, caplog):
        caplog.set_level(logging.DEBUG)
        response = _post(
            authed_client,
            "/payment-methods",
            _card_form(last4=last4),
        )
        assert response.status_code == 422
        assert "Enter only the last 4 digits." in response.text
        assert last4 not in response.text
        assert last4 not in caplog.text

        api = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Everyday Visa",
                "kind": "card",
                "last4": last4,
                "expiry_month": 12,
                "expiry_year": 2027,
                "owner": "Jordan",
            },
        )
        assert api.status_code == 422
        assert last4 not in api.text
        assert "input" not in api.text

        mcp = create_payment_method(
            nickname="Everyday Visa",
            kind="card",
            owner="Jordan",
            last4=last4,
            expiry_month=12,
            expiry_year=2027,
        )
        assert last4 not in str(mcp)
        assert last4 not in caplog.text

    def test_last4_0042_is_kept(self, authed_client, db_session):
        response = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Bank",
                "kind": "bank",
                "last4": "0042",
                "owner": "Jordan",
            },
        )
        assert response.status_code == 201
        assert response.json()["last4"] == "0042"
        stored = db_session.query(PaymentMethod).one()
        assert stored.last4 == "0042"

    @pytest.mark.parametrize("bad", PAN_VARIANTS)
    def test_nickname_owner_and_note_reject_numbers(
        self, authed_client, db_session, bad, caplog
    ):
        caplog.set_level(logging.DEBUG)
        for field in ("nickname", "owner"):
            payload = _card_form()
            payload[field] = bad
            page = _post(authed_client, "/payment-methods", payload)
            assert page.status_code == 422
            assert "card or account numbers" in page.text
            assert bad not in page.text
            api = authed_client.post(
                "/api/payment-methods",
                json={
                    "nickname": bad if field == "nickname" else "Everyday Visa",
                    "kind": "card",
                    "last4": "4321",
                    "expiry_month": 12,
                    "expiry_year": 2027,
                    "owner": bad if field == "owner" else "Jordan",
                },
            )
            assert api.status_code == 422
            assert bad not in api.text
            mcp = create_payment_method(
                nickname=bad if field == "nickname" else "Everyday Visa",
                kind="card",
                owner=bad if field == "owner" else "Jordan",
                last4="4321",
                expiry_month=12,
                expiry_year=2027,
            )
            assert bad not in str(mcp)
            assert bad not in caplog.text

    @pytest.mark.parametrize("ok", ["11/2026", "Visa 2"])
    def test_short_digit_runs_are_allowed(self, authed_client, ok):
        response = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": ok,
                "kind": "manual",
                "owner": "Jordan",
            },
        )
        assert response.status_code == 201
        assert response.json()["nickname"] == ok


class TestKindMerge:
    def _card(self, authed_client) -> dict:
        response = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Everyday Visa",
                "kind": "card",
                "last4": "4321",
                "expiry_month": 12,
                "expiry_year": 2027,
                "owner": "Jordan",
            },
        )
        assert response.status_code == 201
        return response.json()

    def test_nickname_only_keeps_card_fields(self, authed_client):
        card = self._card(authed_client)
        updated = authed_client.put(
            f"/api/payment-methods/{card['id']}",
            json={"nickname": "Household Visa"},
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["nickname"] == "Household Visa"
        assert body["last4"] == "4321"
        assert body["expiry_month"] == 12
        assert body["expiry_year"] == 2027

    def test_card_to_bank_clears_expiry_and_manual_clears_last4(self, authed_client):
        card = self._card(authed_client)
        bank = authed_client.put(
            f"/api/payment-methods/{card['id']}",
            json={"kind": "bank"},
        )
        assert bank.status_code == 200
        assert bank.json()["kind"] == "bank"
        assert bank.json()["last4"] == "4321"
        assert bank.json()["expiry_month"] is None
        assert bank.json()["expiry_year"] is None

        manual = authed_client.put(
            f"/api/payment-methods/{card['id']}",
            json={"kind": "manual"},
        )
        assert manual.status_code == 200
        assert manual.json()["last4"] is None
        assert manual.json()["expiry_month"] is None

    def test_explicit_expiry_on_bank_is_422(self, authed_client):
        response = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Offset",
                "kind": "bank",
                "last4": "1234",
                "expiry_month": 12,
                "expiry_year": 2027,
                "owner": "Jordan",
            },
        )
        assert response.status_code == 422
        assert "Only a card has an expiry." in response.text
        assert "2027" not in response.text

    def test_explicit_last4_on_manual_is_422(self, authed_client):
        response = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "BPAY",
                "kind": "manual",
                "last4": "1234",
                "owner": "Jordan",
            },
        )
        assert response.status_code == 422
        assert "doesn't use last 4 digits" in response.text

    def test_month_input_parses_and_two_digit_year_is_rejected(
        self, authed_client, db_session
    ):
        created = _post(authed_client, "/payment-methods", _card_form(expiry="2026-10"))
        assert created.status_code == 303
        method = db_session.query(PaymentMethod).one()
        assert method.expiry_month == 10
        assert method.expiry_year == 2026

        api = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Other",
                "kind": "card",
                "last4": "9999",
                "expiry_month": 10,
                "expiry_year": 26,
                "owner": "Jordan",
            },
        )
        assert api.status_code == 422
        assert "26" not in api.text


class TestArchiveAndDelete:
    def test_archive_blocked_by_active_bill_including_past_end_date(
        self, authed_client, db_session, sample_category
    ):
        method = authed_client.post(
            "/api/payment-methods",
            json={"nickname": "Old card", "kind": "manual", "owner": "Jordan"},
        ).json()
        _bill(
            db_session,
            sample_category.id,
            name="Gym",
            payment_method_id=method["id"],
            end_date="2020-01-01",
        )
        blocked = authed_client.post(f"/api/payment-methods/{method['id']}/archive")
        assert blocked.status_code == 409
        assert "Move these bills first: Gym." in blocked.text

        page = authed_client.get("/payment-methods")
        assert "Move these bills first: Gym." in page.text

    def test_archive_allowed_when_only_inactive_bill_links(
        self, authed_client, db_session, sample_category
    ):
        method = authed_client.post(
            "/api/payment-methods",
            json={"nickname": "Old card", "kind": "manual", "owner": "Jordan"},
        ).json()
        _bill(
            db_session,
            sample_category.id,
            payment_method_id=method["id"],
            is_active=False,
        )
        archived = authed_client.post(f"/api/payment-methods/{method['id']}/archive")
        assert archived.status_code == 200
        assert archived.json()["is_archived"] is True
        blocked = authed_client.delete(f"/api/payment-methods/{method['id']}")
        assert blocked.status_code == 409
        assert "A bill still links to this." in blocked.text

    def test_delete_blocked_by_open_replacement_and_allowed_after_stop(
        self, authed_client, db_session, sample_category
    ):
        from app.models import PaymentMethodReplacement
        from app.services.payment_methods import start, stop

        old = authed_client.post(
            "/api/payment-methods",
            json={"nickname": "Old", "kind": "manual", "owner": "Jordan"},
        ).json()
        new = authed_client.post(
            "/api/payment-methods",
            json={"nickname": "New", "kind": "manual", "owner": "Jordan"},
        ).json()
        _bill(db_session, sample_category.id, payment_method_id=old["id"])
        replacement = start(db_session, old["id"], new_id=new["id"])
        db_session.commit()
        blocked = authed_client.delete(f"/api/payment-methods/{old['id']}")
        assert blocked.status_code == 409
        assert "Finish or stop replacing first." in blocked.text

        stop(db_session, replacement)
        db_session.commit()
        deleted = authed_client.delete(f"/api/payment-methods/{new['id']}")
        assert deleted.status_code == 200
        assert (
            db_session.query(PaymentMethodReplacement)
            .filter(PaymentMethodReplacement.id == replacement.id)
            .count()
            == 0
        )

"""Wallet payment methods: Apple Pay and Google Pay linked to a card."""

from datetime import date

import pytest

from app.mcp_server import (
    create_payment_method,
    get_payment_method,
    list_payment_methods,
    update_payment_method,
)
from app.models import (
    PaymentMethod,
    PaymentMethodReplacement,
    PaymentMethodReplacementItem,
)
from app.services.payment_methods import (
    PaymentMethodError,
    bill_badge,
    compute_payment_alerts,
    effective_expiry,
    expires_label,
    expiry_status,
    finish,
    method_status,
    progress,
    start,
    tick,
    tick_wallet,
)


def _post(client, url, data):
    return client.post(
        url,
        data=data,
        headers={"x-csrftoken": client.csrf_token},
        follow_redirects=False,
    )


def _card(db, nickname="Everyday Visa", last4="4321", **kwargs) -> PaymentMethod:
    values = {
        "nickname": nickname,
        "kind": "card",
        "last4": last4,
        "expiry_month": 12,
        "expiry_year": 2026,
        "owner": "Jordan",
    }
    values.update(kwargs)
    method = PaymentMethod(**values)
    db.add(method)
    db.commit()
    db.refresh(method)
    return method


def _wallet(
    db, card, provider="apple_pay", nickname="Phone", **kwargs
) -> PaymentMethod:
    values = {
        "nickname": nickname,
        "kind": "wallet",
        "owner": "Jordan",
        "wallet_provider": provider,
        "funding_method_id": card.id,
    }
    values.update(kwargs)
    method = PaymentMethod(**values)
    db.add(method)
    db.commit()
    db.refresh(method)
    return method


def _bill(db, category_id, method_id, name="Netflix", due="2026-11-05"):
    from app.models import RecurringBill

    bill = RecurringBill(
        name=name,
        amount=15,
        debtor_provider=name,
        start_date="2026-01-01",
        frequency="monthly",
        category_id=category_id,
        next_due_date=due,
        payment_method_id=method_id,
        is_active=True,
    )
    db.add(bill)
    db.commit()
    db.refresh(bill)
    return bill


class TestWalletCreate:
    def test_api_and_html_create_and_reject_bad_links(self, authed_client, db_session):
        card = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Everyday Visa",
                "kind": "card",
                "last4": "4321",
                "expiry_month": 12,
                "expiry_year": 2026,
                "owner": "Jordan",
            },
        )
        assert card.status_code == 201
        card_id = card.json()["id"]
        assert card.json()["effective_expiry_month"] == 12
        assert card.json()["wallet_provider"] is None

        made = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Phone",
                "kind": "wallet",
                "owner": "Jordan",
                "wallet_provider": "apple_pay",
                "funding_method_id": card_id,
            },
        )
        assert made.status_code == 201
        body = made.json()
        assert body["kind"] == "wallet"
        assert body["wallet_provider"] == "apple_pay"
        assert body["funding_method_id"] == card_id
        assert body["last4"] is None
        assert body["expiry_month"] is None
        assert body["effective_expiry_month"] == 12
        assert body["effective_expiry_year"] == 2026

        page = authed_client.get("/payment-methods")
        assert "Apple Pay (Everyday Visa ending 4321)" in page.text
        assert "Also used by: Apple Pay" in page.text
        assert "end of December 2026" in page.text
        assert 'id="pm-provider"' in page.text
        assert 'id="pm-funding"' in page.text

        google = _post(
            authed_client,
            "/payment-methods",
            {
                "nickname": "Watch",
                "kind": "wallet",
                "owner": "Jordan",
                "wallet_provider": "google_pay",
                "funding_method_id": str(card_id),
            },
        )
        assert google.status_code == 303
        stored = (
            db_session.query(PaymentMethod)
            .filter(PaymentMethod.nickname == "Watch")
            .one()
        )
        assert stored.wallet_provider == "google_pay"
        assert stored.last4 is None

        missing = authed_client.post(
            "/api/payment-methods",
            json={"nickname": "Bare", "kind": "wallet", "owner": "Jordan"},
        )
        assert missing.status_code == 422
        assert "Pick Apple Pay or Google Pay." in missing.text

        bank = authed_client.post(
            "/api/payment-methods",
            json={"nickname": "Offset", "kind": "bank", "owner": "Jordan"},
        )
        linked_bank = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Bad",
                "kind": "wallet",
                "owner": "Jordan",
                "wallet_provider": "apple_pay",
                "funding_method_id": bank.json()["id"],
            },
        )
        assert linked_bank.status_code == 422
        assert linked_bank.json()["detail"] == "Link the wallet to a card."
        assert "4321" not in linked_bank.text

        digits = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Digits",
                "kind": "wallet",
                "owner": "Jordan",
                "wallet_provider": "apple_pay",
                "funding_method_id": card_id,
                "last4": "9999",
            },
        )
        assert digits.status_code == 422
        assert "own last 4" in digits.text
        assert "9999" not in digits.text

        expiry = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Dated",
                "kind": "wallet",
                "owner": "Jordan",
                "wallet_provider": "apple_pay",
                "funding_method_id": card_id,
                "expiry_month": 3,
                "expiry_year": 2028,
            },
        )
        assert expiry.status_code == 422
        assert "Only a card has an expiry." in expiry.text

        on_card = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Nope",
                "kind": "card",
                "last4": "1111",
                "expiry_month": 1,
                "expiry_year": 2028,
                "owner": "Jordan",
                "wallet_provider": "apple_pay",
                "funding_method_id": card_id,
            },
        )
        assert on_card.status_code == 422
        assert "Only a wallet" in on_card.text

        card_row = db_session.get(PaymentMethod, card_id)
        card_row.is_archived = True
        db_session.commit()
        archived = authed_client.post(
            "/api/payment-methods",
            json={
                "nickname": "Late",
                "kind": "wallet",
                "owner": "Jordan",
                "wallet_provider": "apple_pay",
                "funding_method_id": card_id,
            },
        )
        assert archived.status_code == 422
        assert archived.json()["detail"] == "That card is archived."

    def test_nickname_edit_keeps_an_archived_card_link(self, authed_client, db_session):
        card = _card(db_session)
        other = _card(db_session, nickname="Old", last4="0001", is_archived=True)
        wallet = _wallet(db_session, card)
        card.is_archived = True
        db_session.commit()
        edited = authed_client.put(
            f"/api/payment-methods/{wallet.id}",
            json={"nickname": "Pocket"},
        )
        assert edited.status_code == 200
        assert edited.json()["nickname"] == "Pocket"
        assert edited.json()["funding_method_id"] == card.id
        assert edited.json()["wallet_provider"] == "apple_pay"
        reposted = authed_client.put(
            f"/api/payment-methods/{wallet.id}",
            json={"funding_method_id": card.id},
        )
        assert reposted.status_code == 200
        assert reposted.json()["funding_method_id"] == card.id
        moved = authed_client.put(
            f"/api/payment-methods/{wallet.id}",
            json={"funding_method_id": other.id},
        )
        assert moved.status_code == 422
        assert moved.json()["detail"] == "That card is archived."


class TestKindChanges:
    def test_card_kind_is_locked_and_leaving_wallet_clears_the_link(
        self, authed_client, db_session
    ):
        card = _card(db_session)
        wallet = _wallet(db_session, card)
        locked = authed_client.put(
            f"/api/payment-methods/{card.id}",
            json={"kind": "bank"},
        )
        assert locked.status_code == 409
        assert locked.json()["detail"] == "A wallet still links to this."
        db_session.refresh(card)
        assert card.kind == "card"

        cleared = authed_client.put(
            f"/api/payment-methods/{wallet.id}",
            json={"kind": "bank"},
        )
        assert cleared.status_code == 200
        assert cleared.json()["kind"] == "bank"
        assert cleared.json()["wallet_provider"] is None
        assert cleared.json()["funding_method_id"] is None
        db_session.refresh(wallet)
        assert wallet.wallet_provider is None
        assert wallet.funding_method_id is None

        again = authed_client.put(
            f"/api/payment-methods/{card.id}",
            json={"kind": "manual"},
        )
        assert again.status_code == 200
        assert again.json()["kind"] == "manual"


class TestDelete:
    def test_delete_is_409_while_any_wallet_points_here(
        self, authed_client, db_session
    ):
        card = _card(db_session)
        wallet = _wallet(db_session, card)
        wallet.is_archived = True
        db_session.commit()
        blocked = authed_client.delete(f"/api/payment-methods/{card.id}")
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "A wallet still links to this."
        page = authed_client.get("/payment-methods?archived=1")
        assert "A wallet still links to this." in page.text
        assert f"/payment-methods/{card.id}/delete" not in page.text
        gone = authed_client.delete(f"/api/payment-methods/{wallet.id}")
        assert gone.status_code == 200
        deleted = authed_client.delete(f"/api/payment-methods/{card.id}")
        assert deleted.status_code == 200


class TestExpiry:
    def test_banner_column_and_badges_follow_the_linked_card(
        self, authed_client, db_session, sample_category
    ):
        today = date(2026, 11, 2)
        card = _card(db_session, expiry_month=12, expiry_year=2026)
        expired_card = _card(
            db_session,
            nickname="Travel Visa",
            last4="1111",
            expiry_month=10,
            expiry_year=2026,
        )
        wallet = _wallet(db_session, card)
        expired_wallet = _wallet(
            db_session, expired_card, provider="google_pay", nickname="Watch"
        )
        _bill(db_session, sample_category.id, wallet.id, name="Netflix")
        _bill(db_session, sample_category.id, expired_wallet.id, name="Gym")
        _bill(db_session, sample_category.id, card.id, name="Power")

        assert effective_expiry(wallet) == (12, 2026)
        assert effective_expiry(expired_wallet) == (10, 2026)
        assert expiry_status(wallet, today, 60) == "expiring"
        assert expiry_status(expired_wallet, today, 60) == "expired"
        assert expires_label(wallet) == "end of December 2026"
        assert (
            method_status(wallet, is_old=False, is_new=False, today=today, warn_days=60)
            == "Expires soon"
        )
        assert (
            bill_badge(wallet, is_outgoing=False, today=today, warn_days=60)
            == "Expires soon"
        )
        assert (
            bill_badge(expired_wallet, is_outgoing=False, today=today, warn_days=60)
            == "Expired"
        )

        alerts = compute_payment_alerts(db_session, today=today, warn_days=60)
        texts = [line.text for line in alerts.lines]
        assert (
            "Google Pay (Travel Visa ending 1111) (Jordan) expired at the "
            "end of October 2026. Still used by Gym."
        ) in texts
        assert (
            "Apple Pay (Everyday Visa ending 4321) (Jordan) expires at the "
            "end of December 2026. Used by Netflix."
        ) in texts
        assert (
            "Everyday Visa ending 4321 (Jordan) expires at the "
            "end of December 2026. Used by Power."
        ) in texts
        assert texts.index(
            "Google Pay (Travel Visa ending 1111) (Jordan) expired at the "
            "end of October 2026. Still used by Gym."
        ) < texts.index(
            "Apple Pay (Everyday Visa ending 4321) (Jordan) expires at the "
            "end of December 2026. Used by Netflix."
        )

        card.is_archived = True
        db_session.commit()
        archived = compute_payment_alerts(db_session, today=today, warn_days=60)
        archived_text = next(
            line.text for line in archived.lines if "Apple Pay" in line.text
        )
        assert archived_text == (
            "Apple Pay (Everyday Visa ending 4321) is archived but Netflix still uses it."
        )
        assert any(
            "Power" in line.text and "archived" in line.text for line in archived.lines
        )


class TestReplacement:
    def test_wallet_rows_tick_finish_stop_and_sync(
        self, authed_client, db_session, sample_category
    ):
        old = _card(db_session, last4="4321")
        new = _card(
            db_session,
            nickname="New Visa",
            last4="9876",
            expiry_month=3,
            expiry_year=2028,
        )
        bank = _card(db_session, nickname="Offset", last4=None, kind="bank")
        bank.expiry_month = None
        bank.expiry_year = None
        db_session.commit()
        wallet = _wallet(db_session, old)
        archived = _wallet(db_session, old, nickname="Old phone")
        archived.is_archived = True
        db_session.commit()
        with pytest.raises(PaymentMethodError, match="Pick a card"):
            start(db_session, old.id, new_id=bank.id)

        rep = start(db_session, old.id, new_id=new.id)
        db_session.commit()
        db_session.refresh(wallet)
        assert wallet.funding_method_id == old.id
        assert (
            db_session.query(PaymentMethodReplacementItem)
            .filter(PaymentMethodReplacementItem.wallet_method_id == wallet.id)
            .count()
            == 1
        )
        assert (
            db_session.query(PaymentMethodReplacementItem)
            .filter(PaymentMethodReplacementItem.wallet_method_id == archived.id)
            .count()
            == 0
        )
        page = authed_client.get(f"/payment-methods/{old.id}/replace")
        assert (
            "Add New Visa ending 9876 to Apple Pay on your phone, then tick"
            in page.text
        )
        assert "0 of 1 moved" in page.text
        blocked = _post(authed_client, f"/payment-methods/{old.id}/replace/finish", {})
        assert blocked.status_code == 409
        assert "wallet is still linked" in blocked.text

        ticked = _post(
            authed_client,
            f"/payment-methods/{old.id}/replace/wallet-tick",
            {"wallet_method_id": str(wallet.id)},
        )
        assert ticked.status_code == 303
        db_session.refresh(wallet)
        assert wallet.funding_method_id == new.id

        extra = _wallet(db_session, old, nickname="Tablet")
        prog = progress(db_session, rep)
        assert any(item.wallet_method_id == extra.id for item in prog.items)
        assert prog.can_finish is False
        with pytest.raises(PaymentMethodError, match="wallet is still linked"):
            finish(db_session, rep)
        tick_wallet(db_session, rep, extra.id, today=date(2026, 11, 2))
        db_session.commit()
        done = finish(db_session, rep)
        assert done.is_archived is True
        db_session.refresh(extra)
        assert extra.funding_method_id == new.id

    def test_bills_and_wallets_and_stop(
        self, authed_client, db_session, sample_category
    ):
        old = _card(db_session)
        new = _card(db_session, nickname="New Visa", last4="9876", expiry_year=2028)
        wallet = _wallet(db_session, old)
        other = _wallet(db_session, old, provider="google_pay", nickname="Watch")
        bill = _bill(db_session, sample_category.id, old.id)
        rep = start(db_session, old.id, new_id=new.id)
        db_session.commit()
        prog = progress(db_session, rep, sync=False)
        assert prog.phrase == "0 of 1 bill moved, 0 of 2 wallets moved"
        assert {item.kind for item in prog.items} == {"bill", "wallet"}
        api = authed_client.get(f"/api/payment-methods/{old.id}")
        kinds = {item["kind"] for item in api.json()["replacement"]["items"]}
        assert kinds == {"bill", "wallet"}
        wallet_item = next(
            item
            for item in api.json()["replacement"]["items"]
            if item["wallet_method_id"] == wallet.id
        )
        assert wallet_item["wallet_provider"] == "apple_pay"
        assert wallet_item["funding_method_id"] == old.id
        assert "then tick" in wallet_item["name"]

        tick(db_session, rep, bill.id, today=date(2026, 11, 2))
        tick_wallet(db_session, rep, wallet.id, today=date(2026, 11, 2))
        db_session.commit()
        with pytest.raises(PaymentMethodError, match="wallet is still linked"):
            finish(db_session, rep)
        confirm = authed_client.get(f"/payment-methods/{old.id}/replace/stop")
        assert "Apple Pay you've ticked stays on the new card." in confirm.text
        stopped = _post(authed_client, f"/payment-methods/{old.id}/replace/stop", {})
        assert stopped.status_code == 303
        db_session.refresh(wallet)
        db_session.refresh(other)
        db_session.refresh(bill)
        assert wallet.funding_method_id == new.id
        assert other.funding_method_id == old.id
        assert bill.payment_method_id == new.id
        fresh = db_session.get(PaymentMethodReplacement, rep.id)
        assert fresh.status == "stopped"

    def test_replacing_a_wallet_defaults_the_new_form_to_a_card(
        self, authed_client, db_session, sample_category
    ):
        card = _card(db_session)
        wallet = _wallet(db_session, card)
        _bill(db_session, sample_category.id, wallet.id)
        page = authed_client.get(f"/payment-methods/{wallet.id}/replace")
        assert 'id="pm-kind"' in page.text
        assert (
            'value="card" selected' in page.text or 'value="card" selected' in page.text
        )
        assert "selected" in page.text
        selected = page.text.split('id="pm-kind"', 1)[1].split("</select>", 1)[0]
        assert 'value="card" selected' in selected


class TestMcp:
    def test_reads_include_wallet_fields_and_writes_refuse_wallet(
        self, db_session, setup_database
    ):
        card = _card(db_session, expiry_month=3, expiry_year=2027)
        wallet = _wallet(db_session, card)
        db_session.commit()
        listed = list_payment_methods()
        found = next(row for row in listed if row["id"] == wallet.id)
        assert found["wallet_provider"] == "apple_pay"
        assert found["funding_method_id"] == card.id
        assert found["effective_expiry_month"] == 3
        assert found["effective_expiry_year"] == 2027
        fetched = get_payment_method(wallet.id)
        assert isinstance(fetched, dict)
        assert fetched["effective_expiry_month"] == 3

        refused = create_payment_method(
            nickname="Phone",
            kind="wallet",
            owner="Jordan",
            last4="4321",
        )
        assert refused == "Add a wallet on the payment methods page."
        assert "4321" not in str(refused)
        changed = update_payment_method(wallet.id, kind="wallet")
        assert changed == "Add a wallet on the payment methods page."
        kept = update_payment_method(wallet.id, nickname="Pocket")
        assert isinstance(kept, dict)
        assert kept["nickname"] == "Pocket"
        assert kept["wallet_provider"] == "apple_pay"
        assert kept["funding_method_id"] == card.id
        assert kept["effective_expiry_year"] == 2027
        db_session.refresh(wallet)
        assert wallet.funding_method_id == card.id

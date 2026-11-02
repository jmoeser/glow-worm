"""Saved, resumable payment-method replacement."""

from datetime import date, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    PaymentMethod,
    PaymentMethodReplacement,
    PaymentMethodReplacementItem,
    RecurringBill,
)
from app.services.payment_methods import (
    PaymentMethodError,
    _add_replacement_item,
    compute_payment_alerts,
    finish,
    progress,
    save_note,
    start,
    stop,
    sync_replacement_items,
    tick,
    untick,
)
from tests.conftest import TestingSessionLocal


def _method(db, nickname, **kwargs) -> PaymentMethod:
    values = {
        "nickname": nickname,
        "kind": "manual",
        "owner": "Jordan",
        "last4": None,
        "expiry_month": None,
        "expiry_year": None,
    }
    values.update(kwargs)
    method = PaymentMethod(**values)
    db.add(method)
    db.commit()
    db.refresh(method)
    return method


def _bill(db, category_id, method_id, name="Netflix", due="2026-11-05", **kwargs):
    bill = RecurringBill(
        name=name,
        amount=15,
        debtor_provider=name,
        start_date="2026-01-01",
        frequency="monthly",
        category_id=category_id,
        next_due_date=due,
        payment_method_id=method_id,
        is_active=kwargs.get("is_active", True),
        end_date=kwargs.get("end_date"),
    )
    db.add(bill)
    db.commit()
    db.refresh(bill)
    return bill


def _post(client, url, data):
    return client.post(
        url,
        data=data,
        headers={"x-csrftoken": client.csrf_token},
        follow_redirects=False,
    )


class TestStart:
    def test_start_with_new_or_existing_method(
        self, authed_client, db_session, sample_category
    ):
        old = _method(
            db_session,
            "Everyday Visa",
            kind="card",
            last4="4321",
            expiry_month=12,
            expiry_year=2027,
        )
        _bill(db_session, sample_category.id, old.id)
        existing = _method(db_session, "Offset")
        response = _post(
            authed_client,
            f"/payment-methods/{old.id}/replace",
            {"existing_id": str(existing.id)},
        )
        assert response.status_code == 303
        rep = db_session.query(PaymentMethodReplacement).one()
        assert rep.new_id == existing.id
        assert rep.status == "open"
        page = authed_client.get(f"/payment-methods/{old.id}/replace")
        assert "Moving bills to Offset" in page.text
        assert "0 of 1 bill moved" in page.text

        stop(db_session, rep)
        db_session.commit()
        created = _post(
            authed_client,
            f"/payment-methods/{old.id}/replace",
            {
                "existing_id": "",
                "nickname": "Everyday Visa",
                "kind": "card",
                "owner": "Jordan",
                "last4": "9876",
                "expiry": "2028-03",
            },
        )
        assert created.status_code == 303
        fresh = (
            db_session.query(PaymentMethodReplacement)
            .filter(PaymentMethodReplacement.status == "open")
            .one()
        )
        new = db_session.get(PaymentMethod, fresh.new_id)
        assert new.last4 == "9876"
        assert new.expiry_month == 3

    def test_rejections(self, db_session, sample_category):
        old = _method(db_session, "A")
        other = _method(db_session, "B")
        third = _method(db_session, "C")
        empty = _method(db_session, "Empty")
        with pytest.raises(PaymentMethodError, match="No active bills"):
            start(db_session, empty.id, new_id=other.id)

        _bill(db_session, sample_category.id, old.id, name="Rent")
        _bill(db_session, sample_category.id, other.id, name="Power")
        _bill(db_session, sample_category.id, third.id, name="Water")
        with pytest.raises(PaymentMethodError, match="different payment method"):
            start(db_session, old.id, new_id=old.id)

        old.is_archived = True
        db_session.commit()
        with pytest.raises(PaymentMethodError, match="not found or archived"):
            start(db_session, old.id, new_id=other.id)
        old.is_archived = False
        other.is_archived = True
        db_session.commit()
        with pytest.raises(PaymentMethodError, match="not found or archived"):
            start(db_session, old.id, new_id=other.id)
        other.is_archived = False
        db_session.commit()

        first = start(db_session, old.id, new_id=third.id)
        db_session.commit()
        with pytest.raises(PaymentMethodError, match="already open"):
            start(db_session, old.id, new_id=other.id)
        with pytest.raises(PaymentMethodError, match="already in a replacement"):
            start(db_session, other.id, new_id=old.id)
        with pytest.raises(PaymentMethodError, match="Finish or stop"):
            start(db_session, third.id, new_id=other.id)
        assert first.status == "open"


class TestTicks:
    def test_tick_keeps_typed_date_and_untick_keeps_note(
        self, db_session, sample_category
    ):
        old = _method(db_session, "Old")
        new = _method(db_session, "New")
        bill = _bill(db_session, sample_category.id, old.id)
        rep = start(db_session, old.id, new_id=new.id)
        save_note(db_session, rep, bill.id, note="Called them", updated_on="2026-11-01")
        tick(db_session, rep, bill.id, today=date(2026, 11, 2))
        db_session.flush()
        item = db_session.query(PaymentMethodReplacementItem).one()
        assert bill.payment_method_id == new.id, (
            bill.payment_method_id,
            old.id,
            new.id,
            rep.new_id,
        )
        assert item.updated_on == "2026-11-01"
        assert item.note == "Called them"
        untick(db_session, rep, bill.id)
        db_session.flush()
        assert bill.payment_method_id == old.id, (
            bill.payment_method_id,
            old.id,
            new.id,
        )
        assert item.updated_on is None
        assert item.note == "Called them"

    def test_tick_sets_today_when_date_empty_and_outside_move_shows_moved(
        self, authed_client, db_session, sample_category
    ):
        old = _method(db_session, "Old")
        new = _method(db_session, "New")
        bill = _bill(db_session, sample_category.id, old.id)
        rep = start(db_session, old.id, new_id=new.id)
        db_session.commit()
        tick(db_session, rep, bill.id, today=date(2026, 11, 2))
        item = db_session.query(PaymentMethodReplacementItem).one()
        assert item.updated_on == "2026-11-02"
        item.updated_on = None
        bill.payment_method_id = new.id
        db_session.commit()
        page = authed_client.get(f"/payment-methods/{old.id}/replace")
        assert "Moved" in page.text
        prog = progress(db_session, rep, today=date(2026, 11, 2))
        assert prog.items[0].done is True
        assert prog.items[0].updated_on is None

    def test_state_survives_a_fresh_session(self, db_session, sample_category):
        old = _method(db_session, "Old")
        new = _method(db_session, "New")
        bill = _bill(db_session, sample_category.id, old.id, name="Netflix")
        rep = start(db_session, old.id, new_id=new.id)
        save_note(
            db_session, rep, bill.id, note="Left a message", updated_on="2026-11-01"
        )
        tick(db_session, rep, bill.id, today=date(2026, 11, 2))
        db_session.commit()
        fresh = TestingSessionLocal()
        try:
            stored = fresh.query(PaymentMethodReplacementItem).one()
            assert stored.note == "Left a message"
            assert stored.updated_on == "2026-11-01"
            bill_row = fresh.get(RecurringBill, bill.id)
            assert bill_row.payment_method_id == new.id
        finally:
            fresh.close()


class TestJoinAndDrop:
    def test_turned_on_bill_joins_before_tick_finish_and_reads(
        self, authed_client, db_session, sample_category
    ):
        from app.mcp_server import get_payment_method

        old = _method(db_session, "Old")
        new = _method(db_session, "New")
        first = _bill(db_session, sample_category.id, old.id, name="Netflix")
        paused = _bill(
            db_session,
            sample_category.id,
            old.id,
            name="Gym",
            is_active=False,
        )
        rep = start(db_session, old.id, new_id=new.id)
        db_session.commit()
        assert db_session.query(PaymentMethodReplacementItem).count() == 1
        paused.is_active = True
        db_session.commit()
        tick(db_session, rep, paused.id, today=date(2026, 11, 2))
        db_session.commit()
        assert (
            db_session.query(PaymentMethodReplacementItem)
            .filter(PaymentMethodReplacementItem.recurring_bill_id == paused.id)
            .count()
            == 1
        )
        db_session.refresh(paused)
        assert paused.payment_method_id == new.id

        _bill(db_session, sample_category.id, old.id, name="Spotify")
        api = authed_client.get(f"/api/payment-methods/{old.id}")
        names = [item["name"] for item in api.json()["replacement"]["items"]]
        assert "Spotify" in names
        mcp = get_payment_method(old.id)
        assert isinstance(mcp, dict)
        assert "Spotify" in [item["name"] for item in mcp["replacement"]["items"]]
        finish_rows = progress(db_session, rep)
        assert any(item.name == "Spotify" for item in finish_rows.items)
        assert first.id

    def test_duplicate_sync_and_integrity_error_are_safe(
        self, db_session, sample_category
    ):
        old = _method(db_session, "Old")
        new = _method(db_session, "New")
        bill = _bill(db_session, sample_category.id, old.id)
        rep = start(db_session, old.id, new_id=new.id)
        db_session.commit()
        sync_replacement_items(db_session, rep)
        sync_replacement_items(db_session, rep)
        _add_replacement_item(db_session, rep.id, bill.id)
        assert db_session.query(PaymentMethodReplacementItem).count() == 1

    def test_drop_out_and_past_end_date(self, db_session, sample_category):
        old = _method(db_session, "Old")
        new = _method(db_session, "New")
        third = _method(db_session, "Third")
        stayed = _bill(
            db_session,
            sample_category.id,
            old.id,
            name="Kept",
            end_date="2020-01-01",
        )
        dropped = _bill(db_session, sample_category.id, old.id, name="Gym")
        rep = start(db_session, old.id, new_id=new.id)
        db_session.commit()
        dropped.is_active = False
        db_session.commit()
        prog = progress(db_session, rep, today=date(2026, 11, 2))
        assert prog.counted == 1
        assert "Gym" in prog.not_counted_names
        dropped.is_active = True
        dropped.payment_method_id = third.id
        db_session.commit()
        prog = progress(db_session, rep, today=date(2026, 11, 2))
        assert "Gym" in prog.not_counted_names
        dropped.payment_method_id = None
        db_session.commit()
        prog = progress(db_session, rep, today=date(2026, 11, 2))
        assert "Gym" in prog.not_counted_names
        dropped.payment_method_id = old.id
        db_session.commit()
        prog = progress(db_session, rep, today=date(2026, 11, 2))
        assert prog.counted == 2
        assert "Gym" not in prog.not_counted_names
        assert stayed.end_date == "2020-01-01"

    def test_due_soon_and_row_order(self, db_session, sample_category):
        old = _method(db_session, "Old")
        new = _method(db_session, "New")
        today = date(2026, 11, 1)
        overdue = _bill(
            db_session, sample_category.id, old.id, name="Overdue", due="2026-10-01"
        )
        day7 = _bill(
            db_session, sample_category.id, old.id, name="Day7", due="2026-11-08"
        )
        day8 = _bill(
            db_session, sample_category.id, old.id, name="Day8", due="2026-11-09"
        )
        invalid = _bill(db_session, sample_category.id, old.id, name="Bad", due="nope")
        done = _bill(
            db_session, sample_category.id, old.id, name="Done", due="2026-10-02"
        )
        rep = start(db_session, old.id, new_id=new.id)
        tick(db_session, rep, done.id, today=today)
        db_session.commit()
        prog = progress(db_session, rep, today=today)
        names = [item.name for item in prog.items]
        assert names == ["Overdue", "Day7", "Day8", "Bad", "Done"]
        by_name = {item.name: item for item in prog.items}
        assert by_name["Overdue"].due_soon is True
        assert by_name["Day7"].due_soon is True
        assert by_name["Day8"].due_soon is False
        assert by_name["Bad"].due_soon is False
        assert by_name["Done"].due_soon is False
        assert overdue.id and day7.id and day8.id and invalid.id


class TestFinishAndStop:
    def test_finish_asks_and_refuses_a_bill_left_behind(
        self, authed_client, db_session, sample_category
    ):
        old = _method(
            db_session,
            "Everyday Visa",
            kind="card",
            last4="4321",
            expiry_month=1,
            expiry_year=2027,
        )
        new = _method(
            db_session,
            "New card",
            kind="card",
            last4="9876",
            expiry_month=2,
            expiry_year=2028,
        )
        first = _bill(db_session, sample_category.id, old.id, name="Netflix")
        second = _bill(db_session, sample_category.id, old.id, name="Gym")
        rep = start(db_session, old.id, new_id=new.id)
        tick(db_session, rep, first.id, today=date(2026, 11, 2))
        db_session.commit()
        db_session.refresh(rep)
        assert rep.status == "open"
        confirm = authed_client.get(f"/payment-methods/{old.id}/replace/finish")
        assert "Archive Everyday Visa ending 4321?" in confirm.text
        assert "unarchive it later" in confirm.text
        db_session.refresh(old)
        assert old.is_archived is False

        blocked = _post(authed_client, f"/payment-methods/{old.id}/replace/finish", {})
        assert blocked.status_code == 409
        assert "still on the old payment method" in blocked.text

        tick(db_session, rep, second.id, today=date(2026, 11, 2))
        db_session.commit()
        second.payment_method_id = old.id
        db_session.commit()
        with pytest.raises(PaymentMethodError, match="still on the old"):
            finish(db_session, rep)
        db_session.rollback()
        second.payment_method_id = new.id
        db_session.commit()
        rep = db_session.get(PaymentMethodReplacement, rep.id)
        done = _post(authed_client, f"/payment-methods/{old.id}/replace/finish", {})
        assert done.status_code == 303
        assert done.headers["location"] == "/payment-methods"
        page = authed_client.get("/payment-methods")
        assert "Done. Everyday Visa ending 4321 is archived." in page.text
        db_session.refresh(old)
        assert old.is_archived is True
        stopped = _post(authed_client, f"/payment-methods/{old.id}/replace/stop", {})
        assert stopped.status_code == 409

    def test_stop_keeps_bills_and_expiry_line_returns(
        self, authed_client, db_session, sample_category
    ):
        old = _method(
            db_session,
            "Everyday Visa",
            kind="card",
            last4="4321",
            expiry_month=10,
            expiry_year=2026,
        )
        new = _method(db_session, "New")
        bill = _bill(db_session, sample_category.id, old.id, name="Netflix")
        other = _bill(db_session, sample_category.id, old.id, name="Gym")
        rep = start(db_session, old.id, new_id=new.id)
        tick(db_session, rep, bill.id, today=date(2026, 11, 2))
        db_session.commit()
        confirm = authed_client.get(f"/payment-methods/{old.id}/replace/stop")
        assert "Bills you've moved stay on the new one." in confirm.text
        assert "wrong" in confirm.text
        stopped = _post(authed_client, f"/payment-methods/{old.id}/replace/stop", {})
        assert stopped.status_code == 303
        page = authed_client.get("/payment-methods")
        assert "Stopped. No bills were changed." in page.text
        db_session.refresh(bill)
        db_session.refresh(other)
        assert bill.payment_method_id == new.id
        assert other.payment_method_id == old.id
        again = authed_client.get(f"/payment-methods/{old.id}/replace")
        assert "Start replacing" in again.text
        alerts = compute_payment_alerts(
            db_session, today=date(2026, 11, 2), warn_days=60
        )
        assert any(
            "expired at the end of October 2026" in line.text for line in alerts.lines
        )
        assert all(line.link_label != "Continue" for line in alerts.lines)
        restarted = start(db_session, old.id, new_id=new.id)
        assert restarted.status == "open"


class TestWhileOpen:
    def test_archive_delete_and_chaining_are_blocked(
        self, authed_client, db_session, sample_category
    ):
        old = _method(db_session, "Old")
        new = _method(db_session, "New")
        _bill(db_session, sample_category.id, old.id)
        start(db_session, old.id, new_id=new.id)
        db_session.commit()
        for method_id in (old.id, new.id):
            archived = authed_client.post(f"/api/payment-methods/{method_id}/archive")
            deleted = authed_client.delete(f"/api/payment-methods/{method_id}")
            assert archived.status_code == 409
            assert deleted.status_code == 409
            assert "Finish or stop replacing first." in archived.text
        with pytest.raises(PaymentMethodError, match="Finish or stop"):
            start(db_session, new.id, new_id=old.id)
        with pytest.raises(PaymentMethodError, match="already open"):
            start(db_session, old.id, new_id=new.id)

        extra = _method(db_session, "Extra")
        duplicate = PaymentMethodReplacement(
            old_id=old.id,
            new_id=extra.id,
            status="open",
            started_at=datetime(2026, 11, 2, 0, 0),
        )
        db_session.add(duplicate)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        bad_status = PaymentMethodReplacement(
            old_id=old.id,
            new_id=extra.id,
            status="Open",
            started_at=datetime(2026, 11, 2, 0, 0),
        )
        db_session.add(bad_status)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


class TestBillLinkRules:
    def test_current_outgoing_or_archived_can_be_reposted(
        self, authed_client, db_session, sample_category
    ):
        from app.mcp_server import update_bill

        archived = _method(db_session, "Archived")
        old = _method(db_session, "Outgoing")
        new = _method(db_session, "Incoming")
        other = _method(db_session, "Other")
        bill = _bill(db_session, sample_category.id, archived.id, name="Rent")
        archived.is_archived = True
        other.is_archived = True
        db_session.commit()
        kept = authed_client.put(
            f"/api/bills/{bill.id}",
            json={"payment_method_id": archived.id, "name": "Rent"},
        )
        assert kept.status_code == 200
        assert kept.json()["payment_method_id"] == archived.id

        changed = authed_client.put(
            f"/api/bills/{bill.id}",
            json={"payment_method_id": other.id},
        )
        assert changed.status_code == 422
        assert "not found or archived" in changed.text

        _bill(db_session, sample_category.id, old.id, name="Power")
        start(db_session, old.id, new_id=new.id)
        db_session.commit()
        power = (
            db_session.query(RecurringBill).filter(RecurringBill.name == "Power").one()
        )
        same = authed_client.put(
            f"/api/bills/{power.id}",
            json={"payment_method_id": old.id},
        )
        assert same.status_code == 200
        html = _post(
            authed_client,
            f"/bills/{power.id}",
            {"name": "Power", "payment_method_id": str(old.id)},
        )
        assert html.status_code == 200
        moved = authed_client.put(
            f"/api/bills/{bill.id}",
            json={"payment_method_id": old.id},
        )
        assert moved.status_code == 422
        assert "being replaced" in moved.text
        mcp = update_bill(bill.id, payment_method_id=old.id)
        assert isinstance(mcp, str)
        assert "being replaced" in mcp
        assert "4111" not in mcp

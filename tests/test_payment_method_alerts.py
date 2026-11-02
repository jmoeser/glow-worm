"""Expiry dates and dashboard payment-method lines."""

from datetime import date

from app.config import payment_method_expiry_warn_days
from app.models import PaymentMethod, RecurringBill
from app.services.payment_methods import (
    compute_payment_alerts,
    expires_on,
    expiry_status,
    progress_phrase,
    start,
)


def _method(db, **kwargs) -> PaymentMethod:
    values = {
        "nickname": "Everyday Visa",
        "kind": "card",
        "last4": "4321",
        "expiry_month": 10,
        "expiry_year": 2026,
        "owner": "Jordan",
    }
    values.update(kwargs)
    method = PaymentMethod(**values)
    db.add(method)
    db.commit()
    db.refresh(method)
    return method


def _bill(db, category_id, method_id, name, due, **kwargs) -> RecurringBill:
    bill = RecurringBill(
        name=name,
        amount=10,
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


class TestExpiry:
    def test_expires_on_leap_year_and_december(self):
        assert expires_on(2, 2028) == date(2028, 2, 29)
        assert expires_on(12, 2026) == date(2026, 12, 31)

    def test_october_card_is_valid_on_the_last_day(self, db_session):
        card = _method(db_session, expiry_month=10, expiry_year=2026)
        assert expiry_status(card, date(2026, 10, 31), 60) == "expiring"
        assert expiry_status(card, date(2026, 11, 1), 60) == "expired"

    def test_window_includes_warn_days_and_excludes_the_next_day(self, db_session):
        card = _method(db_session, expiry_month=12, expiry_year=2026)
        today = date(2026, 11, 1)
        assert expiry_status(card, today, 60) == "expiring"
        assert expiry_status(card, today, 59) is None

    def test_env_override_and_bad_values(self, monkeypatch, caplog):
        monkeypatch.delenv("PAYMENT_METHOD_EXPIRY_WARN_DAYS", raising=False)
        assert payment_method_expiry_warn_days() == 60
        monkeypatch.setenv("PAYMENT_METHOD_EXPIRY_WARN_DAYS", "14")
        assert payment_method_expiry_warn_days() == 14
        monkeypatch.setenv("PAYMENT_METHOD_EXPIRY_WARN_DAYS", "0")
        assert payment_method_expiry_warn_days() == 1
        assert "outside 1-365" in caplog.text
        monkeypatch.setenv("PAYMENT_METHOD_EXPIRY_WARN_DAYS", "400")
        assert payment_method_expiry_warn_days() == 365
        caplog.clear()
        monkeypatch.setenv("PAYMENT_METHOD_EXPIRY_WARN_DAYS", "nope")
        assert payment_method_expiry_warn_days() == 60
        assert "Invalid PAYMENT_METHOD_EXPIRY_WARN_DAYS" in caplog.text


class TestAlertLines:
    def test_unused_and_inactive_methods_have_no_line(
        self, db_session, sample_category
    ):
        unused = _method(db_session, nickname="Spare")
        inactive = _method(db_session, nickname="Dormant", last4="0001")
        _bill(
            db_session,
            sample_category.id,
            inactive.id,
            "Old gym",
            "2026-11-01",
            is_active=False,
        )
        alerts = compute_payment_alerts(
            db_session, today=date(2026, 11, 2), warn_days=60
        )
        assert alerts.show_banner is False
        assert unused.id
        assert alerts.lines == ()

    def test_past_end_date_still_counts(self, db_session, sample_category):
        card = _method(db_session)
        _bill(
            db_session,
            sample_category.id,
            card.id,
            "Gym",
            "2026-11-05",
            end_date="2020-01-01",
        )
        alerts = compute_payment_alerts(
            db_session, today=date(2026, 11, 2), warn_days=60
        )
        assert len(alerts.lines) == 1
        assert "Gym" in alerts.lines[0].text
        assert "2026" in alerts.lines[0].text

    def test_one_line_per_case_order_plurals_and_cap(self, db_session, sample_category):
        today = date(2026, 11, 2)
        expired = _method(db_session, nickname="Everyday Visa", last4="4321")
        for name, due in (
            ("Netflix", "2026-11-05"),
            ("Origin Energy", "2026-11-06"),
            ("Spotify", "2026-11-07"),
            ("Gym", "2026-11-20"),
        ):
            _bill(db_session, sample_category.id, expired.id, name, due)

        expiring = _method(
            db_session,
            nickname="Travel Visa",
            last4="1111",
            expiry_month=12,
            expiry_year=2026,
        )
        _bill(db_session, sample_category.id, expiring.id, "Cloud", "2026-12-01")
        _bill(db_session, sample_category.id, expiring.id, "Mobile", "2026-12-02")

        archived = _method(
            db_session,
            nickname="Old Mastercard",
            kind="manual",
            last4=None,
            expiry_month=None,
            expiry_year=None,
            is_archived=True,
        )
        _bill(
            db_session, sample_category.id, archived.id, "Gym membership", "2026-11-03"
        )

        old_bank = _method(
            db_session,
            nickname="Joint account",
            kind="bank",
            last4=None,
            expiry_month=None,
            expiry_year=None,
        )
        new_bank = _method(
            db_session,
            nickname="Offset account",
            kind="bank",
            last4=None,
            expiry_month=None,
            expiry_year=None,
        )
        for index in range(7):
            _bill(
                db_session,
                sample_category.id,
                old_bank.id,
                f"Bill {index}",
                "2027-06-01",
            )
        start(db_session, old_bank.id, new_id=new_bank.id)
        db_session.commit()

        extra_old = _method(
            db_session,
            nickname="Alpha account",
            kind="bank",
            last4=None,
            expiry_month=None,
            expiry_year=None,
        )
        extra_new = _method(
            db_session,
            nickname="Beta account",
            kind="bank",
            last4=None,
            expiry_month=None,
            expiry_year=None,
        )
        _bill(db_session, sample_category.id, extra_old.id, "Quiet", "2027-08-01")
        start(db_session, extra_old.id, new_id=extra_new.id)
        zebra_old = _method(
            db_session,
            nickname="Zebra account",
            kind="bank",
            last4=None,
            expiry_month=None,
            expiry_year=None,
        )
        zebra_new = _method(
            db_session,
            nickname="Zebra new",
            kind="bank",
            last4=None,
            expiry_month=None,
            expiry_year=None,
        )
        _bill(db_session, sample_category.id, zebra_old.id, "Later bill", "2027-09-01")
        start(db_session, zebra_old.id, new_id=zebra_new.id)
        db_session.commit()

        alerts = compute_payment_alerts(db_session, today=today, warn_days=60)
        assert alerts.warning is True
        assert alerts.headline == "Payment updates needed"
        assert alerts.extra == 1
        assert len(alerts.lines) == 5
        assert alerts.lines[0].text == (
            "Everyday Visa ending 4321 (Jordan) expired at the end of October 2026. "
            "Still used by Netflix, Origin Energy, Spotify and 1 more."
        )
        assert "2026" in alerts.lines[0].text
        assert alerts.lines[1].text == (
            "Travel Visa ending 1111 (Jordan) expires at the end of December 2026. "
            "Used by Cloud and Mobile."
        )
        assert alerts.lines[2].rank == 2
        assert "archived" in alerts.lines[2].text
        assert alerts.lines[0].link_label == "Replace"
        assert "October 26" not in alerts.lines[0].text
        joined = " ".join(line.text for line in alerts.lines)
        assert "Zebra account" not in joined

    def test_replacement_lines_and_neutral_headline(self, db_session, sample_category):
        today = date(2026, 11, 2)
        old = _method(
            db_session, nickname="Everyday Visa", last4="4321", expiry_month=10
        )
        new = _method(
            db_session,
            nickname="Everyday Visa",
            last4="9876",
            expiry_month=3,
            expiry_year=2028,
        )
        dues = [
            "2026-11-05",
            "2026-12-01",
            "2026-12-02",
            "2026-12-03",
            "2027-01-01",
            "2027-02-01",
            "2027-03-01",
        ]
        bills = [
            _bill(db_session, sample_category.id, old.id, f"Bill {index}", due)
            for index, due in enumerate(dues)
        ]
        replacement = start(db_session, old.id, new_id=new.id)
        from app.services.payment_methods import tick

        for bill in bills[4:]:
            tick(db_session, replacement, bill.id, today=today)
        db_session.commit()
        alerts = compute_payment_alerts(db_session, today=today, warn_days=60)
        assert alerts.lines[0].text == (
            "Everyday Visa ending 4321 expired at the end of October 2026 "
            "and 4 bills are still on it. "
            "Moving to Everyday Visa ending 9876: 3 of 7 bills moved, 1 due soon."
        )
        assert alerts.lines[0].link_label == "Continue"
        assert alerts.warning is True
        assert progress_phrase(1, 1) == "1 of 1 bill moved"
        assert progress_phrase(3, 7) == "3 of 7 bills moved"

        expiring_old = _method(
            db_session,
            nickname="Everyday Visa",
            last4="2222",
            expiry_month=12,
            expiry_year=2026,
        )
        expiring_new = _method(
            db_session,
            nickname="Everyday Visa",
            last4="3333",
            expiry_month=6,
            expiry_year=2028,
        )
        for index in range(2):
            _bill(
                db_session,
                sample_category.id,
                expiring_old.id,
                f"Later {index}",
                "2027-04-01",
            )
        start(db_session, expiring_old.id, new_id=expiring_new.id)
        db_session.commit()
        mixed = compute_payment_alerts(db_session, today=today, warn_days=60)
        expiring_line = next(line for line in mixed.lines if "2222" in line.text)
        assert expiring_line.text == (
            "Replacing Everyday Visa ending 2222 (expires end of December 2026) "
            "with Everyday Visa ending 3333: 0 of 2 bills moved."
        )

    def test_bank_replacement_is_neutral_without_due_soon(
        self, db_session, sample_category
    ):
        old = _method(
            db_session,
            nickname="Joint account",
            kind="bank",
            last4=None,
            expiry_month=None,
            expiry_year=None,
        )
        new = _method(
            db_session,
            nickname="Offset account",
            kind="bank",
            last4=None,
            expiry_month=None,
            expiry_year=None,
        )
        bills = [
            _bill(db_session, sample_category.id, old.id, f"Bill {index}", "2027-06-01")
            for index in range(7)
        ]
        replacement = start(db_session, old.id, new_id=new.id)
        from app.services.payment_methods import tick

        for bill in bills[:3]:
            tick(db_session, replacement, bill.id, today=date(2026, 11, 2))
        db_session.commit()
        alerts = compute_payment_alerts(
            db_session, today=date(2026, 11, 2), warn_days=60
        )
        assert alerts.warning is False
        assert alerts.headline == "Payment updates in progress"
        assert alerts.lines[0].text == (
            "Replacing Joint account with Offset account: 3 of 7 bills moved."
        )
        assert "expired" not in alerts.lines[0].text
        assert "expires" not in alerts.lines[0].text

    def test_archived_one_and_many(self, db_session, sample_category):
        one = _method(
            db_session,
            nickname="Old Mastercard",
            kind="manual",
            last4=None,
            expiry_month=None,
            expiry_year=None,
            is_archived=True,
        )
        _bill(db_session, sample_category.id, one.id, "Gym", "2026-11-01")
        alerts = compute_payment_alerts(db_session, today=date(2026, 11, 2))
        assert (
            alerts.lines[0].text == "Old Mastercard is archived but Gym still uses it."
        )
        assert alerts.lines[0].link_label == "Edit bills"

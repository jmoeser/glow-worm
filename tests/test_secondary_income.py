from app.models import (
    MonthlyUnallocatedIncome,
    SecondaryIncomeAllocation,
    SecondaryIncomeAllocationRule,
    SinkingFund,
    Transaction,
)


class TestSecondaryIncomePageGet:
    def test_renders_form(self, authed_client):
        response = authed_client.get("/income/secondary")
        assert response.status_code == 200
        assert 'name="label"' in response.text

    def test_renders_fund_inputs(self, authed_client, sample_sinking_funds):
        response = authed_client.get("/income/secondary")
        assert response.status_code == 200
        for fund in sample_sinking_funds:
            assert f'name="sec_goal_{fund.id}"' in response.text
            assert f'name="sec_order_{fund.id}"' in response.text
            assert fund.name in response.text

    def test_prefills_existing_rules(
        self, authed_client, secondary_income_allocation, sample_sinking_funds
    ):
        response = authed_client.get("/income/secondary")
        assert response.status_code == 200
        assert "Partner Income" in response.text
        assert "600.00" in response.text
        assert "400.00" in response.text

    def test_unauthenticated_redirects(self, client):
        response = client.get("/income/secondary", follow_redirects=False)
        assert response.status_code == 303
        assert "/login" in response.headers["location"]

    def test_shows_no_funds_message_when_empty(self, authed_client):
        response = authed_client.get("/income/secondary")
        assert response.status_code == 200
        assert "No active sinking funds" in response.text

    def test_shows_overflow_fund_dropdown(self, authed_client, sample_sinking_funds):
        response = authed_client.get("/income/secondary")
        assert response.status_code == 200
        assert 'name="overflow_sinking_fund_id"' in response.text


class TestSecondaryIncomeConfigPost:
    def _post(self, client, data):
        return client.post(
            "/income/secondary",
            data=data,
            headers={"x-csrftoken": client.csrf_token},
        )

    def test_creates_config(self, authed_client, db_session, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = self._post(
            authed_client,
            {
                "label": "Partner",
                f"sec_goal_{fund.id}": "500",
                f"sec_order_{fund.id}": "1",
            },
        )
        assert response.status_code == 200
        assert "saved" in response.text

        alloc = db_session.query(SecondaryIncomeAllocation).first()
        assert alloc is not None
        assert alloc.label == "Partner"
        rules = db_session.query(SecondaryIncomeAllocationRule).all()
        assert len(rules) == 1
        assert abs(float(rules[0].goal_amount) - 500.0) < 0.01
        assert rules[0].sort_order == 1

    def test_updates_replaces_rules(
        self,
        authed_client,
        db_session,
        secondary_income_allocation,
        sample_sinking_funds,
    ):
        fund = sample_sinking_funds[0]
        self._post(
            authed_client,
            {
                "label": "Updated",
                f"sec_goal_{fund.id}": "750",
                f"sec_order_{fund.id}": "1",
            },
        )

        rules = db_session.query(SecondaryIncomeAllocationRule).all()
        assert len(rules) == 1
        assert abs(float(rules[0].goal_amount) - 750.0) < 0.01

    def test_skips_zero_goal(self, authed_client, db_session, sample_sinking_funds):
        f1, f2 = sample_sinking_funds
        self._post(
            authed_client,
            {
                "label": "X",
                f"sec_goal_{f1.id}": "300",
                f"sec_order_{f1.id}": "1",
                f"sec_goal_{f2.id}": "0",
                f"sec_order_{f2.id}": "2",
            },
        )
        rules = db_session.query(SecondaryIncomeAllocationRule).all()
        assert len(rules) == 1

    def test_saves_label(self, authed_client, db_session, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        self._post(
            authed_client,
            {
                "label": "My Partner",
                f"sec_goal_{fund.id}": "200",
                f"sec_order_{fund.id}": "1",
            },
        )
        alloc = db_session.query(SecondaryIncomeAllocation).first()
        assert alloc.label == "My Partner"

    def test_saves_overflow_fund(self, authed_client, db_session, sample_sinking_funds):
        f1, f2 = sample_sinking_funds
        self._post(
            authed_client,
            {
                "label": "X",
                f"sec_goal_{f1.id}": "300",
                f"sec_order_{f1.id}": "1",
                "overflow_sinking_fund_id": str(f2.id),
            },
        )
        alloc = db_session.query(SecondaryIncomeAllocation).first()
        assert alloc.overflow_sinking_fund_id == f2.id

    def test_csrf_required(self, authed_client, sample_sinking_funds):
        response = authed_client.post(
            "/income/secondary",
            data={"label": "X", f"sec_goal_{sample_sinking_funds[0].id}": "100"},
        )
        assert response.status_code == 403


class TestRecordSecondaryIncome:
    def _record(self, client, data):
        return client.post(
            "/income/secondary/record",
            data=data,
            headers={"x-csrftoken": client.csrf_token},
        )

    def test_creates_income_transaction(
        self,
        authed_client,
        db_session,
        secondary_income_allocation,
        sample_income_category,
        transfer_category,
    ):
        response = self._record(authed_client, {"amount": "500", "date": "2026-05-10"})
        assert response.status_code == 200

        txns = db_session.query(Transaction).filter(Transaction.type == "income").all()
        assert len(txns) == 1
        assert abs(float(txns[0].amount) - 500.0) < 0.01

    def test_funds_goals_in_priority_order(
        self,
        authed_client,
        db_session,
        secondary_income_allocation,
        sample_income_category,
        transfer_category,
        sample_sinking_funds,
    ):
        # Goals: fund0=$600 (priority 1), fund1=$400 (priority 2). Income=$1000 → both fully funded.
        self._record(authed_client, {"amount": "1000", "date": "2026-05-10"})

        alloc_txns = (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "income_allocation")
            .all()
        )
        assert len(alloc_txns) == 2
        amounts = sorted(float(t.amount) for t in alloc_txns)
        assert abs(amounts[0] - 400.0) < 0.01
        assert abs(amounts[1] - 600.0) < 0.01

    def test_increments_sinking_fund_balances(
        self,
        authed_client,
        db_session,
        secondary_income_allocation,
        sample_income_category,
        transfer_category,
        sample_sinking_funds,
    ):
        self._record(authed_client, {"amount": "1000", "date": "2026-05-10"})

        db_session.expire_all()
        f1 = db_session.get(SinkingFund, sample_sinking_funds[0].id)
        f2 = db_session.get(SinkingFund, sample_sinking_funds[1].id)
        assert abs(float(f1.current_balance) - 600.0) < 0.01
        assert abs(float(f2.current_balance) - 400.0) < 0.01

    def test_shortfall_funds_priority_order(
        self,
        authed_client,
        db_session,
        secondary_income_allocation,
        sample_income_category,
        transfer_category,
        sample_sinking_funds,
    ):
        # Goals: fund0=$600 (p1), fund1=$400 (p2). Income=$700 → fund0 gets $600, fund1 gets $100.
        self._record(authed_client, {"amount": "700", "date": "2026-05-10"})

        db_session.expire_all()
        f1 = db_session.get(SinkingFund, sample_sinking_funds[0].id)
        f2 = db_session.get(SinkingFund, sample_sinking_funds[1].id)
        assert abs(float(f1.current_balance) - 600.0) < 0.01
        assert abs(float(f2.current_balance) - 100.0) < 0.01

    def test_surplus_goes_to_overflow_fund(
        self,
        authed_client,
        db_session,
        secondary_income_allocation,
        sample_income_category,
        transfer_category,
        sample_sinking_funds,
    ):
        # Goals total $1000. Income $1500 → $500 surplus to overflow fund.
        overflow_fund = SinkingFund(name="Overflow", color="#cccccc", current_balance=0)
        db_session.add(overflow_fund)
        db_session.flush()
        secondary_income_allocation.overflow_sinking_fund_id = overflow_fund.id
        db_session.commit()

        self._record(authed_client, {"amount": "1500", "date": "2026-05-10"})

        db_session.expire_all()
        overflow_fund = db_session.get(SinkingFund, overflow_fund.id)
        assert abs(float(overflow_fund.current_balance) - 500.0) < 0.01

        unalloc = db_session.query(MonthlyUnallocatedIncome).first()
        assert unalloc is None or abs(float(unalloc.unallocated_amount)) < 0.01

    def test_surplus_without_overflow_fund_goes_to_unallocated(
        self,
        authed_client,
        db_session,
        secondary_income_allocation,
        sample_income_category,
        transfer_category,
    ):
        # Goals total $1000. Income $1200. No overflow fund → $200 to unallocated.
        self._record(authed_client, {"amount": "1200", "date": "2026-05-10"})

        unalloc = db_session.query(MonthlyUnallocatedIncome).first()
        assert unalloc is not None
        assert abs(float(unalloc.unallocated_amount) - 200.0) < 0.01

    def test_partial_allocation_adds_remainder(
        self,
        authed_client,
        db_session,
        sample_income_category,
        transfer_category,
        sample_sinking_funds,
    ):
        # Single $300 goal, income $500, no overflow fund → $200 to unallocated.
        alloc = SecondaryIncomeAllocation(label="Partial")
        db_session.add(alloc)
        db_session.flush()
        db_session.add(
            SecondaryIncomeAllocationRule(
                secondary_income_allocation_id=alloc.id,
                sinking_fund_id=sample_sinking_funds[0].id,
                goal_amount=300.0,
                sort_order=1,
            )
        )
        db_session.commit()

        self._record(authed_client, {"amount": "500", "date": "2026-05-10"})

        unalloc = db_session.query(MonthlyUnallocatedIncome).first()
        assert unalloc is not None
        assert abs(float(unalloc.unallocated_amount) - 200.0) < 0.01

    def test_no_config_returns_error(
        self, authed_client, sample_income_category, transfer_category
    ):
        response = self._record(authed_client, {"amount": "500", "date": "2026-05-10"})
        assert response.status_code == 200
        assert "text-red" in response.text

    def test_invalid_amount_rejected(
        self,
        authed_client,
        secondary_income_allocation,
        sample_income_category,
        transfer_category,
    ):
        response = self._record(authed_client, {"amount": "0", "date": "2026-05-10"})
        assert response.status_code == 200
        assert "text-red" in response.text

    def test_csrf_required(
        self, authed_client, secondary_income_allocation, sample_income_category
    ):
        response = authed_client.post(
            "/income/secondary/record", data={"amount": "500", "date": "2026-05-10"}
        )
        assert response.status_code == 403


class TestSecondaryIncomeAPI:
    def test_get_returns_404_when_no_config(self, authed_client):
        response = authed_client.get("/api/income/secondary")
        assert response.status_code == 404

    def test_post_creates_config(self, authed_client, db_session, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.post(
            "/api/income/secondary",
            json={
                "label": "API Partner",
                "rules": [
                    {"sinking_fund_id": fund.id, "goal_amount": "500", "sort_order": 1}
                ],
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["label"] == "API Partner"
        assert len(data["rules"]) == 1
        assert abs(float(data["rules"][0]["goal_amount"]) - 500.0) < 0.01

    def test_post_updates_config(
        self,
        authed_client,
        db_session,
        secondary_income_allocation,
        sample_sinking_funds,
    ):
        fund = sample_sinking_funds[0]
        response = authed_client.post(
            "/api/income/secondary",
            json={
                "label": "Updated",
                "rules": [
                    {"sinking_fund_id": fund.id, "goal_amount": "800", "sort_order": 1}
                ],
            },
        )
        assert response.status_code == 200

    def test_post_saves_overflow_fund(
        self, authed_client, db_session, sample_sinking_funds
    ):
        f1, f2 = sample_sinking_funds
        response = authed_client.post(
            "/api/income/secondary",
            json={
                "label": "X",
                "rules": [
                    {"sinking_fund_id": f1.id, "goal_amount": "300", "sort_order": 1}
                ],
                "overflow_sinking_fund_id": f2.id,
            },
        )
        assert response.status_code == 201
        assert response.json()["overflow_sinking_fund_id"] == f2.id

    def test_record_creates_distributions(
        self,
        authed_client,
        db_session,
        secondary_income_allocation,
        sample_income_category,
        transfer_category,
    ):
        response = authed_client.post(
            "/api/income/secondary/record",
            json={"amount": "1000", "date": "2026-05-10"},
        )
        assert response.status_code == 201
        data = response.json()
        assert len(data["distributions"]) == 2

    def test_record_no_config_returns_400(
        self, authed_client, sample_income_category, transfer_category
    ):
        response = authed_client.post(
            "/api/income/secondary/record",
            json={"amount": "500", "date": "2026-05-10"},
        )
        assert response.status_code == 400

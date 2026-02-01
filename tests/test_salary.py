import json

from app.models import SalaryAllocation, SalaryAllocationToSinkingFund, SinkingFund


class TestSalaryPageGet:
    def test_renders_form_with_expected_inputs(self, authed_client):
        response = authed_client.get("/salary")
        assert response.status_code == 200
        assert 'name="monthly_salary_amount"' in response.text
        assert 'name="monthly_budget_allocation"' in response.text
        assert 'name="bills_fund_allocation_type"' in response.text

    def test_shows_sinking_fund_inputs(self, authed_client, sample_sinking_funds):
        response = authed_client.get("/salary")
        assert response.status_code == 200
        for fund in sample_sinking_funds:
            assert f'name="fund_{fund.id}"' in response.text
            assert fund.name in response.text

    def test_prefills_values_from_existing_allocation(self, authed_client, db_session):
        alloc = SalaryAllocation(
            monthly_salary_amount=5000,
            monthly_budget_allocation=2000,
            bills_fund_allocation_type="fixed",
            bills_fund_fixed_amount=800,
        )
        db_session.add(alloc)
        db_session.commit()

        response = authed_client.get("/salary")
        assert "5000" in response.text
        assert "2000" in response.text
        assert "800" in response.text

    def test_prefills_sinking_fund_allocation_amounts(
        self, authed_client, db_session, sample_sinking_funds
    ):
        alloc = SalaryAllocation(
            monthly_salary_amount=5000,
            monthly_budget_allocation=2000,
            bills_fund_allocation_type="recommended",
        )
        db_session.add(alloc)
        db_session.flush()
        junction = SalaryAllocationToSinkingFund(
            salary_allocation_id=alloc.id,
            sinking_fund_id=sample_sinking_funds[0].id,
            allocation_amount=300,
        )
        db_session.add(junction)
        db_session.commit()

        response = authed_client.get("/salary")
        assert "300" in response.text

    def test_shows_message_when_no_sinking_funds(self, authed_client):
        response = authed_client.get("/salary")
        assert "No active sinking funds" in response.text

    def test_excludes_soft_deleted_sinking_funds(self, authed_client, db_session):
        active = SinkingFund(
            name="Active Fund", color="#00FF00", monthly_allocation=0, current_balance=0
        )
        deleted = SinkingFund(
            name="Deleted Fund",
            color="#FF0000",
            monthly_allocation=0,
            current_balance=0,
            is_deleted=True,
        )
        db_session.add_all([active, deleted])
        db_session.commit()

        response = authed_client.get("/salary")
        assert "Active Fund" in response.text
        assert "Deleted Fund" not in response.text

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/salary", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_sankey_container_rendered(self, authed_client):
        response = authed_client.get("/salary")
        assert response.status_code == 200
        assert 'id="sankey-chart"' in response.text

    def test_d3_scripts_loaded(self, authed_client):
        response = authed_client.get("/salary")
        assert response.status_code == 200
        assert "https://d3js.org/d3.v7.min.js" in response.text
        assert "https://unpkg.com/d3-sankey@0.12.3/dist/d3-sankey.min.js" in response.text

    def test_fund_metadata_in_page(self, authed_client, db_session):
        funds = [
            SinkingFund(name="Emergency", color="#EF4444", monthly_allocation=0, current_balance=0),
            SinkingFund(name="Holiday", color="#8B5CF6", monthly_allocation=0, current_balance=0),
        ]
        db_session.add_all(funds)
        db_session.commit()

        response = authed_client.get("/salary")
        assert response.status_code == 200
        assert "FUND_META" in response.text
        assert "Emergency" in response.text
        assert "Holiday" in response.text
        assert "#EF4444" in response.text
        assert "#8B5CF6" in response.text


class TestSalaryPagePost:
    def test_creates_new_allocation(self, authed_client, db_session):
        response = authed_client.post(
            "/salary",
            data={
                "monthly_salary_amount": "5000",
                "monthly_budget_allocation": "2000",
                "bills_fund_allocation_type": "recommended",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "saved successfully" in response.text

        alloc = db_session.query(SalaryAllocation).first()
        assert alloc is not None
        assert float(alloc.monthly_salary_amount) == 5000.0
        assert float(alloc.monthly_budget_allocation) == 2000.0

    def test_updates_existing_allocation(self, authed_client, db_session):
        # Create initial
        alloc = SalaryAllocation(
            monthly_salary_amount=5000,
            monthly_budget_allocation=2000,
            bills_fund_allocation_type="recommended",
        )
        db_session.add(alloc)
        db_session.commit()

        # Update via POST
        response = authed_client.post(
            "/salary",
            data={
                "monthly_salary_amount": "6000",
                "monthly_budget_allocation": "2500",
                "bills_fund_allocation_type": "recommended",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "saved successfully" in response.text

        # Verify upsert (not duplicate)
        count = db_session.query(SalaryAllocation).count()
        assert count == 1
        db_session.refresh(alloc)
        assert float(alloc.monthly_salary_amount) == 6000.0

    def test_saves_sinking_fund_allocations(
        self, authed_client, db_session, sample_sinking_funds
    ):
        response = authed_client.post(
            "/salary",
            data={
                "monthly_salary_amount": "5000",
                "monthly_budget_allocation": "2000",
                "bills_fund_allocation_type": "recommended",
                f"fund_{sample_sinking_funds[0].id}": "300",
                f"fund_{sample_sinking_funds[1].id}": "500",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200

        junctions = db_session.query(SalaryAllocationToSinkingFund).all()
        assert len(junctions) == 2
        amounts = {j.sinking_fund_id: float(j.allocation_amount) for j in junctions}
        assert amounts[sample_sinking_funds[0].id] == 300.0
        assert amounts[sample_sinking_funds[1].id] == 500.0

    def test_replaces_junction_rows_on_update(
        self, authed_client, db_session, sample_sinking_funds
    ):
        # Create initial with fund allocation
        alloc = SalaryAllocation(
            monthly_salary_amount=5000,
            monthly_budget_allocation=2000,
            bills_fund_allocation_type="recommended",
        )
        db_session.add(alloc)
        db_session.flush()
        db_session.add(
            SalaryAllocationToSinkingFund(
                salary_allocation_id=alloc.id,
                sinking_fund_id=sample_sinking_funds[0].id,
                allocation_amount=300,
            )
        )
        db_session.commit()

        # Update — only allocate to second fund
        authed_client.post(
            "/salary",
            data={
                "monthly_salary_amount": "5000",
                "monthly_budget_allocation": "2000",
                "bills_fund_allocation_type": "recommended",
                f"fund_{sample_sinking_funds[1].id}": "700",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )

        junctions = db_session.query(SalaryAllocationToSinkingFund).all()
        assert len(junctions) == 1
        assert junctions[0].sinking_fund_id == sample_sinking_funds[1].id
        assert float(junctions[0].allocation_amount) == 700.0

    def test_skips_zero_and_empty_fund_allocations(
        self, authed_client, db_session, sample_sinking_funds
    ):
        authed_client.post(
            "/salary",
            data={
                "monthly_salary_amount": "5000",
                "monthly_budget_allocation": "2000",
                "bills_fund_allocation_type": "recommended",
                f"fund_{sample_sinking_funds[0].id}": "0",
                f"fund_{sample_sinking_funds[1].id}": "",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )

        junctions = db_session.query(SalaryAllocationToSinkingFund).all()
        assert len(junctions) == 0

    def test_error_when_salary_zero(self, authed_client):
        response = authed_client.post(
            "/salary",
            data={
                "monthly_salary_amount": "0",
                "monthly_budget_allocation": "2000",
                "bills_fund_allocation_type": "recommended",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Salary must be greater than zero" in response.text

    def test_error_when_fixed_type_no_amount(self, authed_client):
        response = authed_client.post(
            "/salary",
            data={
                "monthly_salary_amount": "5000",
                "monthly_budget_allocation": "2000",
                "bills_fund_allocation_type": "fixed",
                "bills_fund_fixed_amount": "",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Fixed amount is required" in response.text

    def test_stores_none_for_fixed_amount_when_recommended(self, authed_client, db_session):
        authed_client.post(
            "/salary",
            data={
                "monthly_salary_amount": "5000",
                "monthly_budget_allocation": "2000",
                "bills_fund_allocation_type": "recommended",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        alloc = db_session.query(SalaryAllocation).first()
        assert alloc.bills_fund_fixed_amount is None

    def test_403_when_csrf_missing(self, authed_client):
        response = authed_client.post(
            "/salary",
            data={
                "monthly_salary_amount": "5000",
                "monthly_budget_allocation": "2000",
                "bills_fund_allocation_type": "recommended",
            },
        )
        assert response.status_code == 403


class TestApiGetSalary:
    def test_returns_json_when_allocation_exists(self, authed_client, db_session):
        alloc = SalaryAllocation(
            monthly_salary_amount=5000,
            monthly_budget_allocation=2000,
            bills_fund_allocation_type="recommended",
        )
        db_session.add(alloc)
        db_session.commit()

        response = authed_client.get("/api/salary")
        assert response.status_code == 200
        data = response.json()
        assert float(data["monthly_salary_amount"]) == 5000.0
        assert float(data["monthly_budget_allocation"]) == 2000.0
        assert data["bills_fund_allocation_type"] == "recommended"

    def test_returns_404_when_no_allocation(self, authed_client):
        response = authed_client.get("/api/salary")
        assert response.status_code == 404

    def test_includes_sinking_fund_allocations(
        self, authed_client, db_session, sample_sinking_funds
    ):
        alloc = SalaryAllocation(
            monthly_salary_amount=5000,
            monthly_budget_allocation=2000,
            bills_fund_allocation_type="recommended",
        )
        db_session.add(alloc)
        db_session.flush()
        db_session.add(
            SalaryAllocationToSinkingFund(
                salary_allocation_id=alloc.id,
                sinking_fund_id=sample_sinking_funds[0].id,
                allocation_amount=300,
            )
        )
        db_session.commit()

        response = authed_client.get("/api/salary")
        data = response.json()
        assert len(data["sinking_fund_allocations"]) == 1
        assert data["sinking_fund_allocations"][0]["sinking_fund_id"] == sample_sinking_funds[0].id

    def test_unauthenticated_redirects(self, client):
        response = client.get("/api/salary", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestApiPostSalary:
    def test_creates_allocation_returns_201(self, authed_client, db_session):
        response = authed_client.post(
            "/api/salary",
            json={
                "monthly_salary_amount": "5000",
                "monthly_budget_allocation": "2000",
                "bills_fund_allocation_type": "recommended",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 201
        data = response.json()
        assert float(data["monthly_salary_amount"]) == 5000.0

    def test_updates_existing_returns_200(self, authed_client, db_session):
        alloc = SalaryAllocation(
            monthly_salary_amount=5000,
            monthly_budget_allocation=2000,
            bills_fund_allocation_type="recommended",
        )
        db_session.add(alloc)
        db_session.commit()

        response = authed_client.post(
            "/api/salary",
            json={
                "monthly_salary_amount": "6000",
                "monthly_budget_allocation": "2500",
                "bills_fund_allocation_type": "recommended",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert float(response.json()["monthly_salary_amount"]) == 6000.0

        count = db_session.query(SalaryAllocation).count()
        assert count == 1

    def test_saves_sinking_fund_allocations(
        self, authed_client, db_session, sample_sinking_funds
    ):
        response = authed_client.post(
            "/api/salary",
            json={
                "monthly_salary_amount": "5000",
                "monthly_budget_allocation": "2000",
                "bills_fund_allocation_type": "recommended",
                "sinking_fund_allocations": [
                    {
                        "sinking_fund_id": sample_sinking_funds[0].id,
                        "allocation_amount": "300",
                    }
                ],
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 201
        data = response.json()
        assert len(data["sinking_fund_allocations"]) == 1

    def test_422_on_validation_error(self, authed_client):
        response = authed_client.post(
            "/api/salary",
            json={
                "monthly_salary_amount": "-1",
                "monthly_budget_allocation": "2000",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 422

    def test_403_without_csrf(self, authed_client):
        response = authed_client.post(
            "/api/salary",
            json={
                "monthly_salary_amount": "5000",
                "monthly_budget_allocation": "2000",
            },
        )
        assert response.status_code == 403

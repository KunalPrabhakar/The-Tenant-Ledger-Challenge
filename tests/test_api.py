from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import Base, create_database_engine
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    database_engine = create_database_engine(f"sqlite:///{tmp_path / 'test.db'}")
    factory = sessionmaker(bind=database_engine, autoflush=False, expire_on_commit=False)
    application = create_app(factory, database_engine)
    with TestClient(application) as test_client:
        yield test_client
    database_engine.dispose()


def create_tenant(client, name="Northbridge"):
    response = client.post("/api/v1/tenants", json={"name": name})
    assert response.status_code == 201
    return response.json()


def headers(api_key):
    return {"X-Tenant-Key": api_key}


def transaction_row(external_id="tx-1", **overrides):
    row = {
        "external_id": external_id,
        "kind": "deposit",
        "amount": "25.00",
        "currency": "USD",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "client_ref": "client-opaque-1",
    }
    row.update(overrides)
    return row


def balance(client, api_key, client_ref="client-opaque-1", as_of=None):
    params = {"as_of": as_of} if as_of else {}
    return client.get(
        f"/api/v1/clients/{client_ref}/balance",
        headers=headers(api_key),
        params=params,
    ).json()


def test_tenant_key_is_required_and_data_is_isolated(client):
    tenant_a = create_tenant(client, "Firm A")
    tenant_b = create_tenant(client, "Firm B")

    assert client.get("/api/v1/clients/client-opaque-1/balance").status_code == 422
    assert client.get(
        "/api/v1/clients/client-opaque-1/balance",
        headers=headers("not-a-valid-key"),
    ).status_code == 401

    posted = client.post(
        "/api/v1/transactions/import",
        headers=headers(tenant_a["api_key"]),
        json={"rows": [transaction_row()]},
    ).json()["results"][0]
    assert posted["status"] == "created"

    assert balance(client, tenant_a["api_key"])["balances"] == [
        {"currency": "USD", "amount": "25.00"}
    ]
    assert balance(client, tenant_b["api_key"])["balances"] == []


def test_batch_import_is_partial_and_idempotent(client):
    tenant = create_tenant(client)
    valid = transaction_row()
    rows = [
        valid,
        valid,
        transaction_row("bad-amount", amount="-2.00"),
        transaction_row("bad-currency", currency="XYZ"),
        transaction_row(
            "future",
            occurred_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        ),
    ]
    response = client.post(
        "/api/v1/transactions/import",
        headers=headers(tenant["api_key"]),
        json={"rows": rows},
    )
    assert response.status_code == 200
    payload = response.json()
    assert [row["status"] for row in payload["results"]] == [
        "created", "duplicate", "rejected", "rejected", "rejected"
    ]
    assert payload["counts"] == {"created": 1, "duplicate": 1, "rejected": 3}


def test_transfer_posts_balanced_entries_and_reversal_preserves_history(client):
    tenant = create_tenant(client)
    transfer = transaction_row(
        "transfer-1",
        kind="transfer",
        amount="12.50",
        from_client_ref="source",
        to_client_ref="destination",
    )
    transfer.pop("client_ref")
    result = client.post(
        "/api/v1/transactions/import",
        headers=headers(tenant["api_key"]),
        json={"rows": [transfer]},
    ).json()["results"][0]
    transaction_id = result["transaction_id"]

    assert balance(client, tenant["api_key"], "source")["balances"] == [
        {"currency": "USD", "amount": "-12.50"}
    ]
    assert balance(client, tenant["api_key"], "destination")["balances"] == [
        {"currency": "USD", "amount": "12.50"}
    ]

    reversed_response = client.post(
        f"/api/v1/transactions/{transaction_id}/reverse",
        headers=headers(tenant["api_key"]),
        json={"external_id": "reverse-1", "reason": "Wrong client instruction"},
    )
    assert reversed_response.status_code == 200
    assert balance(client, tenant["api_key"], "source")["balances"] == [
        {"currency": "USD", "amount": "0.00"}
    ]
    history = client.get(
        f"/api/v1/clients/source/transactions", headers=headers(tenant["api_key"])
    ).json()["transactions"]
    assert [item["kind"] for item in history] == ["transfer", "reversal"]
    audit = client.get(
        f"/api/v1/transactions/{transaction_id}/audit",
        headers=headers(tenant["api_key"]),
    ).json()["events"]
    assert [event["action"] for event in audit] == [
        "transaction_posted", "transaction_reversed"
    ]
    assert audit[-1]["reason"] == "Wrong client instruction"


def test_as_of_balance_and_monthly_summary_are_currency_aware(client):
    tenant = create_tenant(client)
    def past(at):
        return at.replace(tzinfo=timezone.utc).isoformat()

    rows = [
        transaction_row(
            "jan-deposit",
            amount="100.00",
            occurred_at=past(datetime(2025, 1, 15, 12)),
        ),
        transaction_row(
            "feb-deposit",
            amount="50.00",
            occurred_at=past(datetime(2025, 2, 1, 9)),
        ),
        transaction_row(
            "feb-withdrawal",
            kind="withdrawal",
            amount="20.00",
            occurred_at=past(datetime(2025, 2, 2, 9)),
        ),
        transaction_row(
            "feb-fee",
            kind="fee",
            amount="5.00",
            occurred_at=past(datetime(2025, 2, 3, 9)),
        ),
        transaction_row(
            "feb-eur",
            amount="10.00",
            currency="EUR",
            occurred_at=past(datetime(2025, 2, 4, 9)),
        ),
    ]
    response = client.post(
        "/api/v1/transactions/import",
        headers=headers(tenant["api_key"]),
        json={"rows": rows},
    )
    assert response.status_code == 200
    assert balance(client, tenant["api_key"], as_of="2025-01-31")["balances"] == [
        {"currency": "USD", "amount": "100.00"}
    ]

    summary = client.get(
        "/api/v1/clients/client-opaque-1/summary",
        headers=headers(tenant["api_key"]),
        params={"month": "2025-02"},
    ).json()
    assert summary["by_currency"] == [
        {"currency": "EUR", "inflows": "10.00", "outflows": "0.00", "fees": "0.00", "net": "10.00"},
        {"currency": "USD", "inflows": "50.00", "outflows": "25.00", "fees": "5.00", "net": "25.00"},
    ]


def test_monthly_summary_is_empty_without_entries(client):
    tenant = create_tenant(client)
    response = client.get(
        "/api/v1/clients/client-opaque-1/summary",
        headers=headers(tenant["api_key"]),
        params={"month": "2025-02"},
    )
    assert response.status_code == 200
    assert response.json()["by_currency"] == []


def test_reversal_is_once_only_and_tenant_scoped(client):
    tenant_a = create_tenant(client, "Firm A")
    tenant_b = create_tenant(client, "Firm B")
    posted = client.post(
        "/api/v1/transactions/import",
        headers=headers(tenant_a["api_key"]),
        json={"rows": [transaction_row()]},
    ).json()["results"][0]
    transaction_id = posted["transaction_id"]

    hidden = client.get(
        f"/api/v1/transactions/{transaction_id}/audit",
        headers=headers(tenant_b["api_key"]),
    )
    assert hidden.status_code == 404

    first = client.post(
        f"/api/v1/transactions/{transaction_id}/reverse",
        headers=headers(tenant_a["api_key"]),
        json={"external_id": "reverse-1", "reason": "Duplicate source entry"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/transactions/{transaction_id}/reverse",
        headers=headers(tenant_a["api_key"]),
        json={"external_id": "reverse-2", "reason": "Second attempt"},
    )
    assert second.status_code == 409
    assert balance(client, tenant_a["api_key"])["balances"] == [
        {"currency": "USD", "amount": "0.00"}
    ]


def test_amount_precision_currency_and_transfer_rules(client):
    tenant = create_tenant(client)
    rows = [
        transaction_row("precision", amount="1.001"),
        transaction_row("zero", amount="0"),
        transaction_row("unknown", currency="JPY"),
        transaction_row(
            "self-transfer",
            kind="transfer",
            from_client_ref="same",
            to_client_ref="same",
        ),
    ]
    rows[-1].pop("client_ref")
    response = client.post(
        "/api/v1/transactions/import",
        headers=headers(tenant["api_key"]),
        json={"rows": rows},
    )
    assert response.status_code == 200
    assert response.json()["counts"] == {"created": 0, "duplicate": 0, "rejected": 4}

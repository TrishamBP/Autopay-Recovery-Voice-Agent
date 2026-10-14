import httpx


async def test_health_is_public(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-API-Key": ""})
    assert response.status_code == 200
    assert response.json()["database"] == "ok"
    assert "x-request-id" in response.headers


async def test_customer_routes_require_operator_key(client: httpx.AsyncClient) -> None:
    for headers in ({"X-API-Key": ""}, {"X-API-Key": "nope"}):
        response = await client.get("/api/v1/customers", headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


async def test_list_customers_paginates(client: httpx.AsyncClient) -> None:
    page = (await client.get("/api/v1/customers", params={"limit": 1, "offset": 1})).json()
    assert page["total"] == 2 and page["limit"] == 1 and page["offset"] == 1
    assert [c["customer_id"] for c in page["items"]] == ["CUS-1002"]
    assert page["items"][0]["payment_amount"] == "499.00"
    assert (await client.get("/api/v1/customers", params={"limit": 0})).status_code == 422


async def test_customer_detail_masks_contact_data(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/customers/CUS-1001")).json()
    assert body["phone_number_masked"] == "+91******1001"
    assert "email" not in body and "payment_link_url" not in body and "phone_number" not in body
    assert body["summary"]["payment_reliability_score"] == 83
    assert body["summary"]["total_amount_paid_6m"] == "3995.00"
    assert body["last_payment_attempt_at"] == "2026-10-05T05:57:00Z"
    assert body["payment_due_date"] == "2026-10-05"


async def test_not_found_and_malformed_ids(client: httpx.AsyncClient) -> None:
    missing = await client.get("/api/v1/customers/CUS-9999")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert (await client.get("/api/v1/customers/robert'); DROP")).status_code in (404, 422)


async def test_payment_history_and_notes(client: httpx.AsyncClient) -> None:
    history = (await client.get("/api/v1/customers/CUS-1002/payment-history")).json()
    assert [h["billing_month"] for h in history][:2] == ["2026-10", "2026-09"]
    partial = next(h for h in history if h["status"] == "partially_paid")
    assert partial["amount_paid"] == "210.00" and partial["minimum_amount_paid"] is True
    notes = (await client.get("/api/v1/customers/CUS-1002/notes")).json()
    assert [n["note_id"] for n in notes] == ["NOTE-0002", "NOTE-0003"]


async def test_note_create_and_update_via_api(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/api/v1/customers/CUS-1001/notes",
        json={"note_type": "callback", "note_text": "Prefers 7 PM."},
    )
    assert created.status_code == 201
    note_id = created.json()["note_id"]
    updated = await client.patch(
        f"/api/v1/customers/CUS-1001/notes/{note_id}", json={"note_text": "Prefers 8 PM."}
    )
    assert updated.status_code == 200 and updated.json()["note_text"] == "Prefers 8 PM."
    wrong_owner = await client.patch(
        f"/api/v1/customers/CUS-1002/notes/{note_id}", json={"note_text": "hijack"}
    )
    assert wrong_owner.status_code == 404
    bad = await client.post(
        "/api/v1/customers/CUS-1001/notes",
        json={"note_type": "callback", "note_text": "card number 4111111111111111"},
    )
    assert bad.status_code == 422 and "4111" not in bad.text


async def test_database_summary(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/database/summary")).json()
    tables = {t["table"]: t["rows"] for t in body["tables"]}
    assert tables["customer"] == 2 and tables["payment_history"] == 12
    assert body["payment_history_by_status"]["partially_paid"] == 1
    assert body["customers_by_recovery_status"] == {"pending": 2}


async def test_validation_errors_do_not_echo_input(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/customers/CUS-1001/notes", json={"note_type": "SECRET-INPUT-123", "note_text": "x"}
    )
    assert response.status_code == 422
    assert "SECRET-INPUT-123" not in response.text
    assert response.json()["error"]["code"] == "request_validation_error"

"""Request bodies are for platform admins only, every read leaves a record, and tenants see their own bill."""
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.models import APIKey, Channel, ManagementEvent, ModelPrice, RequestLog, RequestLogAudit, Tenant, UsageDaily
from app.services import fast_billing
from app.services.auth import hash_api_key
from app.services.settings_service import set_inference_audit_enabled
from tests.conftest import auth_header, create_tenant_account

RAW_KEY = "sk-audit-privacy-key-0123456789"
SECRET = "ghp_" + "Z" * 32
USAGE = {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300}


@pytest.fixture
async def gateway():
    channel = await Channel.create(name="audit", provider="openai", base_url="https://audit-upstream.example.com",
                                   api_key="audit-upstream-secret", models=["gpt-4o"])
    await ModelPrice.create(model="gpt-4o", prompt_price=Decimal("2.5"), completion_price=Decimal(10))
    tenant, _, tenant_headers = await create_tenant_account()
    await Tenant.filter(id=tenant.id).update(quota_total=Decimal(100))
    key = await APIKey.create(tenant_id=tenant.id, name="=cmd|calc", key_hash=hash_api_key(RAW_KEY),
                              key_prefix=RAW_KEY[:8])
    provider = AsyncMock()
    provider.send_request = AsyncMock(return_value={"id": "1", "object": "chat.completion", "model": "gpt-4o",
                                                    "choices": [], "usage": USAGE})
    provider.apply_model_mapping = lambda model: model
    with patch("app.services.routing.resolve_with_fallbacks", return_value=[(channel, lambda _: provider)]):
        yield tenant, key, tenant_headers


async def _call(client):
    body = {"model": "gpt-4o", "max_tokens": 300,
            "messages": [{"role": "user", "content": f"deploy with {SECRET} please"}]}
    return await client.post("/v1/chat/completions", json=body, headers=auth_header(RAW_KEY))


async def test_stored_body_keeps_type_and_fingerprint_but_not_the_secret(client, gateway, admin_token):
    await set_inference_audit_enabled(True)
    assert (await _call(client)).status_code == 200
    stored = (await RequestLogAudit.get()).audit_request
    assert SECRET not in stored and "[REDACTED:github_token#" in stored

    log = await RequestLog.get()
    body = await client.get(f"/api/logs/{log.request_id}/audit", headers=auth_header(admin_token))
    assert body.status_code == 200
    [finding] = body.json()["findings"]
    assert finding["type"] == "github_token" and finding["path"] == "$.messages[0].content"
    assert len(finding["fingerprint"]) == 8 and finding["fingerprint"] in stored


async def test_tenants_get_404_for_bodies_scans_and_reports(client, gateway):
    await set_inference_audit_enabled(True)
    _, key, tenant_headers = gateway
    assert (await _call(client)).status_code == 200
    log = await RequestLog.get()
    for method, path, body in (
        ("get", "/api/logs", None),
        ("get", f"/api/logs/{log.request_id}", None),
        ("get", f"/api/logs/{log.request_id}/audit", None),
        ("get", "/api/audit/keys", None),
        ("get", f"/api/audit/keys/{key.id}/requests", None),
        ("post", "/api/audit/review", {"request_ids": [log.request_id]}),
        ("post", f"/api/audit/keys/{key.id}/report-downloads", {"request_ids": [log.request_id]}),
        ("get", "/api/audit/views", None),
    ):
        response = await client.request(method, path, json=body, headers=tenant_headers)
        assert response.status_code == 404, path
    assert not await ManagementEvent.filter(action__startswith="audit.").exists()


async def test_viewing_bodies_and_downloading_a_report_each_leave_one_record(client, gateway, admin_token):
    await set_inference_audit_enabled(True)
    tenant, key, _ = gateway
    for _ in range(2):
        assert (await _call(client)).status_code == 200
    headers = auth_header(admin_token)
    listed = await client.get(f"/api/audit/keys/{key.id}/requests", headers=headers)
    request_ids = [item["request_id"] for item in listed.json()["items"]]
    assert len(request_ids) == 2
    downloaded = await client.post(f"/api/audit/keys/{key.id}/report-downloads",
                                   json={"request_ids": request_ids}, headers=headers)
    assert downloaded.status_code == 201 and downloaded.json() == {"recorded": 2}

    events = await ManagementEvent.filter(action__startswith="audit.").order_by("id")
    assert [event.action for event in events] == ["audit.view", "audit.report_download"]
    assert all(event.target_id == key.id and event.tenant_id == tenant.id for event in events)
    assert events[1].detail["requests"] == 2 and events[1].detail["from"] <= events[1].detail["to"]
    views = (await client.get("/api/audit/views", headers=headers)).json()
    assert views["total"] == 2
    assert {item["actor"] for item in views["items"]} == {"testadmin"}
    assert views["items"][0]["action"] == "audit.report_download"
    assert views["items"][0]["api_key_name"] == key.name


@pytest.mark.queued_billing
async def test_bill_has_written_and_pending_spend_for_the_tenants_own_keys(client, gateway):
    tenant, key, tenant_headers = gateway
    await UsageDaily.create(day="2026-11-02", api_key_id=key.id, tenant_id=tenant.id, model="gpt-4o",
                            request_count=4, cost=Decimal("0.02"))
    await UsageDaily.create(day="2026-10-31", api_key_id=key.id, tenant_id=tenant.id, model="gpt-4o",
                            request_count=9, cost=Decimal("9"))
    assert (await _call(client)).status_code == 200
    assert await fast_billing.pending("key", key.id) == Decimal("0.0055")

    bill = await client.get("/api/keys/bill?start=2026-11-01&end=2026-11-30", headers=tenant_headers)
    assert bill.status_code == 200
    [row] = bill.json()["items"]
    assert (row["api_key_name"], row["request_count"]) == (key.name, 4)
    assert Decimal(str(row["written"])) == Decimal("0.02") and Decimal(str(row["pending"])) == Decimal("0.0055")

    exported = await client.get("/api/keys/bill.csv?start=2026-11-01&end=2026-11-30", headers=tenant_headers)
    assert exported.status_code == 200 and exported.headers["content-type"].startswith("text/csv")
    text = exported.text
    assert "并发请求可能一起越过额度" in text and "导出时刻" in text
    assert "'=cmd|calc" in text and "0.0055" in text
    for leaked in ("example.com", "audit-upstream-secret", RAW_KEY):
        assert leaked not in text


async def test_another_tenants_bill_is_not_found(client, gateway, admin_token):
    tenant, _, _ = gateway
    _, _, other_headers = await create_tenant_account()
    for path in (f"/api/keys/bill?tenant_id={tenant.id}", f"/api/keys/bill.csv?tenant_id={tenant.id}"):
        assert (await client.get(path, headers=other_headers)).status_code == 404
    assert (await client.get("/api/keys/bill", headers=auth_header(admin_token))).status_code == 400
    admin = await client.get(f"/api/keys/bill?tenant_id={tenant.id}", headers=auth_header(admin_token))
    assert admin.status_code == 200 and admin.json()["tenant"] == tenant.name
    too_long = await client.get("/api/keys/bill?start=2024-01-01&end=2026-01-01", headers=other_headers)
    assert too_long.status_code == 422

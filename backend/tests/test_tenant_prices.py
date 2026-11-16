"""Tenants see what they can call and what it costs, and which budget stopped them."""
from decimal import Decimal

from app.models import APIKey, Channel, ModelPrice, Tenant
from app.services.auth import hash_api_key
from tests.conftest import create_tenant_account


async def _channel(name, models, multiplier=1):
    return await Channel.create(name=name, provider="openai", base_url=f"https://{name}.example.com",
                                api_key=f"{name}-upstream-secret", models=models,
                                price_multiplier=Decimal(multiplier))


async def _key(tenant, name, **fields):
    return await APIKey.create(tenant_id=tenant.id, name=name, key_hash=hash_api_key("sk-" + name),
                               key_prefix=("sk-" + name)[:8], **fields)


async def test_tenant_prices_follow_model_scope_key_whitelist_and_channels(client):
    for model in ("gpt-a", "gpt-b", "gpt-c"):
        await ModelPrice.create(model=model, prompt_price=Decimal(2), completion_price=Decimal(4))
    cheap = await _channel("cheap", ["gpt-a", "gpt-b", "gpt-c"])
    await _channel("dear", ["gpt-a"], multiplier=3)
    tenant, _, headers = await create_tenant_account()
    await Tenant.filter(id=tenant.id).update(allowed_models=["gpt-a", "gpt-b"])

    listed = await client.get("/api/keys/models", headers=headers)
    assert listed.status_code == 200
    rows = {row["model"]: row for row in listed.json()}
    assert set(rows) == {"gpt-a", "gpt-b"}
    assert rows["gpt-a"]["prompt"] == 6 and rows["gpt-a"]["varies_by_route"] is True
    assert rows["gpt-b"]["completion"] == 4 and rows["gpt-b"]["varies_by_route"] is False
    assert "example.com" not in listed.text and "upstream-secret" not in listed.text

    narrow = await _key(tenant, "narrow", allowed_models=["gpt-a"], channel_ids=[cheap.id])
    scoped = await client.get(f"/api/keys/models?key_id={narrow.id}", headers=headers)
    assert [(row["model"], row["prompt"], row["varies_by_route"]) for row in scoped.json()] == [("gpt-a", 2, False)]


async def test_another_tenants_key_is_not_found(client):
    other, _, _ = await create_tenant_account()
    foreign = await _key(other, "foreign")
    _, _, headers = await create_tenant_account()
    assert (await client.get(f"/api/keys/models?key_id={foreign.id}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/keys/models?tenant_id={other.id}", headers=headers)).status_code == 403


async def test_used_up_budgets_are_listed_for_the_tenant(client):
    tenant, _, headers = await create_tenant_account()
    await Tenant.filter(id=tenant.id).update(quota_total=Decimal(5), quota_used=Decimal(5))
    await _key(tenant, "spent", quota_total=Decimal(1), quota_used=Decimal(1))
    await _key(tenant, "fresh", quota_total=Decimal(1))
    summary = await client.get("/api/keys/summary", headers=headers)
    assert summary.json()["exhausted"] == ["quota"]
    keys = {item["name"]: item["exhausted"] for item in (await client.get("/api/keys", headers=headers)).json()["items"]}
    assert keys == {"spent": ["quota"], "fresh": []}

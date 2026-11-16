"""Missing price, used-up quota and no channel each give the client a distinct, readable refusal."""
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.models import APIKey, Channel, ModelPrice, Tenant
from app.services import alerts
from app.services.auth import hash_api_key
from tests.conftest import create_test_tenant

RAW_KEY = "sk-refusal-reasons-key"
UPSTREAM_URL = "https://refusal-upstream.example.com"
UPSTREAM_SECRET = "sk-refusal-upstream-secret"
PROTOCOLS = {
    "openai": ("openai", "/v1/chat/completions",
               lambda model: {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
               {"Authorization": "Bearer " + RAW_KEY}),
    "anthropic": ("anthropic", "/v1/messages",
                  lambda model: {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
                  {"x-api-key": RAW_KEY, "anthropic-version": "2023-06-01"}),
}


async def _setup(protocol: str, model: str, *, priced=True, channel=True, key_quota="-1", tenant_quota="-1"):
    provider = PROTOCOLS[protocol][0]
    if channel:
        await Channel.create(name="refusal", provider=provider, base_url=UPSTREAM_URL, api_key=UPSTREAM_SECRET,
                             models=[model])
    if priced:
        await ModelPrice.create(model=model, prompt_price=Decimal(1), completion_price=Decimal(1))
    tenant = await create_test_tenant()
    await Tenant.filter(id=tenant.id).update(quota_total=Decimal(tenant_quota))
    return await APIKey.create(tenant_id=tenant.id, name="refusal-key", key_hash=hash_api_key(RAW_KEY),
                               key_prefix=RAW_KEY[:8], quota_total=Decimal(key_quota))


async def _call(client, protocol: str, model: str):
    _, path, body, headers = PROTOCOLS[protocol]
    http = AsyncMock()
    http.is_closed = False
    with patch("app.providers.base._shared_http_client", return_value=http):
        response = await client.post(path, json=body(model), headers=headers)
    http.post.assert_not_awaited()
    http.send.assert_not_awaited()
    assert UPSTREAM_URL not in response.text and UPSTREAM_SECRET not in response.text
    error = response.json()["error"]
    if protocol == "anthropic":
        assert response.json()["type"] == "error"
    return response.status_code, error


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_missing_price_names_the_model(client, protocol):
    await _setup(protocol, "unpriced-refusal-model", priced=False)
    status, error = await _call(client, protocol, "unpriced-refusal-model")
    assert (status, error["code"]) == (400, "model_not_priced")
    assert error["message"] == ("Model unpriced-refusal-model has no selling price; "
                                "ask the platform administrator to set one")


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("scope, quotas, code, message", [
    ("key", {"key_quota": "0"}, "quota_exceeded",
     "API key quota is used up; raise the key quota or wait for its reset"),
    ("tenant", {"tenant_quota": "0"}, "tenant_quota_exceeded",
     "Tenant quota is used up; ask the platform administrator to raise it"),
])
async def test_used_up_quota_says_which_budget_and_alerts_the_platform(client, protocol, scope, quotas, code, message):
    key = await _setup(protocol, "priced-refusal-model", **quotas)
    with patch.object(alerts, "notify", AsyncMock(return_value=True)) as notify:
        status, error = await _call(client, protocol, "priced-refusal-model")
    assert (status, error["code"], error["message"]) == (429, code, message)
    notify.assert_awaited_once()
    event, subject, title, text, data, _ = notify.await_args.args
    owner_id = key.tenant_id if scope == "tenant" else key.id
    assert event == "budget" and subject == f"exhausted:{scope}:{owner_id}:"
    assert "refusal-key" in text and data["key_id"] == key.id and data["budget"] == scope


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_no_channel_hides_upstream_details(client, protocol):
    await _setup(protocol, "orphan-refusal-model", channel=False)
    status, error = await _call(client, protocol, "orphan-refusal-model")
    assert (status, error["code"]) == (503, "no_available_channel")
    assert error["message"] == ("No channel is available for model orphan-refusal-model; "
                                "try again later or ask the platform administrator")

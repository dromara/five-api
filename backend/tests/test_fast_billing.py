"""BILLING_MODE=fast: admit against cached spend, queue the charge, write it to MySQL in batches."""
import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.models import APIKey, Channel, ModelPrice, RequestLog, RequestLogAudit, Settlement, Tenant, UsageDaily
from app.services import fast_billing
from app.services.auth import hash_api_key
from app.services.logging_service import save_request_log
from app.services.settings_service import set_inference_audit_enabled
from tests.conftest import auth_header

pytestmark = pytest.mark.fast_billing

RAW_KEY = "sk-fast-billing-key-0123456789"
USAGE = {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300}
# gpt-4o at 2.5 / 10 USD per million tokens: 1000 x 2.5 + 300 x 10 = 5500 micro-USD.
CHARGE = Decimal("0.0055")


@pytest.fixture
async def setup():
    channel = await Channel.create(name="fast", provider="openai", base_url="https://fast.example.com",
                                   api_key="k", models=["gpt-4o"])
    await ModelPrice.create(model="gpt-4o", prompt_price=Decimal("2.5"), completion_price=Decimal(10))
    tenant = await Tenant.create(name="fast-tenant", quota_total=Decimal(100), rpm_limit=-1, tpm_limit=-1,
                                 concurrent_limit=-1)
    key = await APIKey.create(tenant_id=tenant.id, name="fast", key_hash=hash_api_key(RAW_KEY),
                              key_prefix=RAW_KEY[:8], quota_total=Decimal(10))
    provider = AsyncMock()
    provider.send_request = AsyncMock(return_value={"id": "1", "object": "chat.completion", "model": "gpt-4o",
                                                    "choices": [], "usage": USAGE})
    provider.apply_model_mapping = lambda model: model
    with patch("app.services.routing.resolve_with_fallbacks", return_value=[(channel, lambda _: provider)]):
        yield tenant, key, provider


async def _call(client):
    body = {"model": "gpt-4o", "max_tokens": 300, "messages": [{"role": "user", "content": "hello"}]}
    return await client.post("/v1/chat/completions", json=body, headers=auth_header(RAW_KEY))


async def test_charge_is_queued_then_flushed_with_its_log(client, setup):
    tenant, key, _ = setup
    response = await _call(client)
    assert response.status_code == 200
    assert await RequestLog.all().count() == 0
    assert await fast_billing.pending("key", key.id) == CHARGE
    assert await fast_billing.pending("tenant", tenant.id) == CHARGE
    assert not await Settlement.exists()

    assert await fast_billing.flush_once() == 1
    await key.refresh_from_db()
    await tenant.refresh_from_db()
    log = await RequestLog.get()
    assert log.cost == key.quota_used == tenant.quota_used == CHARGE
    assert (await UsageDaily.get()).cost == CHARGE
    assert await fast_billing.pending("key", key.id) == 0
    assert await fast_billing.pending("tenant", tenant.id) == 0
    assert await fast_billing.backlog() == 0


async def test_one_flush_writes_the_batch_with_daily_totals_and_audit(client, setup):
    tenant, _, _ = setup
    await set_inference_audit_enabled(True)
    for _ in range(3):
        assert (await _call(client)).status_code == 200
    assert await fast_billing.flush_once() == 3
    daily = await UsageDaily.get()
    assert daily.request_count == 3 and daily.cost == 3 * CHARGE
    assert daily.prompt_tokens == 3 * USAGE["prompt_tokens"]
    assert await RequestLog.filter(has_audit=True).count() == 3
    assert await RequestLogAudit.all().count() == 3
    await tenant.refresh_from_db()
    assert tenant.quota_used == 3 * CHARGE


async def test_queued_spend_lowers_the_reported_remaining_quota(client, setup):
    assert (await _call(client)).status_code == 200
    me = (await client.get("/v1/me", headers=auth_header(RAW_KEY))).json()
    assert Decimal(str(me["quota_remaining"])) == Decimal(10) - CHARGE


async def test_queued_spend_counts_against_the_quota(client, setup):
    _, key, provider = setup
    await APIKey.filter(id=key.id).update(quota_total=CHARGE)
    assert (await _call(client)).status_code == 200
    # Not yet flushed: quota_used is still 0, but the pending charge already uses the whole quota.
    response = await _call(client)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "quota_exceeded"
    assert provider.send_request.await_count == 1


async def test_concurrent_admission_can_overspend(client, setup):
    _, key, provider = setup
    await APIKey.filter(id=key.id).update(quota_total=CHARGE)
    real_enqueue = fast_billing.enqueue
    held = []

    async def late_enqueue(*args):
        held.append(args)

    with patch.object(fast_billing, "enqueue", late_enqueue):
        first, second = await _call(client), await _call(client)
    for args in held:
        await real_enqueue(*args)
    await fast_billing.flush_once()
    await key.refresh_from_db()
    assert first.status_code == second.status_code == 200
    assert key.quota_used == 2 * CHARGE > key.quota_total


async def test_failed_flush_keeps_the_batch(client, setup, mock_redis):
    tenant, key, _ = setup
    assert (await _call(client)).status_code == 200
    with patch("app.services.logging_service.RequestLog.bulk_create", side_effect=RuntimeError("db down")), \
            pytest.raises(RuntimeError):
        await fast_billing.flush_once()
    assert await mock_redis.llen(fast_billing.QUEUE) == 1
    await key.refresh_from_db()
    assert key.quota_used == 0 and await fast_billing.pending("key", key.id) == CHARGE

    assert await fast_billing.flush_once() == 1
    await key.refresh_from_db()
    assert key.quota_used == CHARGE and await RequestLog.all().count() == 1


async def test_batch_of_an_exited_worker_is_returned_and_charged_once(client, setup, mock_redis):
    tenant, key, _ = setup
    assert (await _call(client)).status_code == 200
    assert (await _call(client)).status_code == 200
    # Another worker took both charges, committed the first, and exited before clearing its list.
    taken = await mock_redis.lpop(fast_billing.QUEUE, 10)
    await mock_redis.rpush(fast_billing.processing_key("exited-worker"), *taken)
    await save_request_log(**json.loads(taken[0])["payload"])
    await APIKey.filter(id=key.id).update(quota_used=CHARGE)
    await Tenant.filter(id=tenant.id).update(quota_used=CHARGE)
    assert await fast_billing.backlog() == 2

    assert await fast_billing.recover_orphans() == 2
    assert await fast_billing.flush_once() == 2
    await key.refresh_from_db()
    await tenant.refresh_from_db()
    assert key.quota_used == tenant.quota_used == 2 * CHARGE
    assert await RequestLog.all().count() == 2
    assert await fast_billing.pending("key", key.id) == 0
    assert await fast_billing.backlog() == 0


async def test_queue_failure_settles_directly(client, setup):
    tenant, key, _ = setup
    with patch.object(fast_billing, "enqueue", side_effect=ConnectionError("redis down")):
        assert (await _call(client)).status_code == 200
    await key.refresh_from_db()
    await tenant.refresh_from_db()
    assert key.quota_used == tenant.quota_used == CHARGE
    assert (await Settlement.get()).state == "settled"
    assert (await RequestLog.get()).cost == CHARGE

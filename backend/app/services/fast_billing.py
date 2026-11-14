"""Fast billing: admit requests against cached spend and write charges to MySQL in batches.

Charges wait in a Redis list until a worker flushes them. Until then their amounts sit in
per-key and per-tenant pending counters, which the admission check adds to quota_used.
Admission takes no lock, so concurrent requests can together pass the remaining balance.

A flushing worker moves its batch into its own processing list, so a batch it has taken but
not committed is still visible and survives the worker exiting.
"""
import asyncio
import json
import logging
from collections import defaultdict
from decimal import Decimal

from tortoise.expressions import F
from tortoise.transactions import in_transaction

from app import dependencies
from app.config import settings
from app.models import APIKey, RequestLog, Tenant
from app.services import cluster

logger = logging.getLogger(__name__)
QUEUE = "five:billing:queue"
PROCESSING_PREFIX = "five:billing:processing:"
PENDING_PREFIX = "five:billing:pending:"
MICRO = Decimal(1_000_000)
FLUSH_INTERVAL = 0.5
BATCH_SIZE = 500

TAKE_SCRIPT = """
local items = redis.call('LPOP', KEYS[1], ARGV[1])
if not items then return {} end
redis.call('RPUSH', KEYS[2], unpack(items))
return items
"""
# Back to the head of the queue in the original order.
RETURN_SCRIPT = """
local items = redis.call('LRANGE', KEYS[2], 0, -1)
for i = #items, 1, -1 do redis.call('LPUSH', KEYS[1], items[i]) end
redis.call('DEL', KEYS[2])
return #items
"""


def enabled() -> bool:
    return settings.BILLING_MODE != "strict"


def pending_key(scope: str, owner_id: int) -> str:
    return f"{PENDING_PREFIX}{scope}:{owner_id}"


def processing_key(owner: str = cluster.OWNER_ID) -> str:
    return PROCESSING_PREFIX + owner


def _micro(amount: Decimal) -> int:
    return int(amount * MICRO)


async def pending(scope: str, owner_id: int) -> Decimal:
    r = await dependencies.get_redis()
    return Decimal(int(await r.get(pending_key(scope, owner_id)) or 0)) / MICRO


async def backlog() -> int:
    """Charges queued or taken by a worker but not yet committed."""
    r = await dependencies.get_redis()
    total = await r.llen(QUEUE)
    async for key in r.scan_iter(match=PROCESSING_PREFIX + "*"):
        total += await r.llen(key)
    return total


async def over_budget(api_key: APIKey, tenant: Tenant) -> str | None:
    """The first scope whose flushed plus pending spend has reached its quota."""
    owners = [(scope, owner) for scope, owner in (("tenant", tenant), ("key", api_key)) if owner.quota_total != -1]
    if not owners:
        return None
    r = await dependencies.get_redis()
    values = await r.mget([pending_key(scope, owner.id) for scope, owner in owners])
    for (scope, owner), value in zip(owners, values):
        if owner.quota_used + Decimal(int(value or 0)) / MICRO >= owner.quota_total:
            return scope
    return None


async def enqueue(payload: dict, cost: Decimal, api_key_id: int, tenant_id: int) -> None:
    r = await dependencies.get_redis()
    pipe = r.pipeline(transaction=False)
    pipe.rpush(QUEUE, json.dumps({"payload": payload, "cost": str(cost)}, default=str))
    micro = _micro(cost)
    if micro > 0:
        pipe.incrby(pending_key("key", api_key_id), micro)
        pipe.incrby(pending_key("tenant", tenant_id), micro)
    await pipe.execute()


async def flush_once(limit: int = BATCH_SIZE) -> int:
    from app.services.logging_service import observe_request_logs, save_request_logs
    from app.services.spend_windows import record_settlement_spends

    r = await dependencies.get_redis()
    mine = processing_key()
    # Leftovers from a failed attempt of this worker go back first so they are not taken twice.
    await r.eval(RETURN_SCRIPT, 2, QUEUE, mine)
    raw = await r.eval(TAKE_SCRIPT, 2, QUEUE, mine, limit)
    if not raw:
        return 0
    items = [(item["payload"], Decimal(item["cost"])) for item in map(json.loads, raw)]
    # Pending counters drop by every taken charge; quota_used grows only by charges logged now.
    queued = {"key": defaultdict(Decimal), "tenant": defaultdict(Decimal)}
    charged = {"key": defaultdict(Decimal), "tenant": defaultdict(Decimal)}
    fresh = []
    try:
        # A worker that exited after committing but before clearing its list leaves charges already logged.
        logged = set(await RequestLog.filter(request_id__in=[payload["request_id"] for payload, _ in items])
                     .values_list("request_id", flat=True))
        for payload, cost in items:
            owners = (("key", payload["api_key_id"]), ("tenant", payload["tenant_id"]))
            for scope, owner_id in owners:
                queued[scope][owner_id] += cost
            if payload["request_id"] not in logged:
                fresh.append((payload, cost))
                for scope, owner_id in owners:
                    charged[scope][owner_id] += cost
        async with in_transaction():
            logs = await save_request_logs([payload for payload, _ in fresh])
            # Same lock order as strict settlement: tenants, then keys, each by id.
            for model, scope in ((Tenant, "tenant"), (APIKey, "key")):
                for owner_id, amount in sorted(charged[scope].items()):
                    if amount:
                        await model.filter(id=owner_id).update(quota_used=F("quota_used") + amount)
    except BaseException:
        await r.eval(RETURN_SCRIPT, 2, QUEUE, mine)
        raise
    pipe = r.pipeline(transaction=False)
    pipe.delete(mine)
    for scope, totals in queued.items():
        for owner_id, amount in totals.items():
            if _micro(amount) > 0:
                pipe.decrby(pending_key(scope, owner_id), _micro(amount))
    await pipe.execute()
    await observe_request_logs(logs)
    try:
        await record_settlement_spends([(log.request_id, log.api_key_id, log.tenant_id, cost, log.created_at)
                                        for log, (_, cost) in zip(logs, fresh) if cost > 0])
    except Exception:
        logger.exception("Window observation failed for %d charges", len(logs))
    return len(items)


async def recover_orphans() -> int:
    """Return batches taken by workers whose heartbeat has expired."""
    r = await dependencies.get_redis()
    owners = [key[len(PROCESSING_PREFIX):] async for key in r.scan_iter(match=PROCESSING_PREFIX + "*")]
    owners = [owner for owner in owners if owner != cluster.OWNER_ID]
    if not owners:
        return 0
    live = await cluster.live_owners(owners)
    returned = 0
    for owner in owners:
        if owner not in live:
            returned += await r.eval(RETURN_SCRIPT, 2, QUEUE, processing_key(owner))
    if returned:
        logger.warning("Returned %d queued charges from exited workers", returned)
    return returned


async def drain(timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while await flush_once():
            pass


async def flush_loop() -> None:
    while True:
        try:
            if await cluster.claim("spend-orphans", settings.PROCESS_STALE_SECONDS):
                await recover_orphans()
            if not await flush_once():
                await asyncio.sleep(FLUSH_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Spend flush failed; batch kept for retry")
            await asyncio.sleep(FLUSH_INTERVAL)

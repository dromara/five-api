"""Durable accounting records for charges that cannot go through the spend queue.

Unpriced usage, charges the queue rejected, internal calls and upstream outcomes nobody can
know are kept here and settled in one SQL transaction with the request log. Occupied amounts
are mirrored in the fast_billing held counters, so admission keeps counting them until the
record settles or an operator resolves it. Recovery rebuilds the counters from these rows.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tortoise.expressions import F
from tortoise.functions import Sum
from tortoise.transactions import in_transaction

from app.config import settings
from app.models import APIKey, Settlement, Tenant
from app.services import cluster, fast_billing
from app.services.cluster import OWNER_ID
from app.services.logging_service import save_request_log, observe_request_log
from app.services.redaction import redact

logger = logging.getLogger(__name__)
RECOVERY_INTERVAL = 2
OCCUPIED_STATES = ("reserved", "pending", "failed", "uncertain")
MAX_ATTEMPTS = 8
# Fields that older releases wrote into pending payloads and request logs no longer have.
LEGACY_PAYLOAD_FIELDS = frozenset({"upstream_cost"})
_unsaved: dict[str, tuple[dict, Decimal | None, str]] = {}


async def occupied(scope: str, owner_id: int) -> Decimal:
    field = "tenant_id" if scope == "tenant" else "api_key_id"
    rows = await Settlement.filter(**{field: owner_id}, state__in=OCCUPIED_STATES).annotate(
        amount=Sum("occupied"),
    ).values("amount")
    return Decimal(str(rows[0]["amount"] or 0)) if rows else Decimal(0)


async def sync_holds() -> None:
    amounts: dict[tuple[str, int], Decimal] = {}
    for scope, field in (("key", "api_key_id"), ("tenant", "tenant_id")):
        rows = await Settlement.filter(state__in=OCCUPIED_STATES).annotate(
            amount=Sum("occupied"),
        ).group_by(field).values(field, "amount")
        for row in rows:
            amounts[(scope, row[field])] = Decimal(str(row["amount"] or 0))
    await fast_billing.set_holds(amounts)


async def hold_unknown(request_id: str, api_key_id: int, tenant_id: int, estimate: Decimal,
                       quotes: dict, payload: dict | None) -> None:
    """The upstream may have charged for this request; keep its estimate occupied for review."""
    await Settlement.create(
        request_id=request_id, tenant_id=tenant_id, api_key_id=api_key_id, state="uncertain",
        occupied=estimate, quotes=quotes, payload=redact(payload) if payload else None,
        owner_id=OWNER_ID, dispatched=True,
        last_error="Upstream outcome unknown; manual reconciliation required",
    )
    try:
        await fast_billing.hold(api_key_id, tenant_id, estimate)
    except Exception:
        # The durable record still shows the amount; only admission stops counting it.
        logger.exception("Spend hold unavailable request_id=%s", request_id)


async def mark_pending(request_id: str, payload: dict, cost: Decimal | None, error: str = "") -> None:
    updates = {"payload": redact(payload), "cost": cost, "state": "pending",
               "last_error": error[:128], "next_retry_at": None}
    if cost is not None:
        updates["occupied"] = cost
    # Uncertain covers a worker presumed exited that later reports its usage; the actual usage then settles.
    updated = await Settlement.filter(request_id=request_id, state__in=("reserved", "uncertain")).update(**updates)
    if not updated and not await Settlement.exists(request_id=request_id, state__in=("pending", "settled", "failed")):
        raise RuntimeError("Reservation missing while persisting usage")


async def persist_usage(request_id: str, payload: dict, cost: Decimal | None, error: str = "") -> None:
    try:
        await mark_pending(request_id, payload, cost, error)
        _unsaved.pop(request_id, None)
    except Exception:
        # The durable reservation remains occupied. Retry saving actual usage in this process.
        _unsaved[request_id] = (redact(payload), cost, error)
        logger.critical("Usage persistence pending request_id=%s", request_id, exc_info=True)


async def apply(request_id: str, owners: tuple[int, int] | None = None) -> bool:
    """owners is (tenant_id, api_key_id) when the caller already knows them, saving one read."""
    if owners is None:
        record = await Settlement.get(request_id=request_id)
        owners = (record.tenant_id, record.api_key_id)
    tenant_id, api_key_id = owners
    log = None
    async with in_transaction():
        tenant = await Tenant.filter(id=tenant_id).select_for_update().first()
        key = await APIKey.filter(id=api_key_id).select_for_update().first()
        record = await Settlement.filter(request_id=request_id).select_for_update().get()
        if (record.tenant_id, record.api_key_id) != owners:
            raise RuntimeError("Settlement owner mismatch")
        if record.state == "settled":
            return False
        if record.state != "pending" or not record.payload or record.cost is None:
            return False
        if tenant is None or key is None:
            raise RuntimeError("Settlement owner missing")
        cost = record.cost
        await APIKey.filter(id=key.id).update(quota_used=F("quota_used") + cost)
        await Tenant.filter(id=tenant.id).update(quota_used=F("quota_used") + cost)
        payload = {key: value for key, value in record.payload.items() if key not in LEGACY_PAYLOAD_FIELDS}
        log = await save_request_log(**payload, observe=False)
        await Settlement.filter(id=record.id).update(state="settled", occupied=0, last_error="",
                                                   next_retry_at=None)
    if log:
        await observe_request_log(log)
    try:
        await record_windows(record)
    except Exception:
        logger.exception("Window observation pending request_id=%s", record.request_id)
    return True


async def record_windows(record: Settlement) -> None:
    if record.windows_recorded:
        return
    from app.services.spend_windows import record_settlement_spend
    await record_settlement_spend(record.request_id, record.api_key_id, record.tenant_id,
                                  record.cost or Decimal(0), record.created_at)
    await Settlement.filter(id=record.id).update(windows_recorded=True)


async def note_failure(request_id: str, exc: Exception) -> None:
    from app.services.metrics import SETTLEMENT_RETRIES
    SETTLEMENT_RETRIES.labels(error_type=type(exc).__name__).inc()
    record = await Settlement.get(request_id=request_id)
    if record.state != "pending":
        return
    attempts = record.attempts + 1
    await Settlement.filter(id=record.id, state="pending").update(
        attempts=attempts, state="failed" if attempts >= MAX_ATTEMPTS else "pending",
        next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=min(300, 2 ** attempts)),
        last_error=type(exc).__name__,
    )
    logger.error("Settlement pending request_id=%s attempt=%d error=%s", request_id, attempts, type(exc).__name__)


async def flush_unsaved() -> None:
    """Usage this worker failed to persist lives only in its memory, so every worker retries its own."""
    for request_id, (payload, cost, error) in list(_unsaved.items()):
        await mark_pending(request_id, payload, cost, error)
        _unsaved.pop(request_id, None)


async def recover_orphans(now: datetime | None = None) -> int:
    """Take over reservations left by releases that reserved before the upstream call.

    Undispatched work never reached the upstream and is released. Dispatched work may have
    been charged upstream, so it stays occupied as uncertain for reconciliation.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=settings.PROCESS_STALE_SECONDS)
    owners = sorted(set(await Settlement.filter(state="reserved", created_at__lt=cutoff)
                        .exclude(owner_id=OWNER_ID).values_list("owner_id", flat=True)))
    if not owners:
        return 0
    try:
        live = await cluster.live_owners(owners)
    except Exception:
        logger.exception("Worker heartbeats unavailable; reservations kept")
        return 0
    exited = [owner for owner in owners if owner not in live]
    if not exited:
        return 0
    released = await Settlement.filter(state="reserved", dispatched=False, owner_id__in=exited,
                                       created_at__lt=cutoff).update(state="released", occupied=0)
    uncertain = 0
    for record in await Settlement.filter(state="reserved", dispatched=True, owner_id__in=exited,
                                          created_at__lt=cutoff):
        if await Settlement.filter(id=record.id, state="reserved").update(
                state="uncertain", last_error="Process exited before usage was persisted"):
            uncertain += 1
            try:
                await fast_billing.hold(record.api_key_id, record.tenant_id, record.occupied)
            except Exception:
                logger.exception("Spend hold unavailable request_id=%s", record.request_id)
    if released or uncertain:
        logger.warning("Took over %d released and %d uncertain reservations from exited workers",
                       released, uncertain)
    return released + uncertain


async def recover_once() -> int:
    now = datetime.now(timezone.utc)
    await flush_unsaved()
    rows = await Settlement.filter(state="pending").order_by("id").limit(100)
    completed = 0
    for record in rows:
        if record.next_retry_at and record.next_retry_at > now:
            continue
        try:
            if record.cost is None:
                # A saved quotation/calculation error requires review, never a silent zero charge.
                raise ValueError("Unpriced pending usage requires review")
            completed += int(await apply(record.request_id))
        except Exception as exc:
            await note_failure(record.request_id, exc)
    for record in await Settlement.filter(state="settled", windows_recorded=False).limit(100):
        try:
            await record_windows(record)
        except Exception:
            logger.exception("Window recovery failed request_id=%s", record.request_id)
    from app.services.metrics import SETTLEMENT_WAITING
    for state in ("pending", "failed", "uncertain"):
        SETTLEMENT_WAITING.labels(state=state).set(await Settlement.filter(state=state).count())
    return completed


async def recovery_loop():
    while True:
        try:
            await flush_unsaved()
            # Settlement rows are locked, so an overlapping run only wastes work; the claim avoids it.
            if await cluster.claim("settlement-recovery", RECOVERY_INTERVAL):
                await recover_orphans()
                await recover_once()
                await sync_holds()
        except Exception:
            logger.exception("Settlement recovery unavailable")
        await asyncio.sleep(RECOVERY_INTERVAL)

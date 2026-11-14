import asyncio
import logging
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tortoise.transactions import in_transaction

from app.models import RequestLog, RequestLogAudit
from app.services.metrics import record_request_metrics
from app.services.usage_stats import record_daily_totals, record_daily_usage, record_minutes, _record_minute
from app.services.redaction import redact
from app.services.request_context import client_request_id as correlation_id

logger = logging.getLogger(__name__)


def _build_log(
    request_id: str,
    api_key_id: int,
    api_key_name: str,
    channel_id: int | None,
    channel_name: str,
    model_requested: str,
    model_actual: str,
    provider: str,
    endpoint: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost: Decimal = Decimal(0),
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    is_stream: bool = False,
    status_code: int = 0,
    latency_ms: int = 0,
    error_message: str = "",
    error_origin: str = "",
    upstream_status_code: int = 0,
    upstream_error: str = "",
    ip_address: str = "",
    failed_over: bool = False,
    audit_request: str = "",
    security_hit: str = "",
    tenant_id: int = 0,
    session_id: str = "",
    cache_savings: Decimal = Decimal(0),
    cost_detail: dict | None = None,
    client_request_id: str = "",
    usage_source: str = "upstream",
    observed_at: str | None = None,
) -> tuple[RequestLog, str]:
    """An unsaved log row and its redacted audit text."""
    try:
        audit_request = json.dumps(redact(json.loads(audit_request)), ensure_ascii=False) if audit_request else ""
    except (ValueError, TypeError):
        audit_request = redact(audit_request)
    log = RequestLog(
        request_id=request_id,
        client_request_id=client_request_id or correlation_id.get(),
        usage_source=usage_source,
        created_at=datetime.fromisoformat(observed_at) if observed_at else datetime.now(timezone.utc),
        api_key_id=api_key_id,
        tenant_id=tenant_id,
        api_key_name=api_key_name,
        channel_id=channel_id,
        channel_name=channel_name,
        model_requested=model_requested,
        model_actual=model_actual,
        provider=provider,
        endpoint=endpoint,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        cost=cost,
        is_stream=is_stream,
        status_code=status_code,
        latency_ms=latency_ms,
        error_message=redact(error_message),
        error_origin=error_origin,
        upstream_status_code=upstream_status_code,
        upstream_error=redact(upstream_error),
        ip_address=ip_address,
        failed_over=failed_over,
        has_audit=bool(audit_request),
        security_hit=security_hit[:128],
        session_id=session_id[:32],
        cache_savings=cache_savings,
        cost_detail=cost_detail,
    )
    return log, audit_request


async def save_request_log(*, observe: bool = True, **fields) -> RequestLog:
    log, audit_request = _build_log(**fields)
    async with in_transaction():
        await log.save()
        if audit_request:
            await RequestLogAudit.create(request_log=log, audit_request=audit_request)
        await record_daily_usage(**_daily_fields(log), when=log.created_at, realtime=False)
    if observe:
        await observe_request_log(log)
    return log


async def save_request_logs(batch: list[dict]) -> list[RequestLog]:
    """Insert many logs at once; the caller provides the transaction and observes them after commit."""
    built = [_build_log(**fields) for fields in batch]
    plain = [log for log, audit_request in built if not audit_request]
    if plain:
        await RequestLog.bulk_create(plain)
    for log, audit_request in built:
        if audit_request:
            await log.save()
            await RequestLogAudit.create(request_log=log, audit_request=audit_request)
    await record_daily_totals([(_daily_fields(log), log.created_at) for log, _ in built])
    return [log for log, _ in built]


def _daily_fields(log: RequestLog) -> dict:
    return dict(
        api_key_id=log.api_key_id, api_key_name=log.api_key_name, tenant_id=log.tenant_id,
        model=log.model_requested, channel_id=log.channel_id, channel_name=log.channel_name,
        status_code=log.status_code, prompt_tokens=log.prompt_tokens, completion_tokens=log.completion_tokens,
        cost=Decimal(str(log.cost)), cached_tokens=log.cached_tokens, cache_write_tokens=log.cache_write_tokens,
        cache_savings=Decimal(str(log.cache_savings)),
    )


async def observe_request_log(log: RequestLog) -> None:
    try:
        await _record_minute(log.created_at)
        _record_metrics(log)
    except Exception:
        logger.exception("Post-commit observation failed for request %s", log.request_id)


async def observe_request_logs(logs: list[RequestLog]) -> None:
    try:
        await record_minutes([log.created_at for log in logs])
        for log in logs:
            _record_metrics(log)
    except Exception:
        logger.exception("Post-commit observation failed for %d requests", len(logs))


def _record_metrics(log: RequestLog) -> None:
    record_request_metrics(
        model=log.model_actual or log.model_requested, provider=log.provider,
        channel_name=log.channel_name, status_code=log.status_code, endpoint=log.endpoint,
        prompt_tokens=log.prompt_tokens, completion_tokens=log.completion_tokens,
        cached_tokens=log.cached_tokens, cost=float(log.cost), latency_seconds=log.latency_ms / 1000.0,
    )


async def cleanup_old_logs(days: int | None = None) -> int:
    """删除超过保留天数的日志，分批删除避免锁表。返回删除总数。"""
    if days is None:
        from app.services.settings_service import gateway_config

        retention = (await gateway_config())["log_retention_days"]
    else:
        retention = days
    if retention <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
    total_deleted = 0
    batch_size = 10000
    while True:
        ids = await RequestLog.filter(created_at__lt=cutoff).limit(batch_size).values_list("id", flat=True)
        if not ids:
            break
        await RequestLogAudit.filter(request_log_id__in=ids).delete()
        deleted = await RequestLog.filter(id__in=ids).delete()
        total_deleted += deleted
    if total_deleted > 0:
        logger.info("Cleaned up %d logs older than %d days", total_deleted, retention)
    return total_deleted


async def log_cleanup_loop() -> None:
    """后台循环：所有 worker 合计每天清理一次过期日志。"""
    from app.services.cluster import claim
    while True:
        await asyncio.sleep(600)
        try:
            if await claim("log-cleanup", 86400):
                await cleanup_old_logs()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error in log cleanup loop")

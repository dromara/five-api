import json

from fastapi import Depends, APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.models import APIKey, ManagementEvent, RequestLog, RequestLogAudit, User
from app.services.ai_audit import MIN_CHARS, _mark, _verdict, build_excerpt, is_final
from app.services.audit import saved_transcript
from app.services.content_filter import keyword_was_hit
from app.services.auth import require_audit_reader
from app.services.management_events import record_view
from app.services.redaction import secret_findings
from app.services.settings_service import ai_audit_config

router = APIRouter(prefix="/api/audit", tags=["audit"])

MAX_REVIEW = 20
_SCAN_LIMIT = 1000


class AuditPerson(BaseModel):
    api_key_id: int
    api_key_name: str
    request_count: int
    latest_at: str = ""


class AuditRequestItem(BaseModel):
    request_id: str
    created_at: str
    model_requested: str
    status_code: int
    error_origin: str
    error_message: str
    ai_review: str
    keyword_hit: bool
    excerpt: str
    saved: str
    findings: list[dict] = []


class AuditReviewBody(BaseModel):
    request_ids: list[str] = Field(min_length=1, max_length=MAX_REVIEW)


class AuditReviewItem(BaseModel):
    request_id: str
    ai_review: str
    excerpt: str


def _excerpt(stored: str) -> str:
    try:
        body = json.loads(stored)
    except json.JSONDecodeError:
        return ""
    if not isinstance(body, dict):
        return ""
    return build_excerpt(body)


@router.get("/keys")
async def list_people(_: User = Depends(require_audit_reader)):
    """Every key can be opened. Counts only include requests whose body was saved."""
    keys = await APIKey.all().order_by("name").values("id", "name")
    rows = await RequestLog.filter(has_audit=True).order_by("-id").limit(_SCAN_LIMIT).values(
        "api_key_id", "created_at",
    )
    stats: dict[int, dict] = {}
    for row in rows:
        item = stats.get(row["api_key_id"])
        if item is None:
            stats[row["api_key_id"]] = {"request_count": 1, "latest_at": row["created_at"]}
            continue
        item["request_count"] += 1
    items = [
        AuditPerson(
            api_key_id=key["id"],
            api_key_name=key["name"],
            request_count=stats.get(key["id"], {}).get("request_count", 0),
            latest_at=stats[key["id"]]["latest_at"].isoformat() if key["id"] in stats else "",
        )
        for key in keys
    ]
    items.sort(key=lambda item: (item.request_count == 0, item.api_key_name.lower()))
    return {"items": items}


@router.get("/keys/{key_id}/requests")
async def list_requests(
    key_id: int,
    page: int = Query(1, ge=1),
    actor: User = Depends(require_audit_reader),
):
    size = MAX_REVIEW
    query = RequestLog.filter(api_key_id=key_id, has_audit=True)
    total = await query.count()
    logs = await query.order_by("-id").offset((page - 1) * size).limit(size)
    audits = {
        row.request_log_id: row.audit_request
        for row in await RequestLogAudit.filter(request_log_id__in=[log.id for log in logs])
    }
    items = [
        AuditRequestItem(
            request_id=log.request_id,
            created_at=log.created_at.isoformat(),
            model_requested=log.model_requested,
            status_code=log.status_code,
            error_origin=log.error_origin,
            error_message=log.error_message,
            ai_review=log.ai_review,
            keyword_hit=keyword_was_hit(log.security_hit),
            excerpt=_excerpt(audits.get(log.id, "")),
            saved=saved_transcript(audits.get(log.id, "")),
            findings=secret_findings(audits.get(log.id, "")),
        )
        for log in logs
    ]
    if logs:
        await record_view(actor, "audit.view", key_id, logs[0].tenant_id, logs, {"page": page})
    return {"total": total, "items": items}


@router.post("/review")
async def review_requests(body: AuditReviewBody, _: User = Depends(require_audit_reader)):
    config = await ai_audit_config()
    if not config["enabled"] or not config["model"]:
        raise HTTPException(status_code=400, detail="AI audit is off")
    logs = await RequestLog.filter(request_id__in=body.request_ids, has_audit=True)
    by_id = {log.request_id: log for log in logs}
    audits = {
        row.request_log_id: row.audit_request
        for row in await RequestLogAudit.filter(request_log_id__in=[log.id for log in logs])
    }
    items: list[AuditReviewItem] = []
    for request_id in body.request_ids:
        log = by_id.get(request_id)
        if log is None:
            continue
        excerpt = _excerpt(audits.get(log.id, ""))
        if is_final(log.ai_review):
            items.append(AuditReviewItem(request_id=request_id, ai_review=log.ai_review, excerpt=excerpt))
            continue
        if len(excerpt) < MIN_CHARS:
            await _mark(request_id, "skipped")
            items.append(AuditReviewItem(request_id=request_id, ai_review="skipped", excerpt=excerpt))
            continue
        try:
            verdict = await _verdict(config["model"], excerpt)
        except Exception:
            verdict = "unavailable"
        await _mark(request_id, verdict)
        items.append(AuditReviewItem(request_id=request_id, ai_review=verdict, excerpt=excerpt))
    return {"items": items}


class ReportDownloadBody(BaseModel):
    request_ids: list[str] = Field(min_length=1, max_length=1000)


@router.post("/keys/{key_id}/report-downloads", status_code=201)
async def record_report_download(key_id: int, body: ReportDownloadBody,
                                 actor: User = Depends(require_audit_reader)):
    """Logged before the browser saves the report, so every download leaves a record."""
    key = await APIKey.get_or_none(id=key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Key not found")
    logs = await RequestLog.filter(api_key_id=key_id, request_id__in=body.request_ids, has_audit=True)
    await record_view(actor, "audit.report_download", key_id, key.tenant_id, logs)
    return {"recorded": len(logs)}


@router.get("/views")
async def list_views(page: int = Query(1, ge=1), _: User = Depends(require_audit_reader)):
    """Body views and report downloads, newest first."""
    size = 50
    query = ManagementEvent.filter(action__in=("audit.view", "audit.report_download"))
    events = await query.order_by("-id").offset((page - 1) * size).limit(size)
    actors = {user.id: user.username for user in await User.filter(id__in={e.actor_id for e in events})}
    keys = {key.id: key.name for key in await APIKey.filter(id__in={e.target_id for e in events})}
    return {"total": await query.count(), "items": [
        {"action": event.action, "actor": actors.get(event.actor_id, f"#{event.actor_id}"),
         "api_key_id": event.target_id, "api_key_name": keys.get(event.target_id, ""),
         "requests": event.detail.get("requests", 0), "from": event.detail.get("from", ""),
         "to": event.detail.get("to", ""), "created_at": event.created_at.isoformat()}
        for event in events
    ]}

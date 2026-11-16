"""A tenant's own bill per key: written spend for a date range plus spend still queued in Redis."""
import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal

from tortoise.functions import Sum

from app.models import APIKey, Tenant, UsageDaily
from app.services import fast_billing

MAX_DAYS = 366
HEADERS = {
    "zh": ("Key 名称", "Key 前缀", "开始日期", "结束日期", "请求数", "已入库花费（USD）", "尚未入库花费（USD）"),
    "en": ("Key name", "Key prefix", "Start date", "End date", "Requests", "Written spend (USD)",
           "Not yet written (USD)"),
}
NOTICE = {
    "zh": ("日期按 UTC 计算。并发请求可能一起越过额度，超出部分照实扣费。",
           "尚未入库花费是导出时刻 Redis 中还没写入数据库的金额，不分日期，稍后会计入已入库花费。"),
    "en": ("Dates are UTC. Concurrent requests can jointly spend past a budget; the excess is charged in full.",
           "Not-yet-written spend is what Redis held at export time, for any date; it moves into written spend shortly."),
}
TOTAL = {"zh": "合计", "en": "Total"}


async def build(tenant: Tenant, start: date, end: date) -> dict:
    keys = await APIKey.filter(tenant_id=tenant.id).order_by("id")
    rows = await UsageDaily.filter(tenant_id=tenant.id, day__gte=start, day__lte=end).annotate(
        requests=Sum("request_count"), spend=Sum("cost"),
    ).group_by("api_key_id").values("api_key_id", "requests", "spend")
    written = {row["api_key_id"]: row for row in rows}
    pending = await fast_billing.pending_many("key", [key.id for key in keys])
    items = []
    for key in keys:
        usage = written.get(key.id, {})
        requests, spend = int(usage.get("requests") or 0), Decimal(str(usage.get("spend") or 0))
        queued = pending.get(key.id, Decimal(0))
        if key.is_revoked and not (requests or spend or queued):
            continue
        items.append({"api_key_id": key.id, "api_key_name": key.name, "key_prefix": key.key_prefix,
                      "request_count": requests, "written": spend, "pending": queued})
    return {
        "tenant": tenant.name, "start": start.isoformat(), "end": end.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "total": {"request_count": sum(item["request_count"] for item in items),
                  "written": sum((item["written"] for item in items), Decimal(0)),
                  "pending": sum((item["pending"] for item in items), Decimal(0))},
    }


def _cell(value) -> str:
    text = str(value)
    # Spreadsheets run cells starting with these as formulas.
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(bill: dict, lang: str) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([f"# Five API {bill['tenant']} {bill['start']} ~ {bill['end']} ({bill['generated_at']})"])
    for line in NOTICE[lang]:
        writer.writerow(["# " + line])
    writer.writerow(HEADERS[lang])
    for item in bill["items"]:
        writer.writerow([_cell(item["api_key_name"]), item["key_prefix"], bill["start"], bill["end"],
                         item["request_count"], item["written"], item["pending"]])
    total = bill["total"]
    writer.writerow([TOTAL[lang], "", bill["start"], bill["end"], total["request_count"], total["written"],
                     total["pending"]])
    return "\ufeff" + out.getvalue()

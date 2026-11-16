"""How much of each capped budget a key and its team have used.

Budget alerts read the key quota, the team quota and every rolling window cap,
all as settled spend. Unlimited budgets are left out.
"""

from dataclasses import dataclass

from app.models import APIKey, Tenant
from app.services.auth import cached_tenant
from app.services.spend_windows import normalize_limits, window_spend


@dataclass(frozen=True)
class BudgetUsage:
    scope: str
    owner_id: int
    owner_name: str
    budget: str
    used: float
    limit: float

    @property
    def share(self) -> float:
        return self.used / self.limit if self.limit > 0 else 1.0


async def _owner_usages(scope: str, owner: APIKey | Tenant) -> list[BudgetUsage]:
    total = float(owner.quota_total)
    caps = normalize_limits(owner.window_limits)
    if total < 0 and not caps:
        return []
    usages = []
    if total >= 0:
        usages.append(BudgetUsage(scope, owner.id, owner.name, "quota", float(owner.quota_used), total))
    if caps:
        spent = await window_spend(scope, owner.id)
        usages.extend(
            BudgetUsage(scope, owner.id, owner.name, window, spent[window], cap)
            for window, cap in caps.items()
        )
    return usages


def used_up(quota_total, quota_used, occupied, limits: dict[str, float], spend: dict[str, float]) -> list[str]:
    """Budgets admission refuses on: the quota counting unwritten spend, then each full rolling window."""
    budgets = ["quota"] if quota_total != -1 and quota_used + occupied >= quota_total else []
    return budgets + [window for window, cap in limits.items() if spend.get(window, 0) >= cap]


async def budget_usages(api_key: APIKey) -> list[BudgetUsage]:
    usages = await _owner_usages("key", api_key)
    if api_key.tenant_id:
        team = cached_tenant(api_key) or await Tenant.get_or_none(id=api_key.tenant_id)
        if team is not None:
            usages.extend(await _owner_usages("team", team))
    return usages


"""Channel plan for one proxied request: session binding and fallbacks."""

from collections.abc import Mapping
from dataclasses import dataclass

from app.models import APIKey
from app.providers.registry import resolve_with_fallbacks
from app.services.pre_checks import get_effective_allowed_models
from app.services.sticky_session import get_sticky_channel, make_session_key, session_log_id


@dataclass
class RoutePlan:
    candidates: list
    session_key: str | None
    session_id: str


async def plan_route(
    api_key: APIKey, model: str, protocol: str, headers: Mapping[str, str], body: dict,
) -> RoutePlan:
    session_key = await make_session_key(api_key.id, headers, body)
    candidates = await resolve_with_fallbacks(
        model, preferred_protocol=protocol,
        sticky_channel_id=await get_sticky_channel(session_key),
        allowed_models=await get_effective_allowed_models(api_key),
        channel_ids=api_key.channel_ids or None,
    )
    return RoutePlan(
        candidates=candidates,
        session_key=session_key,
        session_id=session_log_id(api_key.id, headers, body),
    )

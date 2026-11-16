import hashlib
import json
import logging
import re

from app.services.request_context import credentials

SECRET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]{0,48}PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
)

_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]{0,48}PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]{0,48}PRIVATE KEY-----|\Z)", re.S),
     "[REDACTED:private_key]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws_access_key]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED:github_token]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack_token]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[REDACTED]"),
    (re.compile(r"""(?i)([?&](?:api[_-]?key|access_token|token|password|secret)=)[^&\s'"]+"""), r"\1[REDACTED]"),
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
]
_SENSITIVE = {"authorization", "x-api-key", "api_key", "access_token", "password", "old_password", "new_password", "secret"}

# Upstream error text describes the platform's own account; tenants see the error, not the account.
_UPSTREAM_ACCOUNT_PATTERNS = [
    (re.compile(r"\borg-[A-Za-z0-9]{6,}"), "org-[REDACTED]"),
    (re.compile(r"\bproj_[A-Za-z0-9]{6,}"), "proj_[REDACTED]"),
    (re.compile(r"(?i)(organization\s*\(?)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(limit:?\s*)\d[\d,.]*(,\s*used:?\s*)\d[\d,.]*"), r"\1[REDACTED]\2[REDACTED]"),
    (re.compile(r"(?i)\b(of\s+)\d[\d,]*(\s+(?:input\s+|output\s+)?tokens?\s+per\s+(?:minute|day|hour))"), r"\1[REDACTED]\2"),
]


_SECRET_MASKS = [(replacement.removeprefix("[REDACTED:").removesuffix("]"), pattern)
                 for pattern, replacement in _PATTERNS if replacement.startswith("[REDACTED:")]


def mask_secrets(value):
    """Mask scanned secrets in place as [REDACTED:type#fingerprint].

    The fingerprint is the first 8 hex digits of the secret's SHA-256, enough to tell whether
    two requests carried the same secret without keeping anything that recovers it.
    """
    if isinstance(value, dict):
        return {key: mask_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    if not isinstance(value, str):
        return value
    for label, pattern in _SECRET_MASKS:
        value = pattern.sub(
            lambda match: f"[REDACTED:{label}#{hashlib.sha256(match.group(0).encode()).hexdigest()[:8]}]", value)
    return value


_MASK = re.compile(r"\[REDACTED:([a-z_]+)#([0-9a-f]{8})\]")
MAX_FINDINGS = 50


def secret_findings(stored: str) -> list[dict]:
    """Type, JSON path and fingerprint of every mask in a stored body."""
    try:
        root = json.loads(stored)
    except (TypeError, ValueError):
        root = stored
    found: list[dict] = []
    stack = [("$", root)]
    while stack and len(found) < MAX_FINDINGS:
        path, value = stack.pop()
        if isinstance(value, dict):
            stack.extend((f"{path}.{key}", item) for key, item in reversed(value.items()))
        elif isinstance(value, list):
            stack.extend((f"{path}[{index}]", item) for index, item in reversed(list(enumerate(value))))
        elif isinstance(value, str):
            found.extend({"type": match.group(1), "fingerprint": match.group(2), "path": path}
                         for match in _MASK.finditer(value))
    return found[:MAX_FINDINGS]


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if str(key).lower() in _SENSITIVE else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    for secret in sorted(credentials.get(), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _hide_upstream_account(value):
    if isinstance(value, dict):
        return {key: _hide_upstream_account(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_hide_upstream_account(item) for item in value]
    if not isinstance(value, str):
        return value
    for pattern, replacement in _UPSTREAM_ACCOUNT_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def redact_upstream_error(value):
    """Upstream error bodies sent to tenants: credentials plus the platform's account IDs and limits."""
    return _hide_upstream_account(redact(value))


class RedactingFormatter(logging.Formatter):
    def __init__(self, original: logging.Formatter):
        super().__init__()
        self.original = original

    def format(self, record):
        return redact(self.original.format(record))


_APP_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_safe_logging():
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "app"):
        for handler in logging.getLogger(name).handlers:
            if not isinstance(handler.formatter, RedactingFormatter):
                handler.setFormatter(RedactingFormatter(handler.formatter or logging.Formatter("%(levelname)s %(message)s")))
    app_logger = logging.getLogger("app")
    if not app_logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(RedactingFormatter(logging.Formatter(_APP_FORMAT)))
        app_logger.addHandler(handler)

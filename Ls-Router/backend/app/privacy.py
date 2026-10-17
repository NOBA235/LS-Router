"""Redaction of accidental personal or sensitive information in free text.

One policy shared by every entry point (MCP server, HTTP API) so user-supplied descriptions are treated identically no
matter how they arrive. Public source URLs (OpenStreetMap etc.) and entity ids are preserved; only embedded credentials,
e-mail addresses, phone numbers, API-key-like tokens and local user-profile paths are replaced.
"""
from __future__ import annotations

import re
from typing import Any

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SECRET_TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._-]{12,})\b", re.IGNORECASE)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s-]?\d{8,12}|(?:\+?\d{1,3}[\s-]?)?(?:\(\d{2,4}\)[\s-]?)?\d{3,5}[\s-]\d{5,7})(?!\d)"
)
_USER_PATH_RE = re.compile(r"(?:[A-Za-z]:\\Users\\[^\\\s\"']+)|(?:/Users/[^/\s\"']+)|(?:/home/[^/\s\"']+)")


def redact_sensitive_text(text: str) -> str:
    """Redact accidental personal or sensitive tokens (emails, phone numbers, API keys, local user paths)."""
    if not text or not isinstance(text, str):
        return text
    cleaned = _SECRET_TOKEN_RE.sub("[REDACTED_SECRET]", text)
    cleaned = _EMAIL_RE.sub("[REDACTED_EMAIL]", cleaned)
    cleaned = _PHONE_RE.sub("[REDACTED_PHONE]", cleaned)
    cleaned = _USER_PATH_RE.sub("[REDACTED_USER_PATH]", cleaned)
    return cleaned


def sanitize_payload(value: Any) -> Any:
    """Recursively sanitize strings in a JSON-serializable payload while preserving graph URLs and IDs."""
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            # Preserve public OSM / source provenance URLs while stripping any embedded userinfo
            return re.sub(r"(https?://)[^/@\s]+@", r"\1[REDACTED]@", value)
        return redact_sensitive_text(value)
    if isinstance(value, list):
        return [sanitize_payload(v) for v in value]
    if isinstance(value, tuple):
        return [sanitize_payload(v) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_lower = str(k).lower()
            if any(sec in key_lower for sec in ("password", "api_key", "secret", "access_token", "authorization")):
                out[k] = "[REDACTED]"
            else:
                out[k] = sanitize_payload(v)
        return out
    return value


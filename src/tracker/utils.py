from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from html import escape
from urllib.parse import ParseResult, parse_qsl, urlencode, urljoin, urlparse, urlunparse


TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url: str, base: str | None = None) -> str:
    url = clean_text(url)
    if base:
        url = urljoin(base, url)
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower_key = key.lower()
        if lower_key in TRACKING_QUERY_KEYS:
            continue
        if any(lower_key.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        query_pairs.append((key, value))
    query = urlencode(query_pairs, doseq=True)

    normalized = ParseResult(
        scheme=scheme,
        netloc=netloc,
        path=path,
        params="",
        query=query,
        fragment="",
    )
    return urlunparse(normalized)


def same_domain(url: str, base_url: str) -> bool:
    url_host = urlparse(url).netloc.lower().removeprefix("www.")
    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    return url_host == base_host or url_host.endswith(f".{base_host}")


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def content_hash(value: str | list | dict) -> str:
    if not isinstance(value, str):
        value = repr(value)
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()


def html_escape(value: str) -> str:
    return escape(value or "", quote=True)


def truncate(value: str, limit: int = 260) -> str:
    value = clean_text(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def path_matches_keywords(url: str, include_keywords: list[str], exclude_keywords: list[str]) -> bool:
    path = urlparse(url).path.lower() or "/"
    full = f"{path}?{urlparse(url).query}".lower()
    if any(keyword.lower() in full for keyword in exclude_keywords if keyword):
        return False
    if "/" in include_keywords and path == "/":
        return True
    return any(keyword.lower() in full for keyword in include_keywords if keyword and keyword != "/")


def category_for_url(url: str, title: str = "") -> str:
    text = f"{urlparse(url).path} {title}".lower()
    if any(word in text for word in ("pricing", "price", "quote", "cost")):
        return "pricing"
    if any(word in text for word in ("blog", "article", "resource", "knowledge", "news")):
        return "content"
    if any(word in text for word in ("service", "capabilities", "manufacturing", "machining", "cnc")):
        return "service"
    if urlparse(url).path in ("", "/"):
        return "homepage"
    return "page"


def ensure_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [item.strip() for item in str(value).split(",") if item.strip()]


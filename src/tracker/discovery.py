from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .utils import normalize_url, path_matches_keywords, same_domain


USER_AGENT = (
    "Mozilla/5.0 (compatible; CompetitorTrackingBot/1.0; "
    "+https://github.com/kevi-55/Automated-competitor-tracking)"
)


@dataclass(frozen=True)
class DiscoveryResult:
    competitor: str
    base_url: str
    urls: list[str]
    source_counts: dict[str, int]
    errors: list[str]


def discover_competitor(competitor: dict, config: dict) -> DiscoveryResult:
    base_url = normalize_url(competitor["base_url"])
    max_pages = int(config["run"]["max_pages_per_domain"])
    timeout = int(config["run"]["request_timeout_seconds"])
    include_keywords = config["discovery"]["include_path_keywords"]
    exclude_keywords = config["discovery"]["exclude_path_keywords"]
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    priority_urls = [
        normalize_url(path, base_url) for path in competitor.get("priority_paths", ["/"])
    ]

    errors: list[str] = []
    sitemap_urls = _discover_from_sitemaps(
        session=session,
        base_url=base_url,
        timeout=timeout,
        include_keywords=include_keywords,
        exclude_keywords=exclude_keywords,
        errors=errors,
    )

    link_urls = _discover_from_seed_links(
        session=session,
        seed_urls=priority_urls[:12],
        base_url=base_url,
        timeout=timeout,
        include_keywords=include_keywords,
        exclude_keywords=exclude_keywords,
        errors=errors,
    )

    ranked = _rank_urls(
        base_url=base_url,
        priority_urls=priority_urls,
        discovered_urls=[*sitemap_urls, *link_urls],
        include_keywords=include_keywords,
        exclude_keywords=exclude_keywords,
    )

    return DiscoveryResult(
        competitor=competitor["name"],
        base_url=base_url,
        urls=ranked[:max_pages],
        source_counts={
            "priority": len(priority_urls),
            "sitemap": len(sitemap_urls),
            "links": len(link_urls),
        },
        errors=errors,
    )


def _discover_from_sitemaps(
    session: requests.Session,
    base_url: str,
    timeout: int,
    include_keywords: list[str],
    exclude_keywords: list[str],
    errors: list[str],
) -> list[str]:
    sitemap_candidates = [
        urljoin(base_url, "/sitemap.xml"),
        urljoin(base_url, "/sitemap_index.xml"),
        urljoin(base_url, "/sitemap-index.xml"),
        urljoin(base_url, "/sitemap/sitemap.xml"),
    ]
    queue = deque(sitemap_candidates)
    seen_sitemaps: set[str] = set()
    urls: list[str] = []

    while queue and len(seen_sitemaps) < 30:
        sitemap_url = normalize_url(queue.popleft())
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)
        try:
            response = session.get(sitemap_url, timeout=timeout)
            if response.status_code >= 400:
                continue
            content = response.text
            root = ET.fromstring(content.encode("utf-8"))
        except Exception as exc:
            errors.append(f"Sitemap read failed: {sitemap_url} ({exc})")
            continue

        for loc in root.iter():
            if not loc.tag.lower().endswith("loc") or not loc.text:
                continue
            loc_url = normalize_url(loc.text)
            if loc_url.endswith(".xml") and same_domain(loc_url, base_url):
                queue.append(loc_url)
                continue
            if _is_trackable_url(loc_url, base_url, include_keywords, exclude_keywords):
                urls.append(loc_url)

    return _dedupe(urls)


def _discover_from_seed_links(
    session: requests.Session,
    seed_urls: Iterable[str],
    base_url: str,
    timeout: int,
    include_keywords: list[str],
    exclude_keywords: list[str],
    errors: list[str],
) -> list[str]:
    urls: list[str] = []
    for seed_url in seed_urls:
        try:
            response = session.get(seed_url, timeout=timeout)
            if response.status_code >= 400:
                errors.append(f"Seed page failed: {seed_url} (HTTP {response.status_code})")
                continue
        except Exception as exc:
            errors.append(f"Seed page failed: {seed_url} ({exc})")
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        for link in soup.find_all("a", href=True):
            found_url = normalize_url(link.get("href", ""), seed_url)
            if _is_trackable_url(found_url, base_url, include_keywords, exclude_keywords):
                urls.append(found_url)

    return _dedupe(urls)


def _is_trackable_url(
    url: str,
    base_url: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not same_domain(url, base_url):
        return False
    if _looks_like_file(parsed.path):
        return False
    return path_matches_keywords(url, include_keywords, exclude_keywords)


def _looks_like_file(path: str) -> bool:
    ignored_extensions = (
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".svg",
        ".pdf",
        ".zip",
        ".xml",
        ".json",
        ".js",
        ".css",
        ".ico",
        ".woff",
        ".woff2",
    )
    return path.lower().endswith(ignored_extensions)


def _rank_urls(
    base_url: str,
    priority_urls: list[str],
    discovered_urls: list[str],
    include_keywords: list[str],
    exclude_keywords: list[str],
) -> list[str]:
    candidates = _dedupe([*priority_urls, *discovered_urls])
    candidates = [
        url
        for url in candidates
        if _is_trackable_url(url, base_url, include_keywords, exclude_keywords)
    ]
    priority_set = set(priority_urls)

    def score(url: str) -> tuple[int, int, int, str]:
        path = urlparse(url).path.lower() or "/"
        if url in priority_set:
            bucket = 0
        elif path in {"", "/"}:
            bucket = 1
        elif any(word in path for word in ("pricing", "price", "quote")):
            bucket = 2
        elif any(word in path for word in ("service", "capabilities", "manufacturing")):
            bucket = 3
        elif any(word in path for word in ("blog", "article", "resource", "knowledge", "news")):
            bucket = 4
        else:
            bucket = 5
        return (bucket, path.count("/"), len(path), path)

    return sorted(candidates, key=score)


def _dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


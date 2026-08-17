from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .utils import clean_text, normalize_url, path_matches_keywords, same_domain


USER_AGENT = (
    "Mozilla/5.0 (compatible; CompetitorTrackingBot/2.0; "
    "+https://github.com/kevi-55/Automated-competitor-tracking)"
)


@dataclass(frozen=True)
class DiscoveryResult:
    competitor: str
    base_url: str
    urls: list[str]
    url_metadata: dict[str, dict[str, str]]
    source_counts: dict[str, int]
    errors: list[str]


def discover_competitor(competitor: dict, config: dict) -> DiscoveryResult:
    base_url = normalize_url(competitor["base_url"])
    max_pages = int(config["run"]["max_pages_per_domain"])
    timeout = int(config["run"]["request_timeout_seconds"])
    include_keywords = config["discovery"]["include_path_keywords"]
    exclude_keywords = [
        *config["discovery"]["exclude_path_keywords"],
        *competitor.get("exclude_path_keywords", []),
    ]
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})

    priority_urls = [
        normalize_url(path, base_url) for path in competitor.get("priority_paths", ["/"])
    ]
    errors: list[str] = []

    feed_entries = _discover_from_feeds(
        session=session,
        base_url=base_url,
        timeout=timeout,
        include_keywords=include_keywords,
        exclude_keywords=exclude_keywords,
        errors=errors,
        max_items=int(config["discovery"].get("feed_max_items", 100)),
    )
    sitemap_entries = _discover_from_sitemaps(
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

    metadata: dict[str, dict[str, str]] = {}
    for url in priority_urls:
        metadata[url] = {"source": "priority"}
    for url, values in sitemap_entries.items():
        metadata.setdefault(url, {}).update(values)
    for url in link_urls:
        metadata.setdefault(url, {"source": "links"})
    # A feed date is usually more useful than sitemap lastmod, so apply it last.
    for url, values in feed_entries.items():
        metadata.setdefault(url, {}).update(values)

    ranked = _rank_urls(
        base_url=base_url,
        priority_urls=priority_urls,
        metadata=metadata,
        include_keywords=include_keywords,
        exclude_keywords=exclude_keywords,
    )[:max_pages]

    return DiscoveryResult(
        competitor=competitor["name"],
        base_url=base_url,
        urls=ranked,
        url_metadata={url: metadata.get(url, {}) for url in ranked},
        source_counts={
            "priority": len(priority_urls),
            "feed": len(feed_entries),
            "sitemap": len(sitemap_entries),
            "links": len(link_urls),
        },
        errors=errors,
    )


def _discover_from_feeds(
    session: requests.Session,
    base_url: str,
    timeout: int,
    include_keywords: list[str],
    exclude_keywords: list[str],
    errors: list[str],
    max_items: int,
) -> dict[str, dict[str, str]]:
    feed_urls = {
        urljoin(base_url, path)
        for path in ("/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml")
    }
    try:
        response = session.get(base_url, timeout=timeout)
        if response.status_code < 400:
            soup = BeautifulSoup(response.content, "html.parser")
            for tag in soup.find_all("link", href=True):
                rel = " ".join(tag.get("rel", [])).lower()
                media_type = clean_text(tag.get("type", "")).lower()
                if "alternate" in rel and any(value in media_type for value in ("rss", "atom", "xml")):
                    feed_urls.add(normalize_url(tag["href"], base_url))
    except Exception as exc:
        errors.append(f"Feed discovery failed: {base_url} ({exc})")

    entries: dict[str, dict[str, str]] = {}
    for feed_url in sorted(feed_urls):
        if len(entries) >= max_items:
            break
        try:
            response = session.get(feed_url, timeout=timeout)
            if response.status_code >= 400:
                continue
            root = ET.fromstring(response.content)
        except Exception:
            # Common feed candidates are speculative; do not flood the report.
            continue

        for item in root.iter():
            if _local_name(item.tag) not in {"item", "entry"}:
                continue
            link = _feed_item_link(item, feed_url)
            if not link or not _is_trackable_url(
                link, base_url, include_keywords, exclude_keywords
            ):
                continue
            published = _first_child_text(item, ("published", "pubdate", "date", "updated"))
            title = _first_child_text(item, ("title",))
            values = {"source": "feed"}
            parsed_date = _date_to_iso(published)
            if parsed_date:
                values["published_at"] = parsed_date
            if title:
                values["discovered_title"] = clean_text(title)
            entries[link] = values
            if len(entries) >= max_items:
                break
    return entries


def _discover_from_sitemaps(
    session: requests.Session,
    base_url: str,
    timeout: int,
    include_keywords: list[str],
    exclude_keywords: list[str],
    errors: list[str],
) -> dict[str, dict[str, str]]:
    sitemap_candidates = [
        urljoin(base_url, "/sitemap.xml"),
        urljoin(base_url, "/sitemap_index.xml"),
        urljoin(base_url, "/sitemap-index.xml"),
        urljoin(base_url, "/sitemap/sitemap.xml"),
    ]
    queue = deque(sitemap_candidates)
    seen_sitemaps: set[str] = set()
    entries: dict[str, dict[str, str]] = {}

    while queue and len(seen_sitemaps) < 30:
        sitemap_url = normalize_url(queue.popleft())
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)
        try:
            response = session.get(sitemap_url, timeout=timeout)
            if response.status_code >= 400:
                continue
            root = ET.fromstring(response.content)
        except Exception as exc:
            errors.append(f"Sitemap read failed: {sitemap_url} ({exc})")
            continue

        for node in list(root):
            node_name = _local_name(node.tag)
            loc = _first_child_text(node, ("loc",))
            if not loc:
                continue
            loc_url = normalize_url(loc)
            if node_name == "sitemap" or loc_url.lower().endswith((".xml", ".xml.gz")):
                if same_domain(loc_url, base_url):
                    queue.append(loc_url)
                continue
            if node_name != "url" or not _is_trackable_url(
                loc_url, base_url, include_keywords, exclude_keywords
            ):
                continue
            values = {"source": "sitemap"}
            lastmod = _date_to_iso(_first_child_text(node, ("lastmod",)))
            if lastmod:
                values["lastmod"] = lastmod
            entries[loc_url] = values

    return entries


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

        soup = BeautifulSoup(response.content, "html.parser")
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
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip",
        ".xml", ".xml.gz", ".json", ".js", ".css", ".ico", ".woff", ".woff2",
    )
    return path.lower().endswith(ignored_extensions)


def _rank_urls(
    base_url: str,
    priority_urls: list[str],
    metadata: dict[str, dict[str, str]],
    include_keywords: list[str],
    exclude_keywords: list[str],
) -> list[str]:
    candidates = [
        url
        for url in metadata
        if _is_trackable_url(url, base_url, include_keywords, exclude_keywords)
    ]
    priority_set = set(priority_urls)
    discovery_order = {url: index for index, url in enumerate(metadata)}

    def score(url: str) -> tuple[int, float, int, int, int, str]:
        path = urlparse(url).path.lower() or "/"
        source = metadata.get(url, {}).get("source", "")
        freshness = _date_sort_value(
            metadata.get(url, {}).get("published_at")
            or metadata.get(url, {}).get("lastmod")
        )
        if url in priority_set:
            bucket = 0
        elif source == "feed":
            bucket = 1
        elif freshness and any(
            word in path for word in ("blog", "article", "resource", "knowledge", "news")
        ):
            bucket = 2
        elif any(word in path for word in ("blog", "article", "resource", "knowledge", "news")):
            bucket = 3
        elif any(word in path for word in ("pricing", "price", "quote")):
            bucket = 4
        elif any(word in path for word in ("service", "capabilities", "manufacturing")):
            bucket = 5
        else:
            bucket = 6
        return (
            bucket,
            -freshness,
            discovery_order.get(url, 999999),
            path.count("/"),
            len(path),
            path,
        )

    return sorted(_dedupe(candidates), key=score)


def _feed_item_link(item: ET.Element, feed_url: str) -> str:
    for child in list(item):
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href") or child.text
        rel = child.attrib.get("rel", "alternate")
        if href and rel in {"", "alternate"}:
            return normalize_url(href, feed_url)
    return ""


def _first_child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    wanted = set(names)
    for child in list(node):
        if _local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _date_to_iso(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        pass
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _date_sort_value(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result

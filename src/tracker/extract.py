from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Browser, Page, sync_playwright

from .utils import (
    category_for_url,
    clean_text,
    content_hash,
    normalize_monitored_text,
    normalize_url,
    truncate,
)


VIEWPORTS = {
    "desktop": {"width": 1440, "height": 1200},
    "mobile": {"width": 390, "height": 1200},
}

STATIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

DEFAULT_IGNORE_SELECTORS = (
    "script", "style", "noscript", "template", "svg",
    "[id*='cookie' i]", "[class*='cookie' i]",
    "[id*='consent' i]", "[class*='consent' i]",
    "[id*='intercom' i]", "[class*='intercom' i]",
    "[id*='chat' i]", "[class*='chat-widget' i]",
    "[data-view-count]", "[class*='view-count' i]", "[class*='views-count' i]",
)


class PageCollector:
    def __init__(self, timeout_seconds: int = 25) -> None:
        self.timeout_ms = timeout_seconds * 1000
        self._playwright = None
        self.browser: Browser | None = None

    def __enter__(self) -> "PageCollector":
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.browser:
            self.browser.close()
        if self._playwright:
            self._playwright.stop()

    def collect(
        self,
        url: str,
        competitor: str,
        screenshot_paths: dict[str, Path] | None = None,
        extraction_rules: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.browser:
            raise RuntimeError("PageCollector must be used as a context manager.")

        context = self.browser.new_context(
            user_agent=STATIC_HEADERS["User-Agent"],
            viewport=VIEWPORTS["desktop"],
            locale="en-US",
        )
        page = context.new_page()
        page.set_default_timeout(self.timeout_ms)
        status = None
        final_url = url
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            status = response.status if response else None
            final_url = normalize_url(page.url)
            _settle_page(page)
            snapshot = extract_snapshot(
                page.content(),
                url=final_url,
                competitor=competitor,
                status=status,
                extraction_rules=extraction_rules,
            )

            if screenshot_paths:
                for viewport_name, path in screenshot_paths.items():
                    viewport = VIEWPORTS.get(viewport_name, VIEWPORTS["desktop"])
                    page.set_viewport_size(viewport)
                    _hide_noisy_elements(page)
                    page.screenshot(path=str(path), full_page=True, animations="disabled")
            return snapshot
        finally:
            context.close()


def _settle_page(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(1200)


def _hide_noisy_elements(page: Page) -> None:
    css = ",\n".join(DEFAULT_IGNORE_SELECTORS) + " { visibility: hidden !important; opacity: 0 !important; }"
    try:
        page.add_style_tag(content=css)
    except Exception:
        pass


def extract_snapshot(
    html: str | bytes,
    url: str,
    competitor: str,
    status: int | None,
    extraction_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rules = extraction_rules or {}
    soup = BeautifulSoup(html, "html.parser")

    published_at = _published_date(soup)
    modified_at = _modified_date(soup)
    title = clean_text(soup.title.string if soup.title and soup.title.string else "")
    meta_description = _meta_content(soup, "description")
    canonical = _canonical(soup, url)

    for selector in [*DEFAULT_IGNORE_SELECTORS, *rules.get("ignore_selectors", [])]:
        try:
            for tag in soup.select(selector):
                tag.decompose()
        except Exception:
            continue

    root = _content_root(soup, rules.get("content_selector", ""))
    h1 = _first_text(root, "h1")
    headings = _headings(root)
    ctas = _ctas(root, url)
    sections = _sections(root, rules.get("ignore_text_patterns", []))
    images = _images(root)
    raw_text = root.get_text(" ", strip=True)
    monitored_text = _normalize_for_monitoring(raw_text, rules.get("ignore_text_patterns", []))

    if not published_at:
        published_at = _date_from_visible_text(monitored_text)

    return {
        "schema_version": 2,
        "competitor": competitor,
        "url": url,
        "status": status,
        "category": category_for_url(url, title),
        "title": title,
        "meta_description": meta_description,
        "canonical": canonical,
        "published_at": published_at,
        "modified_at": modified_at,
        "h1": h1,
        "headings": headings[:80],
        "ctas": ctas[:60],
        "sections": sections[:40],
        "images": images[:80],
        "text_excerpt": truncate(monitored_text, 1200),
        "monitored_text": truncate(monitored_text, 40000),
        "text_hash": content_hash(monitored_text),
        "structure_hash": content_hash(
            {
                "title": title,
                "meta_description": meta_description,
                "h1": h1,
                "headings": headings[:80],
                "ctas": ctas[:60],
                "sections": sections[:40],
                "images": images[:80],
            }
        ),
    }


def collect_static(
    url: str,
    competitor: str,
    timeout_seconds: int = 25,
    extraction_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = requests.get(url, headers=STATIC_HEADERS, timeout=timeout_seconds)
    final_url = normalize_url(response.url)
    return extract_snapshot(
        response.content,
        url=final_url,
        competitor=competitor,
        status=response.status_code,
        extraction_rules=extraction_rules,
    )


def _content_root(soup: BeautifulSoup, selector: str) -> Tag:
    if selector:
        try:
            selected = soup.select_one(selector)
            if selected:
                return selected
        except Exception:
            pass
    return soup.find("article") or soup.find("main") or soup.body or soup


def _meta_content(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": re.compile(f"^{re.escape(name)}$", re.I)})
    return clean_text(tag.get("content", "")) if tag else ""


def _canonical(soup: BeautifulSoup, url: str) -> str:
    tag = soup.find("link", rel=lambda value: value and "canonical" in value)
    return normalize_url(tag.get("href", ""), url) if tag and tag.get("href") else ""


def _first_text(root: Tag, selector: str) -> str:
    tag = root.select_one(selector)
    return clean_text(tag.get_text(" ", strip=True)) if tag else ""


def _headings(root: Tag) -> list[dict[str, str]]:
    result = []
    for tag in root.find_all(["h1", "h2", "h3"]):
        text = clean_text(tag.get_text(" ", strip=True))
        if text:
            result.append({"level": tag.name, "text": text})
    return result


def _ctas(root: Tag, page_url: str) -> list[dict[str, str]]:
    result = []
    seen = set()
    cta_words = re.compile(
        r"(quote|pricing|price|contact|demo|get started|start|upload|instant|request|"
        r"learn more|order|buy|consult|talk|try|sign up|submit)",
        re.I,
    )
    for tag in root.find_all(["a", "button"]):
        text = clean_text(tag.get_text(" ", strip=True))
        if not text or len(text) > 90:
            continue
        raw_href = clean_text(tag.get("href", ""))
        if not cta_words.search(text) and not cta_words.search(raw_href):
            continue
        href = normalize_url(raw_href, page_url) if raw_href else ""
        key = (text.lower(), href)
        if key not in seen:
            seen.add(key)
            result.append({"text": text, "href": href})
    return result


def _sections(root: Tag, ignore_patterns: list[str]) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    seen_headings: set[str] = set()
    for heading in root.find_all(["h1", "h2", "h3"]):
        heading_text = clean_text(heading.get_text(" ", strip=True))
        key = heading_text.casefold()
        if not heading_text or key in seen_headings:
            continue
        seen_headings.add(key)

        parts = [heading_text]
        for sibling in heading.find_next_siblings(limit=8):
            if getattr(sibling, "name", "") in {"h1", "h2", "h3"}:
                break
            value = clean_text(sibling.get_text(" ", strip=True))
            if value:
                parts.append(value)
        if len(" ".join(parts)) < len(heading_text) + 20 and heading.parent:
            nearby_headings = heading.parent.find_all(["h1", "h2", "h3"], limit=2)
            if len(nearby_headings) <= 1:
                parts.append(clean_text(heading.parent.get_text(" ", strip=True)))

        text = _normalize_for_monitoring(" ".join(parts), ignore_patterns)
        sections.append(
            {
                "heading": heading_text,
                "level": heading.name,
                "text": truncate(text, 1200),
                "hash": content_hash(text),
            }
        )
    return sections


def _images(root: Tag) -> list[dict[str, str]]:
    result = []
    seen = set()
    for img in root.find_all("img"):
        src = clean_text(img.get("src", "") or img.get("data-src", ""))
        alt = clean_text(img.get("alt", ""))
        if not src and not alt:
            continue
        key = (src, alt)
        if key not in seen:
            seen.add(key)
            result.append({"src": truncate(src, 180), "alt": truncate(alt, 120)})
    return result


def _normalize_for_monitoring(value: str, ignore_patterns: list[str]) -> str:
    normalized = normalize_monitored_text(value)
    for pattern in ignore_patterns:
        try:
            normalized = re.sub(pattern, "<ignored>", normalized, flags=re.I)
        except re.error:
            continue
    return clean_text(normalized)


def _published_date(soup: BeautifulSoup) -> str:
    candidates = _json_ld_dates(soup, "datePublished")
    candidates.extend(_meta_dates(soup, ("article:published_time", "datePublished", "publish-date", "date")))
    return next((date for value in candidates if (date := _date_to_iso(value))), "")


def _modified_date(soup: BeautifulSoup) -> str:
    candidates = _json_ld_dates(soup, "dateModified")
    candidates.extend(_meta_dates(soup, ("article:modified_time", "dateModified", "last-modified")))
    return next((date for value in candidates if (date := _date_to_iso(value))), "")


def _json_ld_dates(soup: BeautifulSoup, key: str) -> list[str]:
    values: list[str] = []
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(tag.string or tag.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if item.get(key):
                    values.append(str(item[key]))
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return values


def _meta_dates(soup: BeautifulSoup, names: tuple[str, ...]) -> list[str]:
    wanted = {name.casefold() for name in names}
    values = []
    for tag in soup.find_all("meta"):
        name = clean_text(tag.get("property", "") or tag.get("name", "")).casefold()
        if name in wanted and tag.get("content"):
            values.append(str(tag["content"]))
    return values


def _date_from_visible_text(value: str) -> str:
    # Fallback for sites such as PCBWay that print dates but omit article metadata.
    patterns = (
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s*\d{4}\b",
        r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            parsed = _date_to_iso(match.group(0))
            if parsed:
                return parsed
    return ""


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
        for date_format in ("%b %d,%Y", "%b %d, %Y", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(value, date_format)
                break
            except ValueError:
                continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()

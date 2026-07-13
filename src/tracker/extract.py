from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Browser, Page, sync_playwright

from .utils import category_for_url, clean_text, content_hash, normalize_url, truncate


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
    ) -> dict[str, Any]:
        if not self.browser:
            raise RuntimeError("PageCollector must be used as a context manager.")

        context = self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
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
            html = page.content()
            snapshot = extract_snapshot(html, url=final_url, competitor=competitor, status=status)

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
    css = """
    [id*="cookie" i], [class*="cookie" i], [aria-label*="cookie" i],
    [id*="consent" i], [class*="consent" i],
    [class*="intercom" i], [id*="intercom" i],
    iframe[src*="intercom" i],
    [class*="chat" i], [id*="chat" i],
    [class*="popup" i], [id*="popup" i],
    [class*="modal" i], [id*="modal" i] {
      visibility: hidden !important;
      opacity: 0 !important;
    }
    """
    try:
        page.add_style_tag(content=css)
    except Exception:
        pass


def extract_snapshot(html: str, url: str, competitor: str, status: int | None) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    title = clean_text(soup.title.string if soup.title and soup.title.string else "")
    meta_description = _meta_content(soup, "description")
    canonical = _canonical(soup)
    h1 = _first_text(soup, "h1")
    headings = _headings(soup)
    ctas = _ctas(soup)
    sections = _sections(soup)
    images = _images(soup)
    body_text = clean_text(soup.get_text(" ", strip=True))

    return {
        "competitor": competitor,
        "url": url,
        "status": status,
        "category": category_for_url(url, title),
        "title": title,
        "meta_description": meta_description,
        "canonical": canonical,
        "h1": h1,
        "headings": headings[:80],
        "ctas": ctas[:60],
        "sections": sections[:40],
        "images": images[:80],
        "text_excerpt": truncate(body_text, 1200),
        "text_hash": content_hash(body_text),
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


def collect_static(url: str, competitor: str, timeout_seconds: int = 25) -> dict[str, Any]:
    response = requests.get(url, headers=STATIC_HEADERS, timeout=timeout_seconds)
    final_url = normalize_url(response.url)
    return extract_snapshot(
        response.text,
        url=final_url,
        competitor=competitor,
        status=response.status_code,
    )


def _meta_content(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": re.compile(f"^{re.escape(name)}$", re.I)})
    if not tag:
        return ""
    return clean_text(tag.get("content", ""))


def _canonical(soup: BeautifulSoup) -> str:
    tag = soup.find("link", rel=lambda value: value and "canonical" in value)
    return clean_text(tag.get("href", "")) if tag else ""


def _first_text(soup: BeautifulSoup, selector: str) -> str:
    tag = soup.select_one(selector)
    return clean_text(tag.get_text(" ", strip=True)) if tag else ""


def _headings(soup: BeautifulSoup) -> list[dict[str, str]]:
    result = []
    for tag in soup.find_all(["h1", "h2", "h3"]):
        text = clean_text(tag.get_text(" ", strip=True))
        if text:
            result.append({"level": tag.name, "text": text})
    return result


def _ctas(soup: BeautifulSoup) -> list[dict[str, str]]:
    result = []
    seen = set()
    cta_words = re.compile(
        r"(quote|pricing|price|contact|demo|get started|start|upload|instant|request|"
        r"learn more|order|buy|consult|talk|try|sign up|submit)",
        re.I,
    )
    for tag in soup.find_all(["a", "button"]):
        text = clean_text(tag.get_text(" ", strip=True))
        if not text or len(text) > 90:
            continue
        href = clean_text(tag.get("href", ""))
        if not cta_words.search(text) and not cta_words.search(href):
            continue
        key = (text.lower(), href)
        if key in seen:
            continue
        seen.add(key)
        result.append({"text": text, "href": href})
    return result


def _sections(soup: BeautifulSoup) -> list[dict[str, str]]:
    main = soup.find("main") or soup.body or soup
    sections: list[dict[str, str]] = []
    seen_headings: set[str] = set()

    for heading in main.find_all(["h1", "h2", "h3"]):
        heading_text = clean_text(heading.get_text(" ", strip=True))
        if not heading_text:
            continue
        container = heading.find_parent(["section", "article"]) or heading.parent
        text = clean_text(container.get_text(" ", strip=True)) if container else heading_text
        if len(text) < len(heading_text) + 20:
            siblings = []
            for sibling in heading.find_next_siblings(limit=5):
                if getattr(sibling, "name", "") in {"h1", "h2", "h3"}:
                    break
                siblings.append(clean_text(sibling.get_text(" ", strip=True)))
            text = clean_text(" ".join([heading_text, *siblings]))
        key = heading_text.lower()
        if key in seen_headings:
            continue
        seen_headings.add(key)
        sections.append(
            {
                "heading": heading_text,
                "level": heading.name,
                "text": truncate(text, 700),
                "hash": content_hash(text),
            }
        )
    return sections


def _images(soup: BeautifulSoup) -> list[dict[str, str]]:
    result = []
    seen = set()
    for img in soup.find_all("img"):
        src = clean_text(img.get("src", "") or img.get("data-src", ""))
        alt = clean_text(img.get("alt", ""))
        if not src and not alt:
            continue
        key = (src, alt)
        if key in seen:
            continue
        seen.add(key)
        result.append({"src": truncate(src, 180), "alt": truncate(alt, 120)})
    return result

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from tracker.diffing import diff_snapshots
from tracker.discovery import _rank_urls
from tracker.extract import extract_snapshot
from tracker.reports import build_daily_report
from tracker.storage import TrackerStorage
from tracker.utils import clean_text, normalize_monitored_text


class TrackerV2Tests(unittest.TestCase):
    def test_repairs_mojibake_and_ignores_live_counters(self) -> None:
        bad = "Xometry" + chr(0x00E2) + chr(0x20AC) + chr(0x2122) + "s"
        self.assertEqual(clean_text(bad), "Xometry’s")
        self.assertEqual(
            normalize_monitored_text("Excellent part 81 views"),
            "Excellent part <dynamic-value>",
        )

    def test_extracts_publish_date_and_classifies_late_discovery(self) -> None:
        html = """
        <html><head><title>New capability</title>
        <meta property="article:published_time" content="2026-07-08T08:00:00Z">
        </head><body><article><h1>New capability</h1><p>Titanium machining.</p></article></body></html>
        """
        snapshot = extract_snapshot(html, "https://example.com/blog/new", "Example", 200)
        snapshot["first_seen_at"] = "2026-07-30T08:00:00+00:00"
        events = diff_snapshots(
            None,
            snapshot,
            now=datetime(2026, 7, 30, tzinfo=timezone.utc),
            new_content_max_age_days=10,
        )
        self.assertEqual(events[0]["type"], "historical_page")
        self.assertIn("22 天", events[0]["details"][-1])

    def test_fresh_dated_article_is_new_content(self) -> None:
        snapshot = {
            "schema_version": 2,
            "competitor": "Example",
            "url": "https://example.com/blog/new",
            "category": "content",
            "title": "Fresh article",
            "h1": "Fresh article",
            "published_at": "2026-07-29T08:00:00+00:00",
            "first_seen_at": "2026-07-30T08:00:00+00:00",
        }
        events = diff_snapshots(
            None,
            snapshot,
            now=datetime(2026, 7, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(events[0]["type"], "new_content")

    def test_content_diff_contains_real_additions_and_deletions(self) -> None:
        previous = _snapshot("We offer aluminum CNC machining with five day lead time.")
        current = _snapshot("We offer titanium CNC machining with three day lead time.")
        current["text_hash"] = "changed"
        events = diff_snapshots(previous, current, minimum_text_change_percent=0.1)
        event = next(event for event in events if event["type"] == "content_changed")
        self.assertTrue(any("titanium" in value for value in event["added"]))
        self.assertTrue(any("aluminum" in value for value in event["removed"]))

    def test_v1_snapshot_migrates_without_false_alert(self) -> None:
        previous = _snapshot("Old whole-page text")
        previous.pop("schema_version")
        current = _snapshot("New article-only text")
        current["text_hash"] = "changed"
        self.assertEqual(diff_snapshots(previous, current), [])

    def test_fresh_feed_items_rank_before_undated_sitemap_content(self) -> None:
        base = "https://example.com/"
        metadata = {
            "https://example.com/blog/old": {"source": "sitemap"},
            "https://example.com/blog/new": {
                "source": "feed",
                "published_at": "2026-07-30T00:00:00+00:00",
            },
        }
        ranked = _rank_urls(base, [], metadata, ["blog"], [])
        self.assertEqual(ranked[0], "https://example.com/blog/new")

    def test_email_marks_historical_content_and_renders_colored_diff(self) -> None:
        changed = {
            "type": "content_changed",
            "priority": "high",
            "competitor": "Example",
            "url": "https://example.com/blog/a",
            "category": "content",
            "page_title": "Article",
            "summary": "页面正文发生可定位的变化",
            "added": ["titanium machining"],
            "removed": ["aluminum machining"],
            "change_percent": 5.0,
            "details": [],
        }
        historical = {
            "type": "historical_page",
            "priority": "low",
            "competitor": "Example",
            "url": "https://example.com/blog/old",
            "category": "content",
            "page_title": "Old article",
            "summary": "历史内容补录：Old article",
            "published_at": "2026-07-08T00:00:00+00:00",
            "first_seen_at": "2026-07-30T00:00:00+00:00",
            "details": ["发现延迟：22 天"],
        }
        report = build_daily_report(
            {"timezone": "Asia/Shanghai", "email": {}},
            {"events": [changed, historical], "errors": [], "discovery": []},
        )
        self.assertIn("历史内容补录（不计入今日新闻）", report["email"]["html"])
        self.assertIn("diff-added", report["email"]["html"])
        self.assertIn("diff-removed", report["email"]["html"])

    def test_unchanged_page_does_not_rewrite_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            storage = TrackerStorage(temp_dir)
            first = _snapshot("Stable content") | {"fetched_at": "2026-07-30T00:00:00Z"}
            second = _snapshot("Stable content") | {"fetched_at": "2026-07-31T00:00:00Z"}
            self.assertTrue(storage.save_page(first["url"], first))
            self.assertFalse(storage.save_page(second["url"], second))


def _snapshot(text: str) -> dict:
    return {
        "schema_version": 2,
        "competitor": "Example",
        "url": "https://example.com/page",
        "category": "page",
        "title": "Page",
        "meta_description": "",
        "h1": "Page",
        "headings": [],
        "ctas": [],
        "sections": [],
        "monitored_text": text,
        "text_hash": text,
    }


if __name__ == "__main__":
    unittest.main()

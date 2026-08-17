from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from .utils import clean_text, truncate


IMPORTANT_CATEGORIES = {"homepage", "pricing", "service"}


def diff_snapshots(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    *,
    now: datetime | None = None,
    new_content_max_age_days: int = 10,
    minimum_text_change_percent: float = 0.5,
    emit_new_page: bool = True,
) -> list[dict[str, Any]]:
    if previous is None:
        return _new_page_events(
            current,
            now=now or datetime.now(timezone.utc),
            max_age_days=new_content_max_age_days,
        ) if emit_new_page else []

    # Existing v1 snapshots used whole-page text. Quietly migrate once instead of
    # reporting the extraction-model change as hundreds of competitor changes.
    if int(previous.get("schema_version", 1)) < int(current.get("schema_version", 2)):
        return []

    events: list[dict[str, Any]] = []
    for field, label in [
        ("title", "SEO 标题"),
        ("meta_description", "Meta 描述"),
        ("h1", "H1 标题"),
    ]:
        before = clean_text(previous.get(field, ""))
        after = clean_text(current.get(field, ""))
        if before != after:
            text_diff = _text_diff(before, after)
            events.append(
                _event(
                    current,
                    event_type=f"{field}_changed",
                    priority=_priority(current, field),
                    summary=f"{label}发生变化",
                    before=before,
                    after=after,
                    added=text_diff["added"],
                    removed=text_diff["removed"],
                    change_percent=text_diff["change_percent"],
                )
            )

    events.extend(_list_diffs(previous, current, "headings", "页面标题结构"))
    events.extend(_list_diffs(previous, current, "ctas", "CTA"))
    events.extend(_section_diffs(previous, current, minimum_text_change_percent))

    if not events and previous.get("text_hash") != current.get("text_hash"):
        before_text = previous.get("monitored_text", "")
        after_text = current.get("monitored_text", "")
        text_diff = _text_diff(before_text, after_text)
        if text_diff["change_percent"] >= minimum_text_change_percent and (
            text_diff["added"] or text_diff["removed"]
        ):
            events.append(
                _event(
                    current,
                    event_type="content_changed",
                    priority=_priority(current, "content"),
                    summary="页面正文发生可定位的变化",
                    before=before_text,
                    after=after_text,
                    added=text_diff["added"],
                    removed=text_diff["removed"],
                    change_percent=text_diff["change_percent"],
                )
            )
    return events


def visual_event(current: dict[str, Any], viewport: str, metrics: dict[str, float]) -> dict[str, Any]:
    details = [
        f"视口：{viewport}",
        f"RMS 差异：{metrics['rms']:.2f}",
        f"变化像素占比：{metrics['changed_pixels_percent']:.2f}%",
    ]
    if metrics.get("height_delta"):
        details.append(f"页面截图高度变化：{metrics['height_delta']:+.0f}px")
    return _event(
        current,
        event_type="visual_changed",
        priority=_priority(current, "visual"),
        summary=f"{viewport} 视图出现明显 UI/布局变化",
        details=details,
    )


def health_event(
    competitor: str,
    base_url: str,
    summary: str,
    details: list[str],
    priority: str = "high",
) -> dict[str, Any]:
    return {
        "type": "monitor_health",
        "priority": priority,
        "competitor": competitor,
        "url": base_url,
        "category": "system",
        "page_title": "监控健康告警",
        "summary": summary,
        "details": details,
        "before": None,
        "after": None,
        "added": [],
        "removed": [],
        "change_percent": 0.0,
        "confidence": "high",
    }


def _new_page_events(current: dict[str, Any], now: datetime, max_age_days: int) -> list[dict[str, Any]]:
    published = _parse_datetime(current.get("published_at"))
    first_seen = _parse_datetime(current.get("first_seen_at")) or now
    age_days = max(0, (now - published).days) if published else None
    delay_days = max(0, (first_seen - published).days) if published else None
    is_content = current.get("category") == "content"

    details = [
        _line("标题", current.get("title")),
        _line("H1", current.get("h1")),
        _line("实际发布时间", _date_label(current.get("published_at"))),
        _line("系统首次发现", _date_label(current.get("first_seen_at"))),
        f"发现延迟：{delay_days} 天" if delay_days is not None else "发布时间：页面未提供，需人工确认",
    ]
    if is_content and published and age_days is not None and age_days > max_age_days:
        return [
            _event(
                current,
                event_type="historical_page",
                priority="low",
                summary=f"历史内容补录：{current.get('title') or current['url']}",
                details=details,
                confidence="high",
            )
        ]
    if is_content and published:
        event_type = "new_content"
        summary = f"新发布内容：{current.get('title') or current['url']}"
        confidence = "high"
    else:
        event_type = "new_page"
        summary = f"新发现页面：{current.get('title') or current['url']}"
        confidence = "medium" if is_content else "high"
    return [
        _event(
            current,
            event_type=event_type,
            priority=_priority(current, event_type),
            summary=summary,
            details=details,
            confidence=confidence,
        )
    ]


def _section_diffs(
    previous: dict[str, Any],
    current: dict[str, Any],
    minimum_text_change_percent: float,
) -> list[dict[str, Any]]:
    old_sections = _section_map(previous.get("sections", []))
    new_sections = _section_map(current.get("sections", []))
    events = []

    for key, section in new_sections.items():
        if key not in old_sections:
            events.append(
                _event(
                    current,
                    event_type="section_added",
                    priority=_priority(current, "section_added"),
                    summary=f"新增板块：{section['heading']}",
                    added=[truncate(section.get("text", ""), 360)],
                )
            )

    for key, section in old_sections.items():
        if key not in new_sections:
            events.append(
                _event(
                    current,
                    event_type="section_removed",
                    priority=_priority(current, "section_removed"),
                    summary=f"删除板块：{section['heading']}",
                    removed=[truncate(section.get("text", ""), 360)],
                )
            )

    for key, new_section in new_sections.items():
        old_section = old_sections.get(key)
        if not old_section or old_section.get("hash") == new_section.get("hash"):
            continue
        before = old_section.get("text", "")
        after = new_section.get("text", "")
        text_diff = _text_diff(before, after)
        if text_diff["change_percent"] < minimum_text_change_percent:
            continue
        events.append(
            _event(
                current,
                event_type="section_copy_changed",
                priority=_priority(current, "section_copy"),
                summary=f"板块文案更新：{new_section['heading']}",
                before=before,
                after=after,
                added=text_diff["added"],
                removed=text_diff["removed"],
                change_percent=text_diff["change_percent"],
            )
        )
    return events


def _list_diffs(
    previous: dict[str, Any],
    current: dict[str, Any],
    field: str,
    label: str,
) -> list[dict[str, Any]]:
    old_items = _item_set(previous.get(field, []))
    new_items = _item_set(current.get(field, []))
    added = sorted(new_items - old_items)
    removed = sorted(old_items - new_items)
    if not added and not removed:
        return []
    return [
        _event(
            current,
            event_type=f"{field}_changed",
            priority=_priority(current, field),
            summary=f"{label}发生变化",
            added=[truncate(item, 180) for item in added[:8]],
            removed=[truncate(item, 180) for item in removed[:8]],
        )
    ]


def _item_set(items: list[dict[str, Any]]) -> set[str]:
    result = set()
    for item in items:
        text = clean_text(item.get("text", ""))
        if not text:
            continue
        if "href" in item:
            href = clean_text(item.get("href", ""))
            result.add(f"{text} -> {href}" if href else text)
        else:
            level = clean_text(item.get("level", ""))
            result.add(f"{level.upper()} {text}" if level else text)
    return result


def _section_map(sections: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        heading.casefold(): section
        for section in sections
        if (heading := clean_text(section.get("heading", "")))
    }


def _text_diff(before: str, after: str) -> dict[str, Any]:
    old_words = clean_text(before).split()
    new_words = clean_text(after).split()
    matcher = SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    added: list[str] = []
    removed: list[str] = []
    changed_words = 0
    for operation, i1, i2, j1, j2 in matcher.get_opcodes():
        if operation == "equal":
            continue
        changed_words += (i2 - i1) + (j2 - j1)
        if operation in {"replace", "delete"} and i2 > i1:
            removed.append(truncate(" ".join(old_words[i1:i2]), 360))
        if operation in {"replace", "insert"} and j2 > j1:
            added.append(truncate(" ".join(new_words[j1:j2]), 360))
    denominator = max(len(old_words), len(new_words), 1)
    return {
        "added": [value for value in added[:5] if value],
        "removed": [value for value in removed[:5] if value],
        "change_percent": round(changed_words * 100 / denominator, 2),
    }


def _event(
    snapshot: dict[str, Any],
    event_type: str,
    priority: str,
    summary: str,
    details: list[str] | None = None,
    before: str | None = None,
    after: str | None = None,
    added: list[str] | None = None,
    removed: list[str] | None = None,
    change_percent: float = 0.0,
    confidence: str = "high",
) -> dict[str, Any]:
    return {
        "type": event_type,
        "priority": priority,
        "competitor": snapshot["competitor"],
        "url": snapshot["url"],
        "category": snapshot.get("category", "page"),
        "page_title": snapshot.get("title", ""),
        "summary": summary,
        "details": [detail for detail in (details or []) if detail],
        "before": before,
        "after": after,
        "added": added or [],
        "removed": removed or [],
        "change_percent": change_percent,
        "confidence": confidence,
        "published_at": snapshot.get("published_at", ""),
        "first_seen_at": snapshot.get("first_seen_at", ""),
        "discovered_via": snapshot.get("discovered_via", ""),
    }


def _priority(snapshot: dict[str, Any], change_type: str) -> str:
    category = snapshot.get("category")
    if category == "pricing":
        return "high"
    if category == "content" and change_type in {
        "new_content", "content", "title", "h1", "title_changed", "h1_changed"
    }:
        return "high"
    if category == "homepage" and change_type in {"h1", "title", "section_added", "visual"}:
        return "high"
    if category in IMPORTANT_CATEGORIES:
        return "medium"
    if change_type in {"new_page", "section_added", "ctas"}:
        return "medium"
    return "low"


def _line(label: str, value: str | None) -> str:
    value = clean_text(value)
    return f"{label}：{value}" if value else ""


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _date_label(value: str | None) -> str:
    parsed = _parse_datetime(value)
    return parsed.date().isoformat() if parsed else ""

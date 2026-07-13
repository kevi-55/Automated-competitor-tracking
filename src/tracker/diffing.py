from __future__ import annotations

from typing import Any

from .utils import clean_text, truncate


IMPORTANT_CATEGORIES = {"homepage", "pricing", "service"}


def diff_snapshots(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    if previous is None:
        return [
            _event(
                current,
                event_type="new_page",
                priority=_priority(current, "new_page"),
                summary=f"新发现页面：{current.get('title') or current['url']}",
                details=[
                    _line("H1", current.get("h1")),
                    _line("标题", current.get("title")),
                    _line("分类", current.get("category")),
                ],
            )
        ]

    events: list[dict[str, Any]] = []
    for field, label in [
        ("title", "SEO 标题"),
        ("meta_description", "Meta description"),
        ("h1", "H1 标题"),
    ]:
        before = clean_text(previous.get(field, ""))
        after = clean_text(current.get(field, ""))
        if before != after:
            events.append(
                _event(
                    current,
                    event_type=f"{field}_changed",
                    priority=_priority(current, field),
                    summary=f"{label}发生变化",
                    before=before,
                    after=after,
                    details=[f"{label}：{_before_after(before, after)}"],
                )
            )

    events.extend(_list_diffs(previous, current, "headings", "页面标题结构"))
    events.extend(_list_diffs(previous, current, "ctas", "CTA"))
    events.extend(_section_diffs(previous, current))

    if not events and previous.get("text_hash") != current.get("text_hash"):
        events.append(
            _event(
                current,
                event_type="content_changed",
                priority=_priority(current, "content"),
                summary="页面正文发生变化，但未定位到明确的标题或板块变化",
                details=[
                    f"旧正文摘要：{truncate(previous.get('text_excerpt', ''), 220)}",
                    f"新正文摘要：{truncate(current.get('text_excerpt', ''), 220)}",
                ],
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


def _section_diffs(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
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
                    details=[f"新增文案：{truncate(section.get('text', ''), 300)}"],
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
                    details=[f"原文案：{truncate(section.get('text', ''), 260)}"],
                )
            )

    for key, new_section in new_sections.items():
        old_section = old_sections.get(key)
        if old_section and old_section.get("hash") != new_section.get("hash"):
            events.append(
                _event(
                    current,
                    event_type="section_copy_changed",
                    priority=_priority(current, "section_copy"),
                    summary=f"板块文案更新：{new_section['heading']}",
                    before=old_section.get("text", ""),
                    after=new_section.get("text", ""),
                    details=[
                        f"旧文案：{truncate(old_section.get('text', ''), 260)}",
                        f"新文案：{truncate(new_section.get('text', ''), 260)}",
                    ],
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

    details = []
    if added:
        details.append("新增：" + "；".join(truncate(item, 120) for item in added[:8]))
    if removed:
        details.append("删除：" + "；".join(truncate(item, 120) for item in removed[:8]))

    return [
        _event(
            current,
            event_type=f"{field}_changed",
            priority=_priority(current, field),
            summary=f"{label}发生变化",
            details=details,
        )
    ]


def _item_set(items: list[dict[str, Any]]) -> set[str]:
    result = set()
    for item in items:
        if "text" in item and "href" in item:
            text = clean_text(item.get("text", ""))
            href = clean_text(item.get("href", ""))
            if text:
                result.add(f"{text} -> {href}" if href else text)
        elif "text" in item:
            level = clean_text(item.get("level", ""))
            text = clean_text(item.get("text", ""))
            if text:
                result.add(f"{level.upper()} {text}" if level else text)
    return result


def _section_map(sections: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for section in sections:
        heading = clean_text(section.get("heading", ""))
        if not heading:
            continue
        result[heading.lower()] = section
    return result


def _event(
    snapshot: dict[str, Any],
    event_type: str,
    priority: str,
    summary: str,
    details: list[str] | None = None,
    before: str | None = None,
    after: str | None = None,
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
    }


def _priority(snapshot: dict[str, Any], change_type: str) -> str:
    category = snapshot.get("category")
    if category == "pricing":
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


def _before_after(before: str, after: str) -> str:
    return f"从「{truncate(before, 140)}」改为「{truncate(after, 140)}」"


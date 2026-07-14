from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .utils import html_escape, truncate


PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

CHANGE_TYPE_LABELS = {
    "new_page": "新页面",
    "title_changed": "SEO 标题",
    "meta_description_changed": "Meta 描述",
    "h1_changed": "H1 标题",
    "headings_changed": "标题结构",
    "ctas_changed": "CTA 按钮",
    "section_added": "新增板块",
    "section_removed": "删除板块",
    "section_copy_changed": "板块文案",
    "content_changed": "正文微调",
    "visual_changed": "UI 布局",
}

CONTENT_PATH_KEYWORDS = ("blog", "article", "resource", "knowledge", "news", "case-study", "case-studies")

EMAIL_SECTIONS = [
    ("new_blog", "新增 Blog / 内容页"),
    ("new_pages", "新增其他页面"),
    ("content_updates", "内容页更新"),
    ("critical", "定价 & 首页变化"),
    ("seo", "SEO 变化"),
    ("copy", "文案 & 结构"),
    ("ui", "UI 布局变化"),
]

EMAIL_LIMITS = {
    "new_blog": 30,
    "new_pages": 15,
    "content_updates": 20,
    "critical": 10,
    "seo": 10,
    "copy": 10,
    "ui": 8,
}
EMAIL_MAX_TOTAL_CHANGES = 35


def build_daily_report(
    config: dict[str, Any],
    run_data: dict[str, Any],
) -> dict[str, Any]:
    timezone = ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    now_local = datetime.now(timezone)
    date_label = now_local.strftime("%Y-%m-%d")
    events = sorted(run_data["events"], key=_event_sort_key)
    errors = run_data.get("errors", [])
    email_events = _prepare_email_events(events)
    prefix = config.get("email", {}).get("subject_prefix", "[Competitor Tracking]")
    new_content_count = _new_content_count(email_events)
    change_count = _change_count(email_events)
    if new_content_count and change_count:
        subject = f"{prefix} 日报 {date_label} · {new_content_count} 篇新内容，{change_count} 项变化"
    elif new_content_count:
        subject = f"{prefix} 日报 {date_label} · {new_content_count} 篇新内容"
    elif change_count:
        subject = f"{prefix} 日报 {date_label} · {change_count} 项变化"
    else:
        subject = f"{prefix} 日报 {date_label} · 无重要变化"

    text = _daily_text(date_label, events, email_events, errors)
    html = _daily_html(date_label, events, email_events, errors)

    report = {
        "mode": "daily",
        "date": date_label,
        "subject": subject,
        "generated_at": now_local.isoformat(timespec="seconds"),
        "events": events,
        "errors": errors,
        "discovery": run_data.get("discovery", []),
        "email": {"text": text, "html": html},
    }
    return report


def build_weekly_report(config: dict[str, Any], reports_dir: Path) -> dict[str, Any]:
    timezone = ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    now_local = datetime.now(timezone)
    start_date = (now_local - timedelta(days=7)).date()
    daily_reports = []

    for path in sorted(reports_dir.glob("daily-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            report_date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if start_date <= report_date <= now_local.date():
            daily_reports.append(data)

    events = []
    errors = []
    for report in daily_reports:
        for event in report.get("events", []):
            event = dict(event)
            event["report_date"] = report.get("date")
            events.append(event)
        errors.extend(report.get("errors", []))

    events = sorted(events, key=_event_sort_key)
    email_events = _prepare_email_events(events)
    prefix = config.get("email", {}).get("subject_prefix", "[Competitor Tracking]")
    new_content_count = _new_content_count(email_events)
    change_count = _change_count(email_events)
    subject = f"{prefix} 周报 {start_date.isoformat()} 至 {now_local.date().isoformat()}"
    if new_content_count:
        subject += f" · {new_content_count} 篇新内容"
    if change_count:
        subject += f"，{change_count} 项变化"
    text = _weekly_text(start_date.isoformat(), now_local.date().isoformat(), events, email_events, errors)
    html = _weekly_html(start_date.isoformat(), now_local.date().isoformat(), events, email_events, errors)

    return {
        "mode": "weekly",
        "date": now_local.strftime("%Y-%m-%d"),
        "subject": subject,
        "generated_at": now_local.isoformat(timespec="seconds"),
        "source_reports": [report.get("date") for report in daily_reports],
        "events": events,
        "errors": errors,
        "email": {"text": text, "html": html},
    }


def save_report(report: dict[str, Any], reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / f"{report['mode']}-{report['date']}.json"
    html_path = reports_dir / f"{report['mode']}-{report['date']}.html"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(report["email"]["html"], encoding="utf-8")
    return json_path, html_path


def _prepare_email_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in EMAIL_SECTIONS}
    hidden_items: list[dict[str, Any]] = []

    for event in events:
        if event.get("type") == "new_page":
            bucket = _email_bucket(event)
            limit = EMAIL_LIMITS.get(bucket, 15)
            if len(grouped[bucket]) < limit:
                grouped[bucket].append(event)
            else:
                hidden_items.append(event)

    for event in events:
        if event.get("type") != "content_changed" or not _is_content_event(event):
            continue
        if len(grouped["content_updates"]) < EMAIL_LIMITS["content_updates"]:
            grouped["content_updates"].append(event)
        else:
            hidden_items.append(event)

    shown_changes = 0
    for event in events:
        if event.get("type") == "new_page":
            continue
        if event.get("type") == "content_changed" and _is_content_event(event):
            continue
        if _should_hide_from_email(event):
            hidden_items.append(event)
            continue

        bucket = _email_bucket(event)
        if bucket not in grouped:
            continue
        limit = EMAIL_LIMITS.get(bucket, 10)
        if len(grouped[bucket]) >= limit or shown_changes >= EMAIL_MAX_TOTAL_CHANGES:
            hidden_items.append(event)
            continue
        grouped[bucket].append(event)
        shown_changes += 1

    action_items = [event for section_events in grouped.values() for event in section_events]
    return {
        "action_items": action_items,
        "hidden_items": hidden_items,
        "grouped": grouped,
        "hidden_counts": _hidden_summary(hidden_items),
    }


def _is_content_event(event: dict[str, Any]) -> bool:
    if event.get("category") == "content":
        return True
    path = urlparse(event.get("url", "")).path.lower()
    return any(keyword in path for keyword in CONTENT_PATH_KEYWORDS)


def _new_content_count(email_events: dict[str, Any]) -> int:
    grouped = email_events["grouped"]
    return len(grouped.get("new_blog", [])) + len(grouped.get("content_updates", []))


def _change_count(email_events: dict[str, Any]) -> int:
    grouped = email_events["grouped"]
    change_sections = ("critical", "seo", "copy", "ui", "new_pages")
    return sum(len(grouped.get(section, [])) for section in change_sections)


def _should_hide_from_email(event: dict[str, Any]) -> bool:
    event_type = event.get("type", "")
    priority = event.get("priority", "low")
    if event_type == "new_page":
        return False
    if event_type == "content_changed" and _is_content_event(event):
        return False
    if event_type == "content_changed" and priority == "low":
        return True
    if event_type == "visual_changed" and priority == "low":
        return True
    return False


def _email_bucket(event: dict[str, Any]) -> str:
    event_type = event.get("type", "")
    category = event.get("category", "page")
    priority = event.get("priority", "low")

    if event_type == "new_page":
        return "new_blog" if _is_content_event(event) else "new_pages"
    if event_type == "content_changed" and _is_content_event(event):
        return "content_updates"
    if event_type == "visual_changed":
        return "ui"
    if category in {"pricing", "homepage"} and priority in {"high", "medium"}:
        return "critical"
    if event_type in {"title_changed", "meta_description_changed", "h1_changed"}:
        return "seo"
    if event_type in {
        "section_copy_changed",
        "section_added",
        "section_removed",
        "ctas_changed",
        "headings_changed",
        "content_changed",
    }:
        return "copy"
    return "copy"


def _hidden_summary(hidden_items: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for event in hidden_items:
        label = CHANGE_TYPE_LABELS.get(event.get("type", ""), "其他")
        counts[label] += 1
    return dict(counts)


def _daily_text(
    date_label: str,
    events: list[dict[str, Any]],
    email_events: dict[str, Any],
    errors: list[str],
) -> str:
    lines = [f"竞品追踪日报 · {date_label}", ""]
    lines.append(_overview_text(events, email_events))
    lines.append("")

    for section_key, section_title in EMAIL_SECTIONS:
        section_events = email_events["grouped"].get(section_key, [])
        if not section_events:
            continue
        lines.append(f"【{section_title}】")
        for row in _rows_for_section(section_events):
            lines.append(f"· {row['competitor']} | {row['page_label']}")
            for change in row["changes"]:
                lines.append(f"  - {change}")
            lines.append(f"  {row['url']}")
        lines.append("")

    if email_events["hidden_counts"]:
        lines.append(_hidden_text(email_events))
        lines.append("")

    if not email_events["action_items"]:
        lines.append("今天没有发现需要重点关注的页面变化。")
        lines.append("")

    if errors:
        lines.append("抓取提醒：")
        for error in errors[:10]:
            lines.append(f"- {error}")
        lines.append("")

    lines.append("完整明细见仓库 reports/ 目录中的 HTML 报告。")
    return "\n".join(lines)


def _daily_html(
    date_label: str,
    events: list[dict[str, Any]],
    email_events: dict[str, Any],
    errors: list[str],
) -> str:
    parts = [_html_shell_start(f"竞品追踪日报 · {date_label}")]
    parts.append(f"<h1>竞品追踪日报</h1><p class='muted'>{html_escape(date_label)}</p>")
    parts.append(_overview_html(events, email_events))

    has_content = False
    for section_key, section_title in EMAIL_SECTIONS:
        section_events = email_events["grouped"].get(section_key, [])
        if not section_events:
            continue
        has_content = True
        parts.append(_section_table_html(section_title, section_events, section_key=section_key))

    if not has_content:
        parts.append("<section><p>今天没有发现需要重点关注的页面变化。</p></section>")

    if email_events["hidden_counts"]:
        parts.append(_hidden_html(email_events))

    if errors:
        parts.append(_errors_html(errors))

    parts.append(
        "<p class='footer'>完整明细已保存到仓库 <code>reports/</code> 目录，"
        "可在 GitHub 上打开 HTML 文件查看全部变化。</p>"
    )
    parts.append(_html_shell_end())
    return "\n".join(parts)


def _weekly_text(
    start_date: str,
    end_date: str,
    events: list[dict[str, Any]],
    email_events: dict[str, Any],
    errors: list[str],
) -> str:
    lines = [f"竞品追踪周报 · {start_date} 至 {end_date}", "", _overview_text(events, email_events), ""]
    for section_key, section_title in EMAIL_SECTIONS:
        section_events = email_events["grouped"].get(section_key, [])
        if not section_events:
            continue
        lines.append(f"【{section_title}】")
        for row in _rows_for_section(section_events):
            date_text = row.get("report_date", "")
            prefix = f"{date_text} " if date_text else ""
            lines.append(f"· {prefix}{row['competitor']} | {row['page_label']}")
            for change in row["changes"]:
                lines.append(f"  - {change}")
        lines.append("")
    if email_events["hidden_counts"]:
        lines.append(_hidden_text(email_events))
    if errors:
        lines.append("")
        lines.append("抓取提醒：")
        for error in errors[:10]:
            lines.append(f"- {error}")
    return "\n".join(lines)


def _weekly_html(
    start_date: str,
    end_date: str,
    events: list[dict[str, Any]],
    email_events: dict[str, Any],
    errors: list[str],
) -> str:
    parts = [_html_shell_start(f"竞品追踪周报 · {start_date} 至 {end_date}")]
    parts.append("<h1>竞品追踪周报</h1>")
    parts.append(f"<p class='muted'>{html_escape(start_date)} 至 {html_escape(end_date)}</p>")
    parts.append(_overview_html(events, email_events))

    for section_key, section_title in EMAIL_SECTIONS:
        section_events = email_events["grouped"].get(section_key, [])
        if section_events:
            parts.append(_section_table_html(section_title, section_events, show_date=True, section_key=section_key))

    if email_events["hidden_counts"]:
        parts.append(_hidden_html(email_events))
    if errors:
        parts.append(_errors_html(errors))
    parts.append(_html_shell_end())
    return "\n".join(parts)


def _overview_text(events: list[dict[str, Any]], email_events: dict[str, Any]) -> str:
    if not events:
        return "今日概览：未发现变化。"
    grouped = email_events["grouped"]
    new_blog = len(grouped.get("new_blog", []))
    new_pages = len(grouped.get("new_pages", []))
    content_updates = len(grouped.get("content_updates", []))
    changes = _change_count(email_events)
    hidden_count = len(email_events["hidden_items"])
    top_competitors = ", ".join(name for name, _ in Counter(e["competitor"] for e in events).most_common(3))

    parts = [f"今日概览：共检测到 {len(events)} 项记录。"]
    if new_blog:
        parts.append(f"新增 Blog/内容页 {new_blog} 篇。")
    if new_pages:
        parts.append(f"新增其他页面 {new_pages} 个。")
    if content_updates:
        parts.append(f"内容页更新 {content_updates} 篇。")
    if changes:
        parts.append(f"页面变化 {changes} 项。")
    if hidden_count:
        parts.append(f"另有 {hidden_count} 项低优先级变化已折叠。")
    parts.append(f"变化较多：{top_competitors or '无'}。")
    return " ".join(parts)


def _overview_html(events: list[dict[str, Any]], email_events: dict[str, Any]) -> str:
    grouped = email_events["grouped"]
    new_blog = len(grouped.get("new_blog", []))
    new_pages = len(grouped.get("new_pages", []))
    content_updates = len(grouped.get("content_updates", []))
    changes = _change_count(email_events)
    hidden_count = len(email_events["hidden_items"])
    top_competitors = ", ".join(
        html_escape(name) for name, _ in Counter(e["competitor"] for e in events).most_common(3)
    )
    return (
        "<section class='summary'>"
        f"<p><strong>今日共检测到 {len(events)} 项记录。</strong></p>"
        "<table class='summary-table'><tbody>"
        f"<tr><td>新增 Blog/内容</td><td><strong>{new_blog}</strong> 篇</td></tr>"
        f"<tr><td>新增其他页面</td><td><strong>{new_pages}</strong> 个</td></tr>"
        f"<tr><td>内容页更新</td><td><strong>{content_updates}</strong> 篇</td></tr>"
        f"<tr><td>页面变化</td><td><strong>{changes}</strong> 项</td></tr>"
        f"<tr><td>已折叠</td><td>{hidden_count} 项</td></tr>"
        f"<tr><td>变化较多</td><td>{top_competitors or '无'}</td></tr>"
        "</tbody></table></section>"
    )


def _rows_for_section(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        key = (event["competitor"], event["url"])
        if key not in grouped:
            grouped[key] = {
                "competitor": event["competitor"],
                "url": event["url"],
                "page_label": _page_label(event),
                "changes": [],
                "priority": event["priority"],
                "report_date": event.get("report_date", ""),
            }
        grouped[key]["changes"].append(_format_change(event))
        if PRIORITY_ORDER.get(event["priority"], 9) < PRIORITY_ORDER.get(grouped[key]["priority"], 9):
            grouped[key]["priority"] = event["priority"]
    rows = list(grouped.values())
    rows.sort(key=lambda row: (PRIORITY_ORDER.get(row["priority"], 9), row["competitor"], row["url"]))
    return rows


def _section_table_html(
    title: str,
    events: list[dict[str, Any]],
    show_date: bool = False,
    section_key: str = "",
) -> str:
    rows = _rows_for_section(events)
    is_new_content = section_key in {"new_blog", "new_pages"}
    change_header = "页面标题 / 链接" if is_new_content else "页面"
    detail_header = "说明" if is_new_content else "具体改了什么"

    body_rows = []
    for row in rows:
        changes_html = "<br>".join(html_escape(change) for change in row["changes"])
        date_cell = ""
        if show_date and row.get("report_date"):
            date_cell = f"<td>{html_escape(row['report_date'])}</td>"
        priority = html_escape(row["priority"])
        if is_new_content:
            page_cell = (
                f"<strong>{html_escape(row['page_label'])}</strong><br>"
                f"<a href='{html_escape(row['url'])}'>{html_escape(row['url'])}</a>"
            )
            detail_cell = changes_html
        else:
            page_cell = (
                f"{html_escape(row['page_label'])}<br>"
                f"<a href='{html_escape(row['url'])}'>{html_escape(_short_url(row['url']))}</a>"
            )
            detail_cell = changes_html

        body_rows.append(
            "<tr>"
            f"{date_cell}"
            f"<td><span class='badge {priority}'>{priority}</span> {html_escape(row['competitor'])}</td>"
            f"<td>{page_cell}</td>"
            f"<td class='changes'>{detail_cell}</td>"
            "</tr>"
        )

    date_header = "<th>日期</th>" if show_date else ""
    return (
        f"<section><h2>{html_escape(title)}</h2>"
        "<table><thead><tr>"
        f"{date_header}<th>竞品</th><th>{html_escape(change_header)}</th><th>{html_escape(detail_header)}</th>"
        "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></section>"
    )


def _format_change(event: dict[str, Any]) -> str:
    event_type = event.get("type", "")
    label = CHANGE_TYPE_LABELS.get(event_type, event.get("summary", "变化"))

    if event_type == "new_page":
        title = event.get("page_title") or _detail_value(event, "标题") or _page_label(event)
        h1 = _detail_value(event, "H1")
        if h1 and h1 != title:
            return f"新页面：{truncate(title, 100)}（H1：{truncate(h1, 80)}）"
        return f"新页面：{truncate(title, 120)}"

    if event.get("before") and event.get("after"):
        return (
            f"{label}：「{truncate(event['before'], 80)}」"
            f" → 「{truncate(event['after'], 80)}」"
        )

    if event_type == "content_changed" and _is_content_event(event):
        old_excerpt = _detail_value(event, "旧正文摘要")
        new_excerpt = _detail_value(event, "新正文摘要")
        if old_excerpt and new_excerpt:
            return f"内容更新：「{truncate(old_excerpt, 70)}」→「{truncate(new_excerpt, 70)}」"
        return "内容页正文有更新（建议打开链接查看）"

    if event_type == "visual_changed":
        viewport = next(
            (detail.removeprefix("视口：") for detail in event.get("details", []) if detail.startswith("视口：")),
            "desktop",
        )
        page = _page_label(event)
        return f"{page} {viewport} 端页面布局有明显变化（建议打开链接目视确认）"

    details = [detail for detail in event.get("details", []) if detail]
    if details:
        cleaned = []
        for detail in details[:2]:
            detail = detail.removeprefix("新增：").removeprefix("删除：")
            cleaned.append(truncate(detail, 120))
        return f"{label}：{'；'.join(cleaned)}"

    return truncate(event.get("summary", label), 140)


def _detail_value(event: dict[str, Any], prefix: str) -> str:
    for detail in event.get("details", []):
        if detail.startswith(f"{prefix}："):
            return detail.split("：", 1)[1].strip()
    return ""


def _page_label(event: dict[str, Any]) -> str:
    title = truncate(event.get("page_title", ""), 60)
    if title:
        return title
    path = urlparse(event.get("url", "")).path or "/"
    if path == "/":
        return "首页"
    return path.strip("/").split("/")[-1] or path


def _short_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    host = parsed.netloc.removeprefix("www.")
    if len(path) > 48:
        path = path[:45] + "..."
    return f"{host}{path}"


def _hidden_text(email_events: dict[str, Any]) -> str:
    parts = [f"{label} {count} 项" for label, count in email_events["hidden_counts"].items()]
    return f"已折叠不展示：{', '.join(parts)}。完整列表见 reports/ 目录。"


def _hidden_html(email_events: dict[str, Any]) -> str:
    parts = [f"{html_escape(label)} {count} 项" for label, count in email_events["hidden_counts"].items()]
    return (
        "<section class='folded'><p class='muted'>"
        f"另有 <strong>{len(email_events['hidden_items'])}</strong> 项低优先级变化未在邮件中展开："
        f"{', '.join(parts)}。"
        "完整明细见仓库 reports/ 目录。"
        "</p></section>"
    )


def _errors_html(errors: list[str]) -> str:
    items = "".join(f"<li>{html_escape(truncate(error, 240))}</li>" for error in errors[:10])
    return f"<section><h2>抓取提醒</h2><ul>{items}</ul></section>"


def _event_sort_key(event: dict[str, Any]) -> tuple[int, str, str]:
    return (
        PRIORITY_ORDER.get(event.get("priority", "low"), 9),
        event.get("competitor", ""),
        event.get("url", ""),
    )


def _html_shell_start(title: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(title)}</title>
  <style>
    body {{ margin: 0; padding: 24px; font-family: Arial, 'Microsoft YaHei', sans-serif; color: #1a1a1a; background: #f4f5f7; line-height: 1.5; }}
    h1 {{ font-size: 22px; margin: 0 0 6px; }}
    h2 {{ font-size: 16px; margin: 24px 0 10px; color: #333; }}
    section {{ margin: 16px 0; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e2e6ea; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf0f3; text-align: left; font-size: 13px; vertical-align: top; }}
    th {{ background: #f8f9fb; color: #555; font-weight: 600; }}
    tr:last-child td {{ border-bottom: none; }}
    a {{ color: #1565c0; text-decoration: none; }}
    .muted {{ color: #666; font-size: 13px; }}
    .summary {{ background: #fff; border: 1px solid #e2e6ea; padding: 14px 16px; border-radius: 4px; }}
    .summary-table td:first-child {{ width: 110px; color: #666; }}
    .summary-table td {{ border: none; padding: 4px 8px 4px 0; }}
    .changes {{ color: #222; }}
    .badge {{ display: inline-block; border-radius: 3px; padding: 2px 6px; font-size: 11px; font-weight: 600; text-transform: uppercase; }}
    .badge.high {{ background: #fde8e8; color: #b42318; }}
    .badge.medium {{ background: #fff4e5; color: #b54708; }}
    .badge.low {{ background: #eef4ff; color: #175cd3; }}
    .folded {{ background: #fafafa; border-left: 3px solid #d0d5dd; padding: 10px 14px; }}
    .footer {{ margin-top: 20px; font-size: 12px; color: #888; }}
    code {{ background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }}
    ul {{ margin: 8px 0 0 18px; padding: 0; }}
  </style>
</head>
<body>"""


def _html_shell_end() -> str:
    return "</body></html>"

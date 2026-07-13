from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .utils import html_escape, truncate


PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def build_daily_report(
    config: dict[str, Any],
    run_data: dict[str, Any],
) -> dict[str, Any]:
    timezone = ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    now_local = datetime.now(timezone)
    date_label = now_local.strftime("%Y-%m-%d")
    events = sorted(run_data["events"], key=_event_sort_key)
    errors = run_data.get("errors", [])
    prefix = config.get("email", {}).get("subject_prefix", "[Competitor Tracking]")
    subject = f"{prefix} 日报 {date_label} - {len(events)} 个变化"

    text = _daily_text(date_label, events, errors, run_data)
    html = _daily_html(date_label, events, errors, run_data)

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
    prefix = config.get("email", {}).get("subject_prefix", "[Competitor Tracking]")
    subject = (
        f"{prefix} 周报 {start_date.isoformat()} 至 {now_local.date().isoformat()}"
        f" - {len(events)} 个变化"
    )
    text = _weekly_text(start_date.isoformat(), now_local.date().isoformat(), events, errors)
    html = _weekly_html(start_date.isoformat(), now_local.date().isoformat(), events, errors)

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


def _daily_text(
    date_label: str,
    events: list[dict[str, Any]],
    errors: list[str],
    run_data: dict[str, Any],
) -> str:
    lines = [f"竞品追踪日报 - {date_label}", ""]
    lines.append(_overview_text(events))
    lines.append("")
    for competitor, competitor_events in _group_by_competitor(events).items():
        lines.append(f"【{competitor}】")
        for event in competitor_events[:12]:
            lines.append(f"- [{event['priority']}] {event['summary']}")
            lines.append(f"  {event['url']}")
            for detail in event.get("details", [])[:3]:
                lines.append(f"  {detail}")
        lines.append("")
    if not events:
        lines.append("今天没有发现重点页面变化。")
        lines.append("")
    if errors:
        lines.append("抓取提醒：")
        for error in errors[:20]:
            lines.append(f"- {error}")
    lines.append("")
    lines.append("完整明细已保存到仓库 reports/ 目录。")
    return "\n".join(lines)


def _daily_html(
    date_label: str,
    events: list[dict[str, Any]],
    errors: list[str],
    run_data: dict[str, Any],
) -> str:
    parts = [_html_shell_start(f"竞品追踪日报 - {date_label}")]
    parts.append(f"<h1>竞品追踪日报</h1><p class='muted'>{html_escape(date_label)}</p>")
    parts.append(_overview_html(events))
    parts.append(_discovery_html(run_data.get("discovery", [])))
    parts.append(_events_html(events, limit_per_competitor=16))
    if errors:
        parts.append(_errors_html(errors))
    parts.append(_html_shell_end())
    return "\n".join(parts)


def _weekly_text(
    start_date: str,
    end_date: str,
    events: list[dict[str, Any]],
    errors: list[str],
) -> str:
    lines = [f"竞品追踪周报 - {start_date} 至 {end_date}", "", _overview_text(events), ""]
    lines.append("本周重点：")
    for event in events[:25]:
        date_text = f"{event.get('report_date')} " if event.get("report_date") else ""
        lines.append(f"- {date_text}[{event['priority']}] {event['competitor']}：{event['summary']}")
        lines.append(f"  {event['url']}")
    if not events:
        lines.append("- 本周没有发现重点变化。")
    if errors:
        lines.append("")
        lines.append("抓取提醒：")
        for error in errors[:20]:
            lines.append(f"- {error}")
    return "\n".join(lines)


def _weekly_html(
    start_date: str,
    end_date: str,
    events: list[dict[str, Any]],
    errors: list[str],
) -> str:
    parts = [_html_shell_start(f"竞品追踪周报 - {start_date} 至 {end_date}")]
    parts.append("<h1>竞品追踪周报</h1>")
    parts.append(f"<p class='muted'>{html_escape(start_date)} 至 {html_escape(end_date)}</p>")
    parts.append(_overview_html(events))
    parts.append(_events_html(events, limit_per_competitor=24, show_date=True))
    if errors:
        parts.append(_errors_html(errors))
    parts.append(_html_shell_end())
    return "\n".join(parts)


def _overview_text(events: list[dict[str, Any]]) -> str:
    if not events:
        return "总览：0 个变化。"
    priorities = Counter(event["priority"] for event in events)
    categories = Counter(event["category"] for event in events)
    competitors = Counter(event["competitor"] for event in events)
    return (
        f"总览：{len(events)} 个变化；高优先级 {priorities['high']}，"
        f"中优先级 {priorities['medium']}，低优先级 {priorities['low']}。"
        f" 变化最多：{', '.join(name for name, _ in competitors.most_common(3))}。"
        f" 类型集中在：{', '.join(name for name, _ in categories.most_common(3))}。"
    )


def _overview_html(events: list[dict[str, Any]]) -> str:
    priorities = Counter(event["priority"] for event in events)
    categories = Counter(event["category"] for event in events)
    cards = [
        ("总变化", str(len(events))),
        ("高优先级", str(priorities["high"])),
        ("中优先级", str(priorities["medium"])),
        ("低优先级", str(priorities["low"])),
    ]
    card_html = "".join(
        f"<div class='metric'><strong>{html_escape(value)}</strong><span>{html_escape(label)}</span></div>"
        for label, value in cards
    )
    cat_html = ", ".join(
        f"{html_escape(name)} {count}" for name, count in categories.most_common(5)
    )
    return f"<section><div class='metrics'>{card_html}</div><p class='muted'>分类：{cat_html or '无'}</p></section>"


def _discovery_html(discovery: list[dict[str, Any]]) -> str:
    if not discovery:
        return ""
    rows = []
    for item in discovery:
        rows.append(
            "<tr>"
            f"<td>{html_escape(item.get('competitor', ''))}</td>"
            f"<td>{item.get('tracked_urls', 0)}</td>"
            f"<td>{item.get('source_counts', {}).get('sitemap', 0)}</td>"
            f"<td>{item.get('source_counts', {}).get('links', 0)}</td>"
            "</tr>"
        )
    return (
        "<section><h2>页面发现</h2><table><thead><tr>"
        "<th>竞品</th><th>本次追踪 URL</th><th>Sitemap 候选</th><th>页面链接候选</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></section>"
    )


def _events_html(
    events: list[dict[str, Any]],
    limit_per_competitor: int,
    show_date: bool = False,
) -> str:
    if not events:
        return "<section><h2>变化明细</h2><p>没有发现重点页面变化。</p></section>"

    sections = ["<section><h2>变化明细</h2>"]
    for competitor, competitor_events in _group_by_competitor(events).items():
        sections.append(f"<h3>{html_escape(competitor)}</h3>")
        for event in competitor_events[:limit_per_competitor]:
            priority = html_escape(event["priority"])
            date_text = ""
            if show_date and event.get("report_date"):
                date_text = f"<span class='tag'>{html_escape(event['report_date'])}</span>"
            details = "".join(
                f"<li>{html_escape(truncate(detail, 260))}</li>"
                for detail in event.get("details", [])[:4]
            )
            sections.append(
                "<article class='event'>"
                f"<div>{date_text}<span class='badge {priority}'>{priority}</span>"
                f"<span class='tag'>{html_escape(event.get('category', 'page'))}</span></div>"
                f"<h4>{html_escape(event['summary'])}</h4>"
                f"<p><a href='{html_escape(event['url'])}'>{html_escape(event['url'])}</a></p>"
                f"<ul>{details}</ul>"
                "</article>"
            )
    sections.append("</section>")
    return "\n".join(sections)


def _errors_html(errors: list[str]) -> str:
    items = "".join(f"<li>{html_escape(truncate(error, 240))}</li>" for error in errors[:30])
    return f"<section><h2>抓取提醒</h2><ul>{items}</ul></section>"


def _group_by_competitor(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[event["competitor"]].append(event)
    return dict(sorted(grouped.items(), key=lambda item: item[0].lower()))


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
    body {{ margin: 0; padding: 28px; font-family: Arial, 'Microsoft YaHei', sans-serif; color: #17202a; background: #f6f8fb; }}
    h1, h2, h3, h4 {{ margin: 0 0 10px; }}
    h1 {{ font-size: 24px; }}
    h2 {{ font-size: 18px; margin-top: 28px; border-bottom: 1px solid #d9e1ec; padding-bottom: 8px; }}
    h3 {{ font-size: 16px; margin-top: 20px; }}
    h4 {{ font-size: 15px; margin-top: 8px; }}
    section {{ margin: 18px 0; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #e7edf4; text-align: left; font-size: 13px; }}
    a {{ color: #0b61a4; }}
    .muted {{ color: #667085; font-size: 13px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }}
    .metric {{ background: #fff; border: 1px solid #dfe7f1; border-radius: 6px; padding: 12px; }}
    .metric strong {{ display: block; font-size: 22px; }}
    .metric span {{ color: #667085; font-size: 12px; }}
    .event {{ background: #fff; border: 1px solid #dfe7f1; border-radius: 6px; margin: 10px 0; padding: 12px; }}
    .badge, .tag {{ display: inline-block; border-radius: 999px; padding: 3px 8px; font-size: 12px; margin-right: 6px; background: #eef2f6; color: #344054; }}
    .badge.high {{ background: #fee4e2; color: #b42318; }}
    .badge.medium {{ background: #fef0c7; color: #93370d; }}
    .badge.low {{ background: #e0f2fe; color: #075985; }}
    ul {{ margin-top: 8px; }}
  </style>
</head>
<body>"""


def _html_shell_end() -> str:
    return "</body></html>"


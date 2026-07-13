from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .diffing import diff_snapshots, visual_event
from .discovery import discover_competitor
from .emailer import send_report_email
from .extract import VIEWPORTS, PageCollector, collect_static
from .reports import build_daily_report, build_weekly_report, save_report
from .storage import TrackerStorage
from .utils import utc_now_iso, url_hash
from .visual import compare_images, is_significant_visual_change


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated competitor tracking")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a tracking job")
    run_parser.add_argument("--mode", choices=["daily", "weekly"], required=True)
    run_parser.add_argument("--config", default="config/competitors.yml")

    args = parser.parse_args()
    config = load_config(args.config)

    if args.mode == "daily":
        report = run_daily(config)
    else:
        report = run_weekly(config)

    status = send_report_email(
        config=config,
        subject=report["subject"],
        html=report["email"]["html"],
        text=report["email"]["text"],
    )
    report["email_status"] = status
    json_path, html_path = save_report(report, Path("reports"))
    print(status)
    print(f"Report JSON: {json_path}")
    print(f"Report HTML: {html_path}")


def run_daily(config: dict) -> dict:
    storage = TrackerStorage()
    index = storage.load_index()
    index.setdefault("known_urls", {})
    index.setdefault("runs", [])

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tmp_dir = storage.root / "tmp" / run_id
    tmp_dir.mkdir(parents=True, exist_ok=True)

    timeout_seconds = int(config["run"]["request_timeout_seconds"])
    screenshot_limit = int(config["run"]["screenshot_priority_pages_per_domain"])
    visual_thresholds = config["run"]["visual_diff_threshold"]

    events = []
    errors = []
    discovery_summary = []

    try:
        with PageCollector(timeout_seconds=timeout_seconds) as collector:
            for competitor in config["competitors"]:
                discovery = discover_competitor(competitor, config)
                errors.extend(discovery.errors)
                discovery_summary.append(
                    {
                        "competitor": discovery.competitor,
                        "base_url": discovery.base_url,
                        "tracked_urls": len(discovery.urls),
                        "source_counts": discovery.source_counts,
                    }
                )

                seen_urls = []
                seen_this_run = set()
                for index_in_domain, url in enumerate(discovery.urls):
                    if url in seen_this_run:
                        continue
                    seen_this_run.add(url)

                    try:
                        screenshot_paths = None
                        if index_in_domain < screenshot_limit:
                            screenshot_paths = {
                                viewport: tmp_dir / f"{url_hash(url)}_{viewport}.png"
                                for viewport in VIEWPORTS
                            }
                            snapshot = collector.collect(
                                url=url,
                                competitor=discovery.competitor,
                                screenshot_paths=screenshot_paths,
                            )
                        else:
                            snapshot = collect_static(
                                url=url,
                                competitor=discovery.competitor,
                                timeout_seconds=timeout_seconds,
                            )

                        snapshot["requested_url"] = url
                        snapshot["fetched_at"] = utc_now_iso()
                        previous = storage.load_page(snapshot["url"]) or storage.load_page(url)
                        page_events = diff_snapshots(previous, snapshot)

                        if screenshot_paths:
                            for viewport, new_path in screenshot_paths.items():
                                old_path = storage.screenshot_path(snapshot["url"], viewport)
                                metrics = compare_images(old_path, new_path)
                                if is_significant_visual_change(metrics, visual_thresholds):
                                    page_events.append(visual_event(snapshot, viewport, metrics))
                                old_path.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copyfile(new_path, old_path)

                        storage.save_page(snapshot["url"], snapshot)
                        seen_urls.append(snapshot["url"])
                        events.extend(page_events)
                    except Exception as exc:
                        errors.append(f"{discovery.competitor} | {url} | {exc}")

                index["known_urls"][discovery.competitor] = seen_urls
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    index["initialized"] = True
    index["last_run_at"] = utc_now_iso()
    index["runs"].append(
        {
            "mode": "daily",
            "run_id": run_id,
            "finished_at": utc_now_iso(),
            "events": len(events),
            "errors": len(errors),
        }
    )
    index["runs"] = index["runs"][-60:]
    storage.save_index(index)

    return build_daily_report(
        config,
        {
            "events": events,
            "errors": errors,
            "discovery": discovery_summary,
        },
    )


def run_weekly(config: dict) -> dict:
    return build_weekly_report(config, Path("reports"))


if __name__ == "__main__":
    main()


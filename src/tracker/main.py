from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .diffing import diff_snapshots, health_event, visual_event
from .discovery import discover_competitor
from .emailer import send_report_email
from .extract import VIEWPORTS, PageCollector, collect_static
from .reports import build_daily_report, build_weekly_report, save_report
from .storage import TrackerStorage
from .utils import normalize_url, same_domain, utc_now_iso, url_hash
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

    report.setdefault("run_id", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))

    # Persist the report before sending so a mail-provider failure never loses
    # the evidence or causes the next run to rediscover the same state blindly.
    json_path, html_path = save_report(report, Path("reports"))
    status = send_report_email(
        config=config,
        subject=report["subject"],
        html=report["email"]["html"],
        text=report["email"]["text"],
    )
    report["email_status"] = status
    save_report(report, Path("reports"))
    print(status)
    print(f"Report JSON: {json_path}")
    print(f"Report HTML: {html_path}")


def run_daily(config: dict) -> dict:
    storage = TrackerStorage()
    index = storage.load_index()
    index.setdefault("known_urls", {})
    index.setdefault("runs", [])
    baseline_run = not bool(index.get("initialized"))
    upgrade_run = int(index.get("schema_version", 1)) < 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tmp_dir = storage.root / "tmp" / run_id
    tmp_dir.mkdir(parents=True, exist_ok=True)

    timeout_seconds = int(config["run"]["request_timeout_seconds"])
    screenshot_limit = int(config["run"]["screenshot_priority_pages_per_domain"])
    visual_thresholds = config["run"]["visual_diff_threshold"]
    max_content_age = int(config["run"]["new_content_max_age_days"])
    minimum_text_change = float(config["run"]["minimum_text_change_percent"])
    coverage_drop_warning = float(config["run"]["coverage_drop_warning_percent"])
    maximum_error_rate = float(config["run"]["maximum_error_rate_percent"])

    events = []
    errors = []
    discovery_summary = []

    try:
        with PageCollector(timeout_seconds=timeout_seconds) as collector:
            for competitor in config["competitors"]:
                page_failures = 0
                previous_urls = index["known_urls"].get(competitor["name"], [])
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
                seen_identities = set()
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
                                extraction_rules=competitor,
                            )
                        else:
                            snapshot = collect_static(
                                url=url,
                                competitor=discovery.competitor,
                                timeout_seconds=timeout_seconds,
                                extraction_rules=competitor,
                            )

                        if snapshot.get("status") is None or not 200 <= int(snapshot["status"]) < 400:
                            raise RuntimeError(f"HTTP {snapshot.get('status') or 'unknown'}")

                        final_url = normalize_url(snapshot["url"])
                        canonical = snapshot.get("canonical", "")
                        identity_url = (
                            normalize_url(canonical)
                            if canonical and same_domain(canonical, discovery.base_url)
                            else final_url
                        )
                        if identity_url in seen_identities:
                            continue
                        seen_identities.add(identity_url)
                        snapshot["url"] = identity_url
                        snapshot["final_url"] = final_url
                        snapshot["requested_url"] = url
                        snapshot["fetched_at"] = utc_now_iso()
                        discovery_meta = discovery.url_metadata.get(url, {})
                        snapshot["discovered_via"] = discovery_meta.get("source", "")
                        snapshot["sitemap_lastmod"] = discovery_meta.get("lastmod", "")
                        snapshot["published_at"] = (
                            snapshot.get("published_at")
                            or discovery_meta.get("published_at", "")
                        )
                        previous = (
                            storage.load_page(identity_url)
                            or storage.load_page(final_url)
                            or storage.load_page(url)
                        )
                        if previous and previous.get("discovered_via"):
                            snapshot["discovered_via"] = previous["discovered_via"]
                        snapshot["first_seen_at"] = (
                            previous.get("first_seen_at") if previous else snapshot["fetched_at"]
                        ) or snapshot["fetched_at"]
                        page_events = diff_snapshots(
                            previous,
                            snapshot,
                            new_content_max_age_days=max_content_age,
                            minimum_text_change_percent=minimum_text_change,
                            emit_new_page=not baseline_run,
                        )

                        if screenshot_paths:
                            for viewport, new_path in screenshot_paths.items():
                                old_path = storage.screenshot_path(identity_url, viewport)
                                metrics = compare_images(old_path, new_path)
                                significant_visual_change = is_significant_visual_change(
                                    metrics, visual_thresholds
                                )
                                if (
                                    not baseline_run
                                    and not upgrade_run
                                    and significant_visual_change
                                ):
                                    page_events.append(visual_event(snapshot, viewport, metrics))
                                # Keep the old visual baseline when the difference is
                                # below threshold. This avoids committing noisy PNGs
                                # every day and lets small changes accumulate.
                                if metrics is None or significant_visual_change:
                                    old_path.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.copyfile(new_path, old_path)

                        storage.save_page(identity_url, snapshot)
                        seen_urls.append(identity_url)
                        events.extend(page_events)
                    except Exception as exc:
                        page_failures += 1
                        errors.append(f"{discovery.competitor} | {url} | {exc}")

                index["known_urls"][discovery.competitor] = seen_urls
                previous_count = len(previous_urls)
                current_count = len(seen_urls)
                if (
                    not upgrade_run
                    and previous_count >= 10
                    and current_count < previous_count * (1 - coverage_drop_warning / 100)
                ):
                    events.append(
                        health_event(
                            discovery.competitor,
                            discovery.base_url,
                            "监控覆盖率明显下降",
                            [
                                f"上次成功页面：{previous_count}",
                                f"本次成功页面：{current_count}",
                                f"下降：{(previous_count-current_count)*100/previous_count:.1f}%",
                            ],
                        )
                    )
                attempted = max(len(discovery.urls), 1)
                if not upgrade_run and page_failures * 100 / attempted >= maximum_error_rate:
                    events.append(
                        health_event(
                            discovery.competitor,
                            discovery.base_url,
                            "抓取失败率过高",
                            [
                                f"尝试页面：{len(discovery.urls)}",
                                f"失败页面：{page_failures}",
                                f"失败率：{page_failures*100/attempted:.1f}%",
                            ],
                        )
                    )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    index["initialized"] = True
    index["schema_version"] = 2
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

    report = build_daily_report(
        config,
        {
            "events": events,
            "errors": errors,
            "discovery": discovery_summary,
        },
    )
    report["run_id"] = run_id
    report["baseline_run"] = baseline_run
    report["upgrade_run"] = upgrade_run
    return report


def run_weekly(config: dict) -> dict:
    return build_weekly_report(config, Path("reports"))


if __name__ == "__main__":
    main()

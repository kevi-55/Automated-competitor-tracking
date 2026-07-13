from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    config.setdefault("timezone", "Asia/Shanghai")
    config.setdefault("email", {})
    config.setdefault("run", {})
    config.setdefault("discovery", {})
    config.setdefault("competitors", [])

    run = config["run"]
    run.setdefault("max_pages_per_domain", 100)
    run.setdefault("request_timeout_seconds", 25)
    run.setdefault("screenshot_priority_pages_per_domain", 5)
    run.setdefault("visual_diff_threshold", {"rms": 7, "changed_pixels_percent": 2.5})

    discovery = config["discovery"]
    discovery.setdefault("include_path_keywords", ["/"])
    discovery.setdefault("exclude_path_keywords", [])

    for competitor in config["competitors"]:
        competitor["base_url"] = normalize_url_text(competitor["base_url"])
        competitor.setdefault("priority_paths", ["/"])
        competitor["priority_paths"] = [
            normalize_path_text(path_value) for path_value in competitor["priority_paths"]
        ]

    return config


def normalize_url_text(value: str) -> str:
    return value.replace("\u00a0", "").strip()


def normalize_path_text(value: str) -> str:
    value = normalize_url_text(value)
    if not value:
        return "/"
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return value if value.startswith("/") else f"/{value}"


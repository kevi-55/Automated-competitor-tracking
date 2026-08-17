from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .utils import url_hash


class TrackerStorage:
    def __init__(self, root: str | Path = ".tracker_state") -> None:
        self.root = Path(root)
        self.pages_dir = self.root / "pages"
        self.screenshots_dir = self.root / "screenshots"
        self.index_path = self.root / "index.json"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    def load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"initialized": False, "runs": []}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def save_index(self, index: dict[str, Any]) -> None:
        self.index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def page_path(self, url: str) -> Path:
        return self.pages_dir / f"{url_hash(url)}.json"

    def load_page(self, url: str) -> dict[str, Any] | None:
        path = self.page_path(url)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save_page(self, url: str, snapshot: dict[str, Any]) -> bool:
        path = self.page_path(url)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if existing and _stable_snapshot(existing) == _stable_snapshot(snapshot):
                return False
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return True

    def screenshot_path(self, url: str, viewport_name: str) -> Path:
        safe_viewport = "".join(ch for ch in viewport_name if ch.isalnum() or ch in ("-", "_"))
        return self.screenshots_dir / f"{url_hash(url)}_{safe_viewport}.png"


def _stable_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Exclude per-run bookkeeping when deciding whether a page file changed."""
    return {
        key: value
        for key, value in snapshot.items()
        if key not in {
            "fetched_at",
            "requested_url",
            "modified_at",
            "sitemap_lastmod",
            "discovered_via",
        }
    }

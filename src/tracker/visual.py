from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageStat


def compare_images(old_path: Path, new_path: Path) -> dict[str, float] | None:
    if not old_path.exists() or not new_path.exists():
        return None

    with Image.open(old_path) as old_img, Image.open(new_path) as new_img:
        old_rgb = old_img.convert("RGB")
        new_rgb = new_img.convert("RGB")
        width = min(old_rgb.width, new_rgb.width)
        height = min(old_rgb.height, new_rgb.height)
        if width <= 0 or height <= 0:
            return None

        old_crop = old_rgb.crop((0, 0, width, height))
        new_crop = new_rgb.crop((0, 0, width, height))
        diff = ImageChops.difference(old_crop, new_crop)
        stat = ImageStat.Stat(diff)
        rms = sum(value**2 for value in stat.rms) ** 0.5

        diff_gray = diff.convert("L")
        histogram = diff_gray.histogram()
        changed_pixels = sum(count for value, count in enumerate(histogram) if value > 30)
        total_pixels = width * height
        changed_percent = changed_pixels * 100 / total_pixels

        return {
            "rms": float(rms),
            "changed_pixels_percent": float(changed_percent),
            "height_delta": float(new_rgb.height - old_rgb.height),
            "old_width": float(old_rgb.width),
            "old_height": float(old_rgb.height),
            "new_width": float(new_rgb.width),
            "new_height": float(new_rgb.height),
        }


def is_significant_visual_change(metrics: dict[str, float] | None, thresholds: dict) -> bool:
    if not metrics:
        return False
    rms_threshold = float(thresholds.get("rms", 7))
    changed_threshold = float(thresholds.get("changed_pixels_percent", 2.5))
    height_delta = abs(metrics.get("height_delta", 0))
    return (
        metrics["rms"] >= rms_threshold
        and metrics["changed_pixels_percent"] >= changed_threshold
    ) or height_delta >= 500


"""週次スナップショットの永続化とロード。

history/YYYY-MM-DD.json 形式で保存。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HISTORY_DIR = Path(__file__).parent.parent / "history"


def save_snapshot(snapshot_date: date, data: dict) -> Path:
    """スナップショットを YYYY-MM-DD.json で保存。既存は上書き。"""
    HISTORY_DIR.mkdir(exist_ok=True)
    fp = HISTORY_DIR / f"{snapshot_date.isoformat()}.json"
    with fp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved snapshot: {fp}")
    return fp


def load_snapshot(snapshot_date: date) -> dict | None:
    fp = HISTORY_DIR / f"{snapshot_date.isoformat()}.json"
    if not fp.exists():
        return None
    with fp.open(encoding="utf-8") as f:
        return json.load(f)


def list_snapshots() -> list[date]:
    """保存済みスナップショット日付をソート済で返す。"""
    if not HISTORY_DIR.exists():
        return []
    dates = []
    for fp in HISTORY_DIR.glob("*.json"):
        try:
            dates.append(date.fromisoformat(fp.stem))
        except ValueError:
            continue
    return sorted(dates)


def load_latest_snapshot() -> tuple[date, dict] | None:
    dates = list_snapshots()
    if not dates:
        return None
    d = dates[-1]
    data = load_snapshot(d)
    return (d, data) if data else None


def load_baseline_snapshot(baseline_date: date | None = None) -> dict | None:
    """NOINDEX処理前のベースライン。デフォルトは 2026-05-01。"""
    baseline_date = baseline_date or date(2026, 5, 1)
    return load_snapshot(baseline_date)

"""インデックス状態の永続化。

3つのファイルに分けている：
- state.csv        … URL ごとの「最新の状態」1行。URLソート済みなので git 差分が行単位で残る。
- changes/*.json   … その日に状態が変わったURLだけ。日次レポートの本体。
- daily.csv        … 日次のステータス別件数。推移グラフ用。

日次の全件スナップショットは保存しない。14,146行 × 毎日をリポジトリに積むと
履歴が肥大するだけで、知りたいのは「変わったURL」だから。
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import date
from pathlib import Path

from src.index_status.status_map import STATUS_ORDER

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "index_status"
STATE_PATH = DATA_DIR / "state.csv"
DAILY_PATH = DATA_DIR / "daily.csv"
CHANGES_DIR = DATA_DIR / "changes"

STATE_FIELDS = [
    "url",
    "group",
    "sitemap",
    "status",            # status_map のキー
    "coverage_raw",      # APIが返した生文字列（文言変更の検知用に必ず残す）
    "verdict",
    "indexing_state",
    "robots_txt_state",
    "page_fetch_state",
    "google_canonical",
    "user_canonical",
    "last_crawl_time",
    "checked_at",        # 最後に「取得成功」した時刻
    "last_attempt_at",   # 最後に「試行」した時刻。ローテーションの順序はこちらで決める
    "prev_status",
    "changed_at",        # status が最後に変化した日
    "error_count",
]


def load_state(path=None) -> dict:
    """url -> 行dict。"""
    path = path or STATE_PATH
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {r["url"]: r for r in csv.DictReader(f)}


def save_state(state: dict, path=None) -> Path:
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=STATE_FIELDS, extrasaction="ignore")
        w.writeheader()
        for url in sorted(state):
            row = {k: (state[url].get(k) or "") for k in STATE_FIELDS}
            row["url"] = url
            w.writerow(row)
    logger.info(f"保存: {path} ({len(state)} URL)")
    return path


def sync_urls(state: dict, urls: list) -> tuple:
    """sitemap の最新URLリストに state を合わせる。

    追加分は status="unchecked" で入れ、sitemap から消えたURLは state からも消す。
    戻り値は (追加数, 削除数)。
    """
    current = {u: (g, s) for u, g, s in urls}
    added = 0
    for url, (group, sitemap) in current.items():
        if url in state:
            state[url]["group"] = group
            state[url]["sitemap"] = sitemap
        else:
            state[url] = {
                "url": url, "group": group, "sitemap": sitemap,
                "status": "unchecked", "error_count": "0",
            }
            added += 1
    removed = [u for u in state if u not in current]
    for u in removed:
        del state[u]
    if added or removed:
        logger.info(f"URLリスト同期: 追加 {added} / 削除 {len(removed)}")
    return added, len(removed)


def save_changes(d: date, changes: list, summary: dict) -> Path:
    CHANGES_DIR.mkdir(parents=True, exist_ok=True)
    fp = CHANGES_DIR / f"{d.isoformat()}.json"
    with fp.open("w", encoding="utf-8") as f:
        json.dump({"date": d.isoformat(), "summary": summary, "changes": changes},
                  f, ensure_ascii=False, indent=1)
    logger.info(f"保存: {fp} (遷移 {len(changes)} 件)")
    return fp


def load_changes(d: date) -> dict:
    fp = CHANGES_DIR / f"{d.isoformat()}.json"
    if not fp.exists():
        return {}
    with fp.open(encoding="utf-8") as f:
        return json.load(f)


def list_change_dates() -> list:
    if not CHANGES_DIR.exists():
        return []
    out = []
    for fp in CHANGES_DIR.glob("*.json"):
        try:
            out.append(date.fromisoformat(fp.stem))
        except ValueError:
            continue
    return sorted(out)


def append_daily(d: date, counts: dict, checked: int, changed: int, path=None) -> Path:
    """日次の件数を1行追記。同じ日を再実行したら上書きする。"""
    path = path or DAILY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["date", "checked", "changed"] + STATUS_ORDER
    rows = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            rows = {r["date"]: r for r in csv.DictReader(f)}
    row = {"date": d.isoformat(), "checked": checked, "changed": changed}
    for k in STATUS_ORDER:
        row[k] = counts.get(k, 0)
    rows[d.isoformat()] = row
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])
    return path


def load_daily(path=None) -> list:
    path = path or DAILY_PATH
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))

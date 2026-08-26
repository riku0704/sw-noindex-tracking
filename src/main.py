"""オーケストレーター - 週次ダッシュボード更新のエントリポイント。

使い方:
    python -m src.main                  # 通常実行 (取得+HTML生成)
    python -m src.main --dry-run        # データ取得のみ
    python -m src.main --html-only      # 既存データからHTML再生成
    python -m src.main --inspect-only   # URL Inspection のみ
    python -m src.main --backfill       # 過去のスナップショットも一括取得
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from src import fetch_gsc, fetch_crawl_status, generate_html, history_store

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
TARGETS_PATH = REPO_ROOT / "targets.json"


# GSC の確定データ(dataState="final")は数日遅れて揃う。実測3日なので余裕を見て4日。
# ここを 1 にすると直近週の末尾がまだ空のまま集計され、前週比が常にマイナスに振れる。
GSC_LAG_DAYS = 4


def _latest_final_date(today: date | None = None) -> date:
    """確定データが揃っている最新日。"""
    return (today or date.today()) - timedelta(days=GSC_LAG_DAYS)


def _same_week_last_year(d: date) -> date:
    """前年同週の終了日 = 364日前 (52週ちょうど)。

    replace(year=-1) だと曜日が変わってしまい、検索トラフィックの
    曜日変動が前年比較に混入する。364日前なら曜日が保たれる。
    """
    return d - timedelta(days=364)


def load_targets() -> dict:
    with TARGETS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def take_snapshot(site_url: str, targets: dict, snapshot_date: date, include_crawl: bool = True) -> dict:
    """1週間分のスナップショットを取得 (GSCデータ + クロール状態)。

    snapshot_date は「スナップショット対象週の終わり日」として扱い、
    その日から遡って7日間を取得。
    """
    date_to = snapshot_date
    date_from = date_to - timedelta(days=6)
    logger.info(f"=== Snapshot: {date_from} 〜 {date_to} ===")

    gsc = fetch_gsc.fetch_week_snapshot(site_url, date_from, date_to, targets)

    if include_crawl:
        urls = (
            [t["url"] for t in targets["countries"]]
            + [t["url"] for t in targets["cities"]]
            + [t["url"] for t in targets["schools"]]
        )
        prev = history_store.load_latest_snapshot()
        prev_crawl = prev[1].get("crawl", []) if prev else None
        crawl_raw = fetch_crawl_status.inspect_all(site_url, urls)
        crawl = fetch_crawl_status.diff_crawl_data(crawl_raw, prev_crawl)
        gsc["crawl"] = crawl
    else:
        gsc["crawl"] = []

    return gsc


def build_and_render(targets: dict, latest_date: date | None = None) -> Path:
    """スナップショットからHTML生成。"""
    all_dates = history_store.list_snapshots()
    if not all_dates:
        raise RuntimeError("No snapshots found. Run without --html-only first.")
    curr_date = latest_date or all_dates[-1]
    curr = history_store.load_snapshot(curr_date)

    # 直近以外で最新の週次スナップショット
    prev = None
    for d in reversed(all_dates):
        if d < curr_date:
            prev = history_store.load_snapshot(d)
            break

    # NOINDEXベースライン: 2026-05-07
    baseline = history_store.load_baseline_snapshot(date(2026, 5, 7))

    # 昨年同期
    from datetime import date as _date
    ly_baseline = history_store.load_snapshot(_date(2025, 5, 7))
    ly_current = history_store.load_snapshot(_same_week_last_year(curr_date)) if curr_date else None

    data = generate_html.build_dashboard_data(
        targets=targets,
        curr_snapshot=curr,
        prev_snapshot=prev,
        baseline_snapshot=baseline,
        ly_baseline=ly_baseline,
        ly_current=ly_current,
        crawl_data=curr.get("crawl", []),
    )
    return generate_html.render(data)


def cmd_default(args):
    """通常実行: 今週スナップショット取得 + HTML更新。"""
    site = os.environ["GSC_SITE_URL"]
    targets = load_targets()
    # 今週の end date (GSC の確定データ遅延ぶん遡る)
    snap_date = _latest_final_date()
    if args.date:
        snap_date = date.fromisoformat(args.date)

    snap = take_snapshot(site, targets, snap_date, include_crawl=not args.dry_run)
    history_store.save_snapshot(snap_date, snap)
    if not args.dry_run:
        build_and_render(targets, snap_date)


def cmd_html_only(args):
    targets = load_targets()
    build_and_render(targets)


def cmd_inspect_only(args):
    """URL Inspection だけ実行して直近スナップショットに差し込む。"""
    site = os.environ["GSC_SITE_URL"]
    targets = load_targets()
    latest = history_store.load_latest_snapshot()
    if not latest:
        raise RuntimeError("No existing snapshot to update.")
    snap_date, snap = latest
    urls = (
        [t["url"] for t in targets["countries"]]
        + [t["url"] for t in targets["cities"]]
        + [t["url"] for t in targets["schools"]]
    )
    crawl_raw = fetch_crawl_status.inspect_all(site, urls)
    snap["crawl"] = fetch_crawl_status.diff_crawl_data(crawl_raw, snap.get("crawl", []))
    history_store.save_snapshot(snap_date, snap)
    build_and_render(targets, snap_date)


def cmd_backfill(args):
    """過去N週分のGSCデータを取得 (URL Inspectionは含まない=過去分は取れないため)。

    NOINDEX前ベースライン(2026-05-01~07) + 昨年同期(2025-05-01~07, 2025-07-16~22) も含む。
    """
    site = os.environ["GSC_SITE_URL"]
    targets = load_targets()
    backfill_dates = [
        date(2025, 5, 7),   # 昨年NOINDEX前ベースライン相当
        date(2025, 7, 22),  # 昨年同期
        date(2026, 5, 7),   # 今年NOINDEX前ベースライン
    ]
    # 直近8週 + それぞれの昨年同週 (前年同期比較に必要)
    latest = _latest_final_date()
    for w in range(8):
        d = latest - timedelta(days=w * 7)
        for cand in (d, _same_week_last_year(d)):
            if cand not in backfill_dates:
                backfill_dates.append(cand)

    for d in backfill_dates:
        if history_store.load_snapshot(d) and not args.force_full:
            logger.info(f"Skip {d} (already exists)")
            continue
        try:
            snap = take_snapshot(site, targets, d, include_crawl=(d == latest))
            history_store.save_snapshot(d, snap)
        except Exception as e:
            logger.error(f"Failed to snapshot {d}: {e}")

    build_and_render(targets)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="SW NOINDEX Tracking Dashboard Updater")
    p.add_argument("--dry-run", action="store_true", help="データ取得のみ (HTMLは生成しない)")
    p.add_argument("--html-only", action="store_true", help="既存データからHTMLだけ再生成")
    p.add_argument("--inspect-only", action="store_true", help="URL Inspectionだけ更新")
    p.add_argument("--backfill", action="store_true", help="過去N週+ベースライン+昨年同期を一括取得")
    p.add_argument("--force-full", action="store_true", help="backfill時、既存も上書き")
    p.add_argument("--date", type=str, help="スナップショットの終了日 (YYYY-MM-DD) 明示指定")
    args = p.parse_args()

    if args.html_only:
        cmd_html_only(args)
    elif args.inspect_only:
        cmd_inspect_only(args)
    elif args.backfill:
        cmd_backfill(args)
    else:
        cmd_default(args)


if __name__ == "__main__":
    main()

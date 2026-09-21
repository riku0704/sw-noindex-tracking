"""日次バッチ本体：ローテーションで一定件数を照会し、状態の遷移を記録する。

なぜ全件を毎日見ないか
----------------------
URL Inspection API のクォータは **2,000 URL / 日 / プロパティ** で、サイトマップは
14,000URL超。全件を毎日は物理的に不可能なので、最終試行が古いURLから順に
既定 1,800 件/日を照会し、約8日で一巡させる。
したがって「ステータス別の件数」は全URLの最新既知値の合算であり、
最大で8日前の値を含む。GSC のページレポートの数字とは一致しない（レポート側にAPIは無い）。
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from src import fetch_crawl_status
from src.index_status import store, urlset
from src.index_status.status_map import PROBLEM_STATUSES, normalize, unknown_states

logger = logging.getLogger(__name__)

# クォータ 2,000 に対する余裕分。リトライや手動のURL検査でも消費されるため使い切らない。
DEFAULT_BUDGET = 1800
# 1件あたり実測7秒。10並列で 1,800件 ≒ 21分。レートは約85req/分で上限600/分に対し十分低い。
DEFAULT_WORKERS = 10
# 連続エラーがこの数に達したら中断する。クォータ切れや認証失効で延々と叩き続けないため。
ABORT_AFTER_CONSECUTIVE_ERRORS = 40

_local = threading.local()


def _service():
    """スレッドごとに1つのAPIクライアント。googleapiclient は http が共有されると壊れる。"""
    if not hasattr(_local, "svc"):
        _local.svc = fetch_crawl_status._build_service()
    return _local.svc


def _tiebreak(url: str) -> str:
    """同着時の並び順。URL文字列そのものを使うとパスのアルファベット順になり、
    初回の一巡中に特定グループ（/areas/ → /categories/ → …）だけが先に埋まって、
    集計値が7日間ずっとグループ単位で偏る。URLのハッシュで擬似ランダム化して、
    毎日の1,800件がサイト全体の比例サンプルになるようにする。決定的なので再実行しても同じ順。
    """
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def pick_targets(state: dict, budget: int) -> list:
    """最終試行が古い順に budget 件。未照会(空文字)が先頭に来る。"""
    return sorted(state, key=lambda u: (state[u].get("last_attempt_at") or "", _tiebreak(u)))[:budget]


def run(site_url: str, budget: int = DEFAULT_BUDGET, workers: int = DEFAULT_WORKERS,
        refresh_urls: bool = True, run_date=None) -> dict:
    run_date = run_date or date.today()
    state = store.load_state()

    urls = urlset.load()
    if refresh_urls or not urls:
        logger.info("sitemap からURLリストを更新中…")
        try:
            urls = urlset.collect()
            urlset.save(urls)
        except (OSError, RuntimeError) as e:
            # sitemap が落ちていても既存リストで巡回は続ける。止めると穴が空く。
            logger.error(f"sitemap 更新に失敗、既存リストで続行: {e}")
            urls = urlset.load()
    if not urls:
        raise RuntimeError("追跡対象URLが0件。`python -m src.index_status.urlset` を先に実行してください。")
    store.sync_urls(state, urls)

    targets = pick_targets(state, budget)
    logger.info(f"照会対象 {len(targets)} / 全 {len(state)} URL (並列 {workers})")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changes = []
    lock = threading.Lock()
    counters = {"ok": 0, "err": 0, "consecutive_err": 0, "aborted": False, "first_seen": 0}

    def work(url: str):
        if counters["aborted"]:
            return
        raw = fetch_crawl_status.inspect_url(_service(), site_url, url)
        summary = fetch_crawl_status.summarize_result(url, raw)
        with lock:
            row = state[url]
            row["last_attempt_at"] = now
            if summary.get("error"):
                counters["err"] += 1
                counters["consecutive_err"] += 1
                row["error_count"] = str(int(row.get("error_count") or 0) + 1)
                if counters["consecutive_err"] >= ABORT_AFTER_CONSECUTIVE_ERRORS:
                    if not counters["aborted"]:
                        logger.error(
                            f"連続エラー {counters['consecutive_err']} 件。"
                            "クォータ超過か認証失効の可能性があるため中断します。"
                        )
                    counters["aborted"] = True
                return  # 失敗時は前回の状態を保持する（上書きして値を失わない）
            counters["ok"] += 1
            counters["consecutive_err"] = 0
            new_status = normalize(
                summary.get("coverage_state"), summary.get("verdict"), summary.get("indexing_state")
            )
            old_status = row.get("status") or "unchecked"
            row.update({
                "status": new_status,
                "coverage_raw": summary.get("coverage_state") or "",
                "verdict": summary.get("verdict") or "",
                "indexing_state": summary.get("indexing_state") or "",
                "robots_txt_state": summary.get("robots_txt_state") or "",
                "page_fetch_state": summary.get("page_fetch_state") or "",
                "google_canonical": summary.get("google_canonical") or "",
                "user_canonical": summary.get("user_canonical") or "",
                "last_crawl_time": summary.get("last_crawl_time") or "",
                "checked_at": now,
                "error_count": "0",
            })
            if new_status != old_status:
                row["prev_status"] = old_status
                row["changed_at"] = run_date.isoformat()
                # 未照会→何か は「遷移」ではなく初回観測。一巡し終わるまでの約8日間、
                # これを遷移に混ぜると本来見たい変化が毎日1,800件のノイズに埋もれる。
                if old_status == "unchecked":
                    counters["first_seen"] += 1
                    return
                changes.append({
                    "url": url,
                    "group": row.get("group", ""),
                    "from": old_status,
                    "to": new_status,
                    "coverage_raw": row["coverage_raw"],
                    "google_canonical": row["google_canonical"],
                    "user_canonical": row["user_canonical"],
                    "last_crawl_time": row["last_crawl_time"],
                })

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, _ in enumerate(ex.map(work, targets), 1):
            if i % 200 == 0:
                logger.info(f"  {i}/{len(targets)} (成功 {counters['ok']} / 失敗 {counters['err']})")

    counts = Counter(r.get("status") or "unchecked" for r in state.values())
    coverage_state_unknowns = unknown_states()
    summary = {
        "total_urls": len(state),
        "checked_today": counters["ok"],
        "errors_today": counters["err"],
        "aborted": counters["aborted"],
        "changed_today": len(changes),
        "first_seen_today": counters["first_seen"],
        "counts": dict(counts),
        "problem_total": sum(counts.get(k, 0) for k in PROBLEM_STATUSES),
        "unchecked": counts.get("unchecked", 0),
        "unknown_coverage_states": coverage_state_unknowns,
        "generated_at": now,
    }

    store.save_state(state)
    changes.sort(key=lambda c: (c["group"], c["url"]))
    store.save_changes(run_date, changes, summary)
    store.append_daily(run_date, dict(counts), counters["ok"], len(changes))

    logger.info(
        f"完了: 照会 {counters['ok']} 成功 / {counters['err']} 失敗、"
        f"遷移 {len(changes)} 件、初回観測 {counters['first_seen']} 件、"
        f"未照会 残り {summary['unchecked']}"
    )
    if coverage_state_unknowns:
        logger.warning(f"未知の coverageState {len(coverage_state_unknowns)} 種: {coverage_state_unknowns}")
    return summary


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="GSCインデックス状態の日次更新")
    p.add_argument("--budget", type=int, default=int(os.environ.get("GSC_DAILY_BUDGET", DEFAULT_BUDGET)),
                   help=f"1日に照会するURL数 (既定 {DEFAULT_BUDGET}、クォータ上限2,000)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="並列数")
    p.add_argument("--no-refresh-urls", action="store_true", help="sitemap を再取得しない")
    p.add_argument("--date", type=str, help="記録日を明示 (YYYY-MM-DD)")
    args = p.parse_args()

    if args.budget > 2000:
        raise SystemExit("--budget は 2,000 以下にしてください (URL Inspection API の日次クォータ)")

    site = os.environ["GSC_SITE_URL"]
    run(site, budget=args.budget, workers=args.workers,
        refresh_urls=not args.no_refresh_urls,
        run_date=date.fromisoformat(args.date) if args.date else None)


if __name__ == "__main__":
    main()

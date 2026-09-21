"""ダッシュボードHTML + ダウンロード用CSV を生成する。

グラフは「問題ステータスだけ」を描く。インデックス登録済み(1万件規模)と
重複系(数百規模)を同じ軸に載せると後者が潰れて読めなくなるため、
登録済みはスタットタイルの数字として出す（2軸グラフは作らない）。
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.index_status import store
from src.index_status.status_map import (PROBLEM_STATUSES, STATUS_ORDER, label, short)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates"
OUT_DIR = REPO_ROOT / "docs" / "index-status"

# dataviz の既定カテゴリカル配色をスロット順に使用（検証済み: light/dark とも全チェックPASS）。
# 系列の色は順序に固定で紐づける。系列が減っても残りを塗り替えない。
SERIES_COLORS = [
    ("#2a78d6", "#3987e5"),
    ("#eb6834", "#d95926"),
    ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"),
    ("#e87ba4", "#d55181"),
]

# 表示で使うグラフ領域
CHART_W, CHART_H = 860, 320
PAD_L, PAD_R, PAD_T, PAD_B = 52, 74, 16, 34
MAX_TABLE_ROWS = 3000  # 問題URL一覧のインライン上限。全件は CSV へ。


def _nice_ceiling(v: int) -> int:
    """目盛りの上限をきりのいい数に。"""
    if v <= 5:
        return 5
    mag = 10 ** (len(str(int(v))) - 1)
    for m in (1, 2, 2.5, 5, 10):
        if v <= mag * m:
            return int(mag * m)
    return int(mag * 10)


def build_chart(daily: list) -> dict:
    """問題ステータス5系列の推移を SVG 座標に変換。"""
    rows = daily[-90:]  # 直近90日
    if not rows:
        return {"empty": True}
    xs = [r["date"] for r in rows]
    series = []
    ymax_raw = 0
    for i, key in enumerate(PROBLEM_STATUSES):
        vals = [int(r.get(key) or 0) for r in rows]
        ymax_raw = max(ymax_raw, max(vals) if vals else 0)
        series.append({"key": key, "label": label(key), "short": short(key), "values": vals,
                       "light": SERIES_COLORS[i][0], "dark": SERIES_COLORS[i][1]})
    ymax = _nice_ceiling(ymax_raw)

    pw = CHART_W - PAD_L - PAD_R
    ph = CHART_H - PAD_T - PAD_B
    n = len(rows)
    def px(i): return PAD_L + (pw * i / (n - 1) if n > 1 else pw / 2)
    def py(v): return PAD_T + ph - (ph * v / ymax if ymax else 0)

    for s in series:
        pts = [{"x": round(px(i), 1), "y": round(py(v), 1), "v": v, "d": xs[i]}
               for i, v in enumerate(s["values"])]
        s["points"] = pts
        s["path"] = " ".join(("M" if i == 0 else "L") + f"{p['x']},{p['y']}"
                             for i, p in enumerate(pts))
        s["last"] = pts[-1] if pts else None

    # 末尾の直接ラベルは「値だけ」の1行にする。系列名まで置くと、値が近接する
    # 4系列が団子になって読めなくなる（系列の識別は凡例とツールチップが担う）。
    ordered = sorted([s for s in series if s["last"]], key=lambda s: s["last"]["y"])
    prev_y = -99
    for s in ordered:
        y = max(s["last"]["y"], prev_y + 15)
        s["label_y"] = round(y, 1)
        prev_y = s["label_y"]

    yticks = [{"v": round(ymax * f), "y": round(py(ymax * f), 1)} for f in (0, 0.25, 0.5, 0.75, 1)]
    step = max(1, n // 6)
    xticks = [{"label": xs[i][5:], "x": round(px(i), 1)} for i in range(0, n, step)]
    if xticks and xticks[-1]["x"] < px(n - 1) - 30:
        xticks.append({"label": xs[-1][5:], "x": round(px(n - 1), 1)})

    return {
        "empty": False, "w": CHART_W, "h": CHART_H,
        "pad": {"l": PAD_L, "r": PAD_R, "t": PAD_T, "b": PAD_B},
        "plot_w": pw, "plot_h": ph, "series": series,
        "yticks": yticks, "xticks": xticks, "dates": xs,
        "xcoords": [round(px(i), 1) for i in range(n)],
        # コントラストが 3:1 を切る系列色があるため、同じ数字を読める表ビューを必ず添える
        "table": [{"date": xs[i], "vals": [s["values"][i] for s in series]}
                  for i in range(len(xs) - 1, -1, -1)][:30],
    }


def build_matrix(state: dict) -> dict:
    """グループ × ステータス の件数表。"""
    groups = {}
    for row in state.values():
        g = row.get("group") or "その他"
        s = row.get("status") or "unchecked"
        groups.setdefault(g, {}).setdefault(s, 0)
        groups[g][s] += 1
    used = [k for k in STATUS_ORDER if any(k in v for v in groups.values())]
    ordered_groups = sorted(groups, key=lambda g: -sum(groups[g].values()))
    return {
        "statuses": [{"key": k, "label": label(k), "short": short(k)} for k in used],
        "rows": [{
            "group": g,
            "total": sum(groups[g].values()),
            "cells": [groups[g].get(k, 0) for k in used],
            "problems": sum(groups[g].get(k, 0) for k in PROBLEM_STATUSES),
        } for g in ordered_groups],
    }


def _write_csv(path: Path, header: list, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def render(run_date=None) -> Path:
    run_date = run_date or date.today()
    state = store.load_state()
    if not state:
        raise RuntimeError("state.csv が空です。先に `python -m src.index_status.daily` を実行してください。")

    dates = store.list_change_dates()
    today = store.load_changes(run_date) or (store.load_changes(dates[-1]) if dates else {})
    changes = today.get("changes", [])
    summary = today.get("summary", {})
    daily = store.load_daily()

    problems = [r for r in state.values() if (r.get("status") in PROBLEM_STATUSES)]
    problems.sort(key=lambda r: (r.get("group", ""), r.get("status", ""), r.get("url", "")))

    # ダウンロード用CSVは常に全件
    _write_csv(OUT_DIR / "problems.csv",
               ["url", "group", "status", "coverage_raw", "google_canonical",
                "user_canonical", "last_crawl_time", "checked_at"],
               [[r.get("url", ""), r.get("group", ""), label(r.get("status", "")),
                 r.get("coverage_raw", ""), r.get("google_canonical", ""),
                 r.get("user_canonical", ""), r.get("last_crawl_time", ""),
                 r.get("checked_at", "")] for r in problems])
    _write_csv(OUT_DIR / "changes.csv",
               ["url", "group", "from", "to", "coverage_raw", "google_canonical", "last_crawl_time"],
               [[c["url"], c["group"], label(c["from"]), label(c["to"]), c.get("coverage_raw", ""),
                 c.get("google_canonical", ""), c.get("last_crawl_time", "")] for c in changes])

    counts = summary.get("counts") or {}
    if not counts:
        from collections import Counter
        counts = dict(Counter(r.get("status") or "unchecked" for r in state.values()))

    prev_counts = {}
    if len(daily) >= 2:
        prev_counts = {k: int(daily[-2].get(k) or 0) for k in STATUS_ORDER}

    problem_total = sum(counts.get(k, 0) for k in PROBLEM_STATUSES)
    problem_prev = sum(prev_counts.get(k, 0) for k in PROBLEM_STATUSES) if prev_counts else None

    checked_at_list = [r.get("checked_at") for r in state.values() if r.get("checked_at")]
    oldest = min(checked_at_list) if checked_at_list else None

    data = {
        "run_date": run_date.isoformat(),
        "generated_at": summary.get("generated_at") or datetime.now().isoformat(timespec="seconds"),
        "total_urls": len(state),
        "indexed": counts.get("indexed", 0),
        "indexed_delta": counts.get("indexed", 0) - prev_counts["indexed"] if prev_counts else None,
        "problem_total": problem_total,
        "problem_delta": problem_total - problem_prev if problem_prev is not None else None,
        "unchecked": counts.get("unchecked", 0),
        "checked_today": summary.get("checked_today", 0),
        "first_seen_today": summary.get("first_seen_today", 0),
        "errors_today": summary.get("errors_today", 0),
        "aborted": summary.get("aborted", False),
        "oldest_checked_at": oldest[:10] if oldest else None,
        "changes": changes,
        "change_count": len(changes),
        "status_cards": [{
            "key": k, "label": label(k), "short": short(k), "count": counts.get(k, 0),
            "delta": (counts.get(k, 0) - prev_counts[k]) if prev_counts else None,
            "light": SERIES_COLORS[i][0], "dark": SERIES_COLORS[i][1],
        } for i, k in enumerate(PROBLEM_STATUSES)],
        "chart": build_chart(daily),
        "matrix": build_matrix(state),
        "problems_shown": [{
            "url": r.get("url", ""), "group": r.get("group", ""),
            "status": short(r.get("status", "")), "status_key": r.get("status", ""),
            "gc": r.get("google_canonical", ""), "uc": r.get("user_canonical", ""),
            "crawl": (r.get("last_crawl_time") or "")[:10],
        } for r in problems[:MAX_TABLE_ROWS]],
        "problems_total": len(problems),
        "problems_truncated": len(problems) > MAX_TABLE_ROWS,
        "unknown_states": summary.get("unknown_coverage_states") or [],
        "status_labels": {k: short(k) for k in STATUS_ORDER},
        # 短縮名 ↔ GSC の正式文言。画面上でどの指標を見ているか迷わせないため。
        "glossary": [{"short": short(k), "full": label(k)} for k in PROBLEM_STATUSES
                     + ["indexed", "alternate_canonical", "excluded_noindex", "redirect"]],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "data.json").open("w", encoding="utf-8") as f:
        json.dump({k: v for k, v in data.items() if k != "chart"}, f, ensure_ascii=False, indent=1)

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)),
                      autoescape=select_autoescape(["html"]))
    env.filters["comma"] = lambda v: f"{v:,}" if isinstance(v, (int, float)) else v
    html = env.get_template("index_status.html.j2").render(**data)
    out = OUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    logger.info(f"生成: {out} ({len(html):,} bytes)")
    return out


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="インデックス状態ダッシュボードのHTML生成")
    p.add_argument("--date", type=str, help="対象日 (YYYY-MM-DD)")
    a = p.parse_args()
    render(date.fromisoformat(a.date) if a.date else None)

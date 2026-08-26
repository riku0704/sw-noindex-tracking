"""ダッシュボードHTML生成。

Jinja2 テンプレート + データ埋め込み。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
DOCS_DIR = Path(__file__).parent.parent / "docs"


def _diff(new: dict, old: dict) -> dict:
    """1URLの新旧比較。"""
    return {
        "d_clicks": new.get("clicks", 0) - old.get("clicks", 0),
        "d_impressions": new.get("impressions", 0) - old.get("impressions", 0),
        "d_position": round(new.get("avg_position", 0) - old.get("avg_position", 0), 2),
        "d_kw_count": new.get("kw_count", 0) - old.get("kw_count", 0),
    }


def build_dashboard_data(
    targets: dict,
    curr_snapshot: dict,
    prev_snapshot: dict | None,
    baseline_snapshot: dict | None,
    ly_baseline: dict | None,
    ly_current: dict | None,
    crawl_data: list[dict],
) -> dict:
    """テンプレートに渡すデータ構造を組み立て。"""
    empty = {"clicks": 0, "impressions": 0, "avg_position": 0, "kw_count": 0, "keywords": []}

    def build_items(target_list, key_field, agg_key):
        curr_agg = curr_snapshot[agg_key]
        prev_agg = (prev_snapshot or {}).get(agg_key, {})
        base_agg = (baseline_snapshot or {}).get(agg_key, {})
        items = []
        for t in target_list:
            key = t[key_field]
            if key_field == "sid":
                key = int(key)
            curr = curr_agg.get(str(key), empty) or curr_agg.get(key, empty)
            prev = prev_agg.get(str(key), empty) or prev_agg.get(key, empty)
            base = base_agg.get(str(key), empty) or base_agg.get(key, empty)
            items.append({
                **t,
                "prev": prev,
                "curr": curr,
                "baseline": base,
                "diff": _diff(curr, prev),
                "diff_from_baseline": _diff(curr, base),
            })
        return items

    countries = build_items(targets["countries"], "code", "countries_agg")
    cities = build_items(targets["cities"], "cid", "cities_agg")
    schools = build_items(targets["schools"], "sid", "schools_agg")

    # クロール状態を url でマージ
    crawl_map = {c["url"]: c for c in crawl_data}
    for x in countries + cities + schools:
        x["crawl"] = crawl_map.get(x["url"], {})

    summary = _calc_summary(countries, cities, schools)
    yoy = _calc_yoy(countries, ly_baseline, ly_current) if ly_baseline and ly_current else None
    problems = _detect_problems(countries, cities, schools, baseline_snapshot is not None)
    breakdown = {
        "country": _breakdown(countries),
        "city": _breakdown(cities),
        "school": _breakdown(schools),
    }
    crawl_summary = _crawl_summary(countries + cities + schools)

    return {
        "generated_at": datetime.now().isoformat(),
        "period_curr": curr_snapshot["period"],
        "period_prev": (prev_snapshot or {}).get("period"),
        "period_baseline": (baseline_snapshot or {}).get("period"),
        "countries": countries,
        "cities": cities,
        "schools": schools,
        "summary": summary,
        "yoy": yoy,
        "problems": problems,
        "breakdown": breakdown,
        "crawl_summary": crawl_summary,
    }


def _calc_summary(countries, cities, schools):
    def totals(items, k):
        return {
            "clicks": sum(x[k].get("clicks", 0) for x in items),
            "impressions": sum(x[k].get("impressions", 0) for x in items),
            "kw_count": sum(x[k].get("kw_count", 0) for x in items),
        }
    return {
        "country": {"prev": totals(countries, "prev"), "curr": totals(countries, "curr"), "baseline": totals(countries, "baseline")},
        "city": {"prev": totals(cities, "prev"), "curr": totals(cities, "curr"), "baseline": totals(cities, "baseline")},
        "school": {"prev": totals(schools, "prev"), "curr": totals(schools, "curr"), "baseline": totals(schools, "baseline")},
    }


def _calc_yoy(countries, ly_baseline, ly_current):
    """昨年同期比較で純NOINDEX効果を計算。"""
    result = []
    for c in countries:
        code = c["code"]
        p = c["baseline"]["impressions"]
        n = c["curr"]["impressions"]
        this_pct = ((n - p) / p * 100) if p > 0 else None
        ly_p = ly_baseline["countries_agg"].get(code, {}).get("impressions", 0)
        ly_n = ly_current["countries_agg"].get(code, {}).get("impressions", 0)
        ly_pct = ((ly_n - ly_p) / ly_p * 100) if ly_p > 0 else None
        net = this_pct - ly_pct if (this_pct is not None and ly_pct is not None) else None
        result.append({
            "code": code, "jp": c["jp"],
            "cur_prev": p, "cur_curr": n, "cur_pct": round(this_pct, 1) if this_pct else None,
            "ly_prev": ly_p, "ly_curr": ly_n, "ly_pct": round(ly_pct, 1) if ly_pct else None,
            "net_noindex": round(net, 1) if net else None,
        })
    return sorted(result, key=lambda x: x["net_noindex"] if x["net_noindex"] is not None else 0)


def _breakdown(items):
    """KW単位で 消失/新規/継続 を分解。"""
    lost_i = lost_c = 0
    new_i = new_c = 0
    cont_di = cont_dc = 0
    lost_ct = new_ct = cont_ct = 0
    lost_top = []
    new_top = []
    down_top = []
    up_top = []
    for it in items:
        page = it.get("jp") or it.get("city_jp") or it.get("name", "")
        old = {k["query"]: k for k in it["prev"].get("keywords", [])}
        new = {k["query"]: k for k in it["curr"].get("keywords", [])}
        for q in set(old) | set(new):
            o = old.get(q); n = new.get(q)
            if o and not n:
                lost_ct += 1; lost_i += o["impressions"]; lost_c += o["clicks"]
                lost_top.append({"page": page, "query": q, "prev_i": o["impressions"], "prev_c": o["clicks"], "prev_p": o["position"]})
            elif n and not o:
                new_ct += 1; new_i += n["impressions"]; new_c += n["clicks"]
                new_top.append({"page": page, "query": q, "curr_i": n["impressions"], "curr_c": n["clicks"], "curr_p": n["position"]})
            else:
                cont_ct += 1
                di = n["impressions"] - o["impressions"]
                cont_di += di; cont_dc += n["clicks"] - o["clicks"]
                if di < 0:
                    down_top.append({"page": page, "query": q, "prev_i": o["impressions"], "curr_i": n["impressions"], "d_i": di, "prev_p": o["position"], "curr_p": n["position"]})
                elif di > 0:
                    up_top.append({"page": page, "query": q, "prev_i": o["impressions"], "curr_i": n["impressions"], "d_i": di, "prev_p": o["position"], "curr_p": n["position"]})
    return {
        "lost_count": lost_ct, "new_count": new_ct, "cont_count": cont_ct,
        "lost_impr": lost_i, "new_impr": new_i, "cont_delta_impr": cont_di,
        "lost_clicks": lost_c, "new_clicks": new_c, "cont_delta_clicks": cont_dc,
        "net_impr": new_i - lost_i + cont_di,
        "net_clicks": new_c - lost_c + cont_dc,
        "lost_top": sorted(lost_top, key=lambda x: -x["prev_i"])[:10],
        "new_top": sorted(new_top, key=lambda x: -x["curr_i"])[:10],
        "impr_down_top": sorted(down_top, key=lambda x: x["d_i"])[:10],
        "impr_up_top": sorted(up_top, key=lambda x: -x["d_i"])[:10],
    }


def _detect_problems(countries, cities, schools, has_baseline):
    """問題整理タブ用: 大幅減少・クリック消失などを抽出。"""
    from itertools import chain
    problems = {"url_impr_drops": [], "clicks_lost": [], "critical_impr_drop": [], "position_dropoff": []}
    for it in chain(countries, cities, schools):
        cat = "国" if "code" in it else ("都市" if "cid" in it else "学校")
        label = it.get("jp") or it.get("city_jp") or it.get("name", "")
        p, cu = (it["baseline"] if has_baseline else it["prev"]), it["curr"]
        if p.get("impressions", 0) >= 50 and cu.get("impressions", 0) < p["impressions"] * 0.3:
            problems["url_impr_drops"].append({
                "cat": cat, "label": label, "url": it["url"],
                "prev_impr": p["impressions"], "curr_impr": cu["impressions"],
                "prev_clicks": p["clicks"], "curr_clicks": cu["clicks"],
                "prev_pos": p["avg_position"], "curr_pos": cu["avg_position"],
                "drop_pct": round((p["impressions"] - cu["impressions"]) / p["impressions"] * 100, 1),
                "severity": "重大",
            })
        old = {k["query"]: k for k in p.get("keywords", [])}
        new = {k["query"]: k for k in cu.get("keywords", [])}
        for q in set(old) | set(new):
            o, n = old.get(q), new.get(q)
            if o and n and o["impressions"] >= 20:
                dp = (o["impressions"] - n["impressions"]) / o["impressions"] * 100
                if dp >= 50:
                    problems["critical_impr_drop"].append({
                        "cat": cat, "label": label, "query": q,
                        "prev_impr": o["impressions"], "curr_impr": n["impressions"],
                        "drop_pct": round(dp, 1), "prev_pos": o["position"], "curr_pos": n["position"],
                    })
            if o and n and o["position"] <= 10 and n["position"] > 20:
                problems["position_dropoff"].append({
                    "cat": cat, "label": label, "query": q,
                    "prev_pos": o["position"], "curr_pos": n["position"],
                    "prev_impr": o["impressions"], "curr_impr": n["impressions"],
                })
            if o and o["clicks"] >= 1:
                nc = n["clicks"] if n else 0
                if nc == 0:
                    problems["clicks_lost"].append({
                        "cat": cat, "label": label, "query": q,
                        "prev_clicks": o["clicks"], "curr_clicks": nc,
                        "prev_impr": o["impressions"], "curr_impr": n["impressions"] if n else 0,
                        "prev_pos": o["position"], "curr_pos": n["position"] if n else 0,
                    })
    problems["url_impr_drops"].sort(key=lambda x: -x["prev_impr"])
    problems["clicks_lost"].sort(key=lambda x: -x["prev_clicks"])
    problems["critical_impr_drop"].sort(key=lambda x: -x["prev_impr"])
    return problems


def _crawl_summary(items):
    """クロール状況の集計。"""
    from itertools import groupby
    total = len(items)
    with_data = [x for x in items if x.get("crawl") and not x["crawl"].get("error")]
    never_crawled = [x for x in with_data if not x["crawl"].get("last_crawl_time")]
    fresh_7 = [x for x in with_data if x["crawl"].get("days_since_crawl") is not None and x["crawl"]["days_since_crawl"] <= 7]
    stale_30 = [x for x in with_data if x["crawl"].get("days_since_crawl") is not None and x["crawl"]["days_since_crawl"] > 30]
    stale_60 = [x for x in with_data if x["crawl"].get("days_since_crawl") is not None and x["crawl"]["days_since_crawl"] > 60]
    blocked_by_meta = [x for x in with_data if x["crawl"].get("indexing_state") == "BLOCKED_BY_META_TAG"]
    coverage_states = {}
    for x in with_data:
        s = x["crawl"].get("coverage_state", "unknown")
        coverage_states[s] = coverage_states.get(s, 0) + 1
    return {
        "total": total,
        "inspected": len(with_data),
        "never_crawled": len(never_crawled),
        "fresh_7d": len(fresh_7),
        "stale_30d": len(stale_30),
        "stale_60d": len(stale_60),
        "blocked_by_meta_tag": len(blocked_by_meta),
        "coverage_states": coverage_states,
    }


def render(data: dict, output_path: Path | None = None) -> Path:
    """テンプレートをレンダーして出力。"""
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))
    env.globals["json"] = json
    tpl = env.get_template("dashboard.html.j2")
    output_path = output_path or (DOCS_DIR / "index.html")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = tpl.render(d=data, data_json=json.dumps(data, ensure_ascii=False, default=str))
    output_path.write_text(html, encoding="utf-8")
    # data.json も別途出力 (デバッグ・再解析用)
    (output_path.parent / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info(f"Generated {output_path} ({output_path.stat().st_size:,} bytes)")
    return output_path

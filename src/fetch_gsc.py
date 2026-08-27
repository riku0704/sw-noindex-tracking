"""Google Search Console - Search Analytics API 呼び出し。

Windsor.ai を経由せず、公式 Google API を直接使う。
サービスアカウント認証、weekly な page+query 集計を返す。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
ROW_LIMIT = 25000  # GSC API 最大値

# サーバー側で絞り込むための対象URLパターン (RE2)。
# filter_to_targets() の3パターンと同じものを1本にまとめた「粗い前段フィルタ」で、
# targets.json のID照合による厳密な絞り込みは従来どおりクライアント側で行う。
# → ここが多少ゆるく一致しても最終結果は変わらない。
TARGET_PAGE_REGEX = (
    r"^https://schoolwith\.me/"
    r"(countries/school/[A-Z]+|areas/school/[0-9]+|areas/[0-9]+|schools/[0-9]+)/?$"
)


def _build_service():
    """サービスアカウントで Search Console API サービスを構築。"""
    key_json = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    if not key_json:
        raise RuntimeError("GSC_SERVICE_ACCOUNT_JSON env var is required")
    key_info = json.loads(key_json)
    creds = service_account.Credentials.from_service_account_info(key_info, scopes=SCOPES)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def fetch_page_query(
    site_url: str,
    date_from: date,
    date_to: date,
    row_limit: int = ROW_LIMIT,
    page_regex: str | None = TARGET_PAGE_REGEX,
) -> list[dict[str, Any]]:
    """指定期間の page × query 集計を取得。

    Returns: [{"page": str, "query": str, "clicks": int, "impressions": int, "position": float}, ...]
    """
    service = _build_service()
    all_rows: list[dict[str, Any]] = []
    start_row = 0

    while True:
        body = {
            "startDate": date_from.isoformat(),
            "endDate": date_to.isoformat(),
            "dimensions": ["page", "query"],
            "rowLimit": row_limit,
            "startRow": start_row,
            "dataState": "final",
        }
        if page_regex:
            # サイト全体(約6,900ページ)を引かず、対象URLだけをAPI側で絞る。
            body["dimensionFilterGroups"] = [{
                "groupType": "and",
                "filters": [{
                    "dimension": "page",
                    "operator": "includingRegex",
                    "expression": page_regex,
                }],
            }]
        for attempt in range(3):
            try:
                resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
                break
            except HttpError as e:
                if e.resp.status in (429, 500, 503) and attempt < 2:
                    wait = 2 ** attempt
                    logger.warning(f"GSC API {e.resp.status}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    raise
        rows = resp.get("rows", [])
        for r in rows:
            all_rows.append({
                "page": r["keys"][0],
                "query": r["keys"][1],
                "clicks": r["clicks"],
                "impressions": r["impressions"],
                "position": round(r["position"], 4),
            })
        if len(rows) < row_limit:
            break
        start_row += row_limit
        logger.info(f"Fetched {len(all_rows)} rows so far, continuing pagination...")
    logger.info(f"Total {len(all_rows)} page×query rows for {date_from}〜{date_to}")
    return all_rows


def filter_to_targets(rows: list[dict], targets: dict) -> dict[str, list]:
    """全ページから対象URLだけをカテゴリ別に絞り込み。"""
    import re
    codes = {t["code"] for t in targets["countries"]}
    cids = {t["cid"] for t in targets["cities"]}
    sids = {int(t["sid"]) for t in targets["schools"]}

    countries, cities, schools = [], [], []
    for r in rows:
        m = re.match(r"https://schoolwith\.me/countries/school/([A-Z]+)/?$", r["page"])
        if m and m.group(1) in codes:
            countries.append({**r, "code": m.group(1)})
            continue
        m = re.match(r"https://schoolwith\.me/areas/school/(\d+)/?$", r["page"])
        if m and m.group(1) in cids:
            cities.append({**r, "cid": m.group(1)})
            continue
        # 国ページは都市を /areas/{id} でリンクする。/areas/school/{id} とは別ページで、
        # ここを追跡しないと国ページ側のリンク構造の変化を見落とす。同じ都市として合算する。
        m = re.match(r"https://schoolwith\.me/areas/(\d+)/?$", r["page"])
        if m and m.group(1) in cids:
            cities.append({**r, "cid": m.group(1)})
            continue
        m = re.match(r"https://schoolwith\.me/schools/(\d+)/?$", r["page"])
        if m and int(m.group(1)) in sids:
            schools.append({**r, "sid": int(m.group(1))})
    return {"countries": countries, "cities": cities, "schools": schools}


def aggregate(rows: list[dict], key_fn) -> dict[Any, dict]:
    """key単位で集計 + KWリストを返す。"""
    grouped: dict[Any, list] = defaultdict(list)
    for r in rows:
        grouped[key_fn(r)].append(r)
    result = {}
    for k, lst in grouped.items():
        clicks = sum(r["clicks"] for r in lst)
        impr = sum(r["impressions"] for r in lst)
        avg_pos = (
            sum(r["position"] * r["impressions"] for r in lst) / impr
            if impr > 0
            else (sum(r["position"] for r in lst) / len(lst) if lst else 0)
        )
        kws = sorted(lst, key=lambda x: -x["impressions"])
        result[k] = {
            "clicks": clicks,
            "impressions": impr,
            "avg_position": round(avg_pos, 2),
            "kw_count": len(lst),
            "keywords": [
                {"query": r["query"], "clicks": r["clicks"], "impressions": r["impressions"], "position": round(r["position"], 2)}
                for r in kws
            ],
        }
    return result


def fetch_week_snapshot(site_url: str, date_from: date, date_to: date, targets: dict) -> dict:
    """1週間のスナップショットを取得+集計。"""
    raw = fetch_page_query(site_url, date_from, date_to)
    logger.info(f"  → 対象URLに絞り込み前: {len(raw)}行")
    filtered = filter_to_targets(raw, targets)
    c_agg = aggregate(filtered["countries"], lambda r: r["code"])
    ci_agg = aggregate(filtered["cities"], lambda r: r["cid"])
    s_agg = aggregate(filtered["schools"], lambda r: r["sid"])

    return {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "countries_agg": c_agg,
        "cities_agg": ci_agg,
        "schools_agg": s_agg,
    }


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="疎通確認 (直近7日のTop5行表示)")
    args = p.parse_args()

    site = os.environ["GSC_SITE_URL"]
    if args.test:
        today = date.today()
        d_to = today - timedelta(days=4)   # GSC確定データの遅延ぶん下げる
        d_from = d_to - timedelta(days=6)
        rows = fetch_page_query(site, d_from, d_to)
        if not rows:
            # 認証は通ったが対象URLが0件 = 正規表現側の問題を切り分ける
            allrows = fetch_page_query(site, d_from, d_to, page_regex=None)
            print(f"⚠ 対象URL 0行。サイト全体では {len(allrows)}行あります。")
            print(f"  → 認証はOK。TARGET_PAGE_REGEX を確認してください。")
            sys.exit(1)
        print(f"OK. {d_from}〜{d_to} で {len(rows)}行取得。Top5:")
        for r in sorted(rows, key=lambda x: -x["impressions"])[:5]:
            print(f"  {r['impressions']:>4} imp / #{r['position']:.1f}  {r['query']} @ {r['page']}")

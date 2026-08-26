"""GSC URL Inspection API 呼び出し。

各URLの:
- 最終クロール日 (lastCrawlTime)
- インデックス状態 (indexingState, coverageState)
- Google が認識している canonical URL
- robots.txt の状態
を取得。114URL程度なら4-5分で完了。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


def _build_service():
    key_json = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    if not key_json:
        raise RuntimeError("GSC_SERVICE_ACCOUNT_JSON env var is required")
    key_info = json.loads(key_json)
    creds = service_account.Credentials.from_service_account_info(key_info, scopes=SCOPES)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def inspect_url(service, site_url: str, target_url: str) -> dict[str, Any] | None:
    """1URLを inspect。エラー時は None を返す。"""
    body = {
        "inspectionUrl": target_url,
        "siteUrl": site_url,
        "languageCode": "ja-JP",
    }
    for attempt in range(3):
        try:
            resp = service.urlInspection().index().inspect(body=body).execute()
            return resp.get("inspectionResult", {})
        except HttpError as e:
            if e.resp.status in (429, 500, 503) and attempt < 2:
                wait = 2 ** attempt
                logger.warning(f"URL Inspection {e.resp.status} for {target_url}, retry in {wait}s")
                time.sleep(wait)
            else:
                logger.error(f"URL Inspection failed for {target_url}: {e}")
                return None


def summarize_result(url: str, result: dict | None) -> dict[str, Any]:
    """API結果を必要フィールドに絞って正規化。"""
    if not result:
        return {"url": url, "error": True}
    idx = result.get("indexStatusResult", {})
    return {
        "url": url,
        "last_crawl_time": idx.get("lastCrawlTime"),
        "indexing_state": idx.get("indexingState"),  # e.g. INDEXING_ALLOWED / BLOCKED_BY_META_TAG
        "coverage_state": idx.get("coverageState"),  # e.g. "Submitted and indexed"
        "robots_txt_state": idx.get("robotsTxtState"),
        "page_fetch_state": idx.get("pageFetchState"),
        "verdict": idx.get("verdict"),  # PASS / FAIL / NEUTRAL (indexStatusResult 内)
        "google_canonical": idx.get("googleCanonical"),
        "user_canonical": idx.get("userCanonical"),
        "referring_urls_count": idx.get("referringUrls", []).__len__() if idx.get("referringUrls") else 0,
        "sitemaps": idx.get("sitemap", []),
    }


def inspect_all(site_url: str, target_urls: list[str], delay_sec: float = 0.5) -> list[dict]:
    """全URLを順次 inspect。クォータ配慮で軽い間隔を空ける。"""
    service = _build_service()
    results = []
    total = len(target_urls)
    for i, url in enumerate(target_urls, 1):
        logger.info(f"[{i}/{total}] Inspecting {url}")
        r = inspect_url(service, site_url, url)
        results.append(summarize_result(url, r))
        if delay_sec > 0:
            time.sleep(delay_sec)
    ok = sum(1 for r in results if not r.get("error"))
    logger.info(f"URL Inspection 完了: {ok}/{total} 成功")
    return results


def calc_days_since_crawl(last_crawl_time: str | None) -> int | None:
    """ISO8601日時 → 経過日数。"""
    if not last_crawl_time:
        return None
    try:
        dt = datetime.fromisoformat(last_crawl_time.replace("Z", "+00:00"))
        return (datetime.now(dt.tzinfo) - dt).days
    except (ValueError, AttributeError):
        return None


def diff_crawl_data(new: list[dict], old: list[dict] | None) -> list[dict]:
    """前回スナップショットとの差分計算。"""
    old_map = {r["url"]: r for r in (old or [])}
    out = []
    for n in new:
        url = n["url"]
        o = old_map.get(url, {})
        days_now = calc_days_since_crawl(n.get("last_crawl_time"))
        days_prev = calc_days_since_crawl(o.get("last_crawl_time"))
        # 前回クロールと今回クロールの間隔 (クロール頻度の指標)
        crawled_since_last_check = (
            n.get("last_crawl_time") and o.get("last_crawl_time")
            and n["last_crawl_time"] != o["last_crawl_time"]
        )
        out.append({
            **n,
            "days_since_crawl": days_now,
            "prev_last_crawl_time": o.get("last_crawl_time"),
            "prev_days_since_crawl": days_prev,
            "crawled_since_last_check": bool(crawled_since_last_check),
            "prev_indexing_state": o.get("indexing_state"),
            "state_changed": o.get("indexing_state") and o.get("indexing_state") != n.get("indexing_state"),
        })
    return out


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="1URLだけ inspect して結果表示")
    args = p.parse_args()

    site = os.environ["GSC_SITE_URL"]
    if args.test:
        service = _build_service()
        test_url = "https://schoolwith.me/countries/school/UK"
        r = inspect_url(service, site, test_url)
        print(json.dumps(summarize_result(test_url, r), ensure_ascii=False, indent=2))

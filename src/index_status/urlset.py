"""sitemap index を辿って追跡対象URLを組み立てる。

URL は sitemap から取り直す。手書きのリストにすると、サイト側でページが増減した
ときに追跡対象が実態からずれ、「消えたページ」と「最初から見ていないページ」が
区別できなくなる。
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "index_status"
URLS_PATH = DATA_DIR / "urls.csv"

SITEMAP_INDEX = "https://schoolwith.me/sitemaps/index.xml"
UA = "Mozilla/5.0 (compatible; sw-index-status/1.0)"

_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)

# 先に一致したものを採用するので、狭いパターンを上に置く。
_GROUP_RULES = [
    ("LP", re.compile(r"^/lp/")),
    ("口コミ", re.compile(r"^/reviews?/|/review")),
    ("コラム", re.compile(r"^/columns?/")),
    ("スクール", re.compile(r"^/schools?/")),
    ("エリア", re.compile(r"^/areas?/")),
    ("国", re.compile(r"^/countries/")),
    ("カテゴリ", re.compile(r"^/(categor|types?|purposes)")),
    ("ヘルプ", re.compile(r"^/help")),
    ("セミナー", re.compile(r"^/seminars?/")),
    ("マッチング", re.compile(r"^/matchings?")),
]


def classify(url: str) -> str:
    """URL をレポート用のグループへ。パラメータ付きは別枠にする。

    パラメータ付きURLは重複判定の温床なので、通常ページと混ぜると
    グループ別の件数が読めなくなる。
    """
    p = urlparse(url)
    if p.query:
        return "パラメータ付き"
    path = p.path or "/"
    if path == "/":
        return "TOP"
    for name, rx in _GROUP_RULES:
        if rx.search(path):
            return name
    return "その他"


def _fetch(url: str) -> str:
    r = requests.get(url, timeout=60, headers={"User-Agent": UA})
    r.raise_for_status()
    body = r.content
    if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
        body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
    return body.decode("utf-8", errors="replace")


def _locs(xml: str) -> list:
    # sitemap は HTML の 404 ページが返ることがある。<loc> が無ければ空扱いで進める。
    return [m.group(1).replace("&amp;", "&") for m in _LOC_RE.finditer(xml)]


def collect(index_url: str = SITEMAP_INDEX) -> list:
    """sitemap index → 子sitemap → 全URL。(url, group, sitemap) のリストを返す。"""
    children = _locs(_fetch(index_url))
    if not children:
        raise RuntimeError(f"sitemap index に <loc> がありません: {index_url}")
    logger.info(f"子sitemap {len(children)} 件")

    seen = {}
    for child in children:
        name = child.rsplit("/", 1)[-1].replace(".xml", "")
        try:
            urls = _locs(_fetch(child))
        except requests.RequestException as e:
            logger.error(f"sitemap 取得失敗 {child}: {e}")
            continue
        logger.info(f"  {name}: {len(urls)} URL")
        for u in urls:
            # 複数sitemapに載るURLは最初に見つけたsitemapに帰属させる
            if u not in seen:
                seen[u] = (u, classify(u), name)
    return sorted(seen.values())


def save(rows: list, path=None) -> Path:
    path = path or URLS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["url", "group", "sitemap"])
        w.writerows(rows)
    logger.info(f"保存: {path} ({len(rows)} URL)")
    return path


def load(path=None) -> list:
    path = path or URLS_PATH
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return [(r["url"], r["group"], r["sitemap"]) for r in csv.DictReader(f)]


if __name__ == "__main__":
    import argparse
    from collections import Counter

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="sitemap から追跡対象URLリストを再生成")
    p.add_argument("--dry-run", action="store_true", help="保存せず件数だけ表示")
    args = p.parse_args()

    rows = collect()
    print(f"\n合計 {len(rows)} URL")
    for g, n in Counter(r[1] for r in rows).most_common():
        print(f"  {g:14s} {n:6,d}")
    if not args.dry_run:
        save(rows)

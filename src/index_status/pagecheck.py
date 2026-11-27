"""改修案の「根拠」を集める：実ページ・サイトマップ・robots.txt を取得して事実だけを記録する。

なぜ必要か
----------
URL Inspection API が教えてくれるのは「Googleがどう判定したか」だけで、
「サイト側の何が原因か」は分からない。原因はページの HTML（canonical / robots meta）、
サイトマップの中身、robots.txt にある。改修案を推測で書かないために、これらを実際に取りに行く。

全URLは取らない。エラーは型（テンプレート）単位で起きるので、型ごとに数件を取れば
その型の設定が分かる。結果は `page_checks.csv` にキャッシュし、古い順に1日 BUDGET 件まで更新する。
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests

from src.index_status import urlset
from src.index_status.patterns import template

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "index_status"
CHECKS_PATH = DATA_DIR / "page_checks.csv"
SITE_FACTS_PATH = DATA_DIR / "site_facts.json"

UA = urlset.UA
# 問題URL＋Google未認識（約4,000件）を14日で一巡させると1日約290件。型サンプルと再試行ぶんを足した値。
DEFAULT_BUDGET = 500
# 問題が無いURLは型ごとにこの件数だけ取る（型の設定を知るため・登録済みとの比較用）。
SAMPLES_PER_TEMPLATE = 2
INDEXED_SAMPLES_PER_TEMPLATE = 2
# テンプレートの設定はそう頻繁に変わらない。改修の反映を2週間以内に拾えれば十分。
MAX_AGE_DAYS = 14
WORKERS = 3  # 自社サイトへの負荷を抑える

CHECK_FIELDS = ["url", "template", "fetched_at", "http_status", "final_url", "canonical",
                "robots", "text_len", "title", "error"]

_CANON_RE = re.compile(r"<link\b[^>]*\brel=[\"']?canonical[\"']?[^>]*>", re.I)
_HREF_RE = re.compile(r"\bhref=[\"']([^\"']+)[\"']", re.I)
_ROBOTS_RE = re.compile(r"<meta\b[^>]*\bname=[\"']?(?:robots|googlebot)[\"']?[^>]*>", re.I)
_CONTENT_RE = re.compile(r"\bcontent=[\"']([^\"']*)[\"']", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_STRIP_RE = re.compile(r"<script\b.*?</script>|<style\b.*?</style>|<noscript\b.*?</noscript>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_page(url: str, html: str) -> dict:
    head = html[:200_000]
    canon = ""
    m = _CANON_RE.search(head)
    if m:
        h = _HREF_RE.search(m.group(0))
        canon = urljoin(url, unescape(h.group(1))) if h else ""
    robots = []
    for tag in _ROBOTS_RE.findall(head):
        c = _CONTENT_RE.search(tag)
        if c:
            robots.append(c.group(1).strip().lower())
    t = _TITLE_RE.search(head)
    text = _TAG_RE.sub(" ", _STRIP_RE.sub(" ", html))
    return {
        "canonical": canon,
        "robots": ", ".join(robots),
        # 空白を除いた本文の文字数。日本語ページなので語数ではなく文字数で見る。
        "text_len": len("".join(unescape(text).split())),
        "title": " ".join(unescape(t.group(1)).split())[:120] if t else "",
    }


def fetch(url: str) -> dict:
    row = {"url": url, "template": template(url),
           "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": UA}, allow_redirects=True)
    except requests.RequestException as e:
        row["error"] = type(e).__name__
        return row
    row["http_status"] = str(r.history[0].status_code if r.history else r.status_code)
    row["final_url"] = r.url if r.url != url else ""
    xr = r.headers.get("X-Robots-Tag", "")
    if r.status_code == 200 and "html" in r.headers.get("Content-Type", ""):
        row.update(parse_page(r.url, r.text))
    if xr:
        row["robots"] = ", ".join(filter(None, [row.get("robots", ""), xr.lower()]))
    return row


def load_checks(path=None) -> dict:
    path = path or CHECKS_PATH
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {r["url"]: r for r in csv.DictReader(f)}


def save_checks(checks: dict, path=None) -> None:
    path = path or CHECKS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CHECK_FIELDS, extrasaction="ignore")
        w.writeheader()
        for u in sorted(checks):
            w.writerow({k: checks[u].get(k, "") for k in CHECK_FIELDS})


def _h(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def choose_samples(records: list) -> set:
    """取得するURL。records は issues.build_records() の行。

    問題URLは全件取る。noindex のようにURLごとに設定が違うもの（NOINDEX施策の対象校など）があり、
    型のサンプルから全件を推し量ると誤るため。問題の無いURLは型ごとに数件だけ取る。
    同順位はURLのハッシュで決める（毎回同じURLを選ぶ：日によってサンプルが変わると
    「設定が変わった」のか「別のURLを見た」のか区別できなくなるため）。
    """
    by_t = {}
    for r in records:
        by_t.setdefault(r["template"], []).append(r)
    chosen = set()
    for rows in by_t.values():
        # 「Google未認識」は問題ステータスに数えていないが、クロール待ちの判定には実物が要る
        chosen.update(r["url"] for r in rows if r["problem"] or r["status"] == "unknown_to_google")
        rest = sorted((r for r in rows if not r["problem"] and r["status"] != "indexed"),
                      key=lambda r: (not r["in_sitemap"], _h(r["url"])))
        idx = sorted((r for r in rows if r["status"] == "indexed"), key=lambda r: _h(r["url"]))
        chosen.update(r["url"] for r in rest[:SAMPLES_PER_TEMPLATE] + idx[:INDEXED_SAMPLES_PER_TEMPLATE])
    return chosen


def refresh(records: list, budget: int = DEFAULT_BUDGET) -> dict:
    """サンプルのうち未取得・期限切れのものを古い順に budget 件取得する。"""
    checks = load_checks()
    want = choose_samples(records)
    # サンプルから外れたURLの結果は捨てる（古い結果が根拠に混ざらないように）
    checks = {u: c for u, c in checks.items() if u in want}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)).isoformat()
    stale = sorted((u for u in want if (checks.get(u, {}).get("fetched_at") or "") < cutoff),
                   key=lambda u: (checks.get(u, {}).get("fetched_at") or "", _h(u)))[:budget]
    logger.info(f"ページ点検: サンプル {len(want):,} 件中 {len(stale):,} 件を取得")
    lock = threading.Lock()

    def work(u):
        row = fetch(u)
        with lock:
            checks[u] = row

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(work, stale))
    save_checks(checks)
    errs = sum(1 for u in stale if checks[u].get("error"))
    if errs:
        logger.warning(f"ページ点検: 取得失敗 {errs} 件")
    return checks


# ---- サイト全体の事実（サイトマップ・robots.txt） ----

def collect_site_facts(index_url: str = urlset.SITEMAP_INDEX) -> dict:
    """子サイトマップごとのURL数・lastmod数、パラメータURLだけのサイトマップ、robots.txt。"""
    from src.index_status.patterns import has_params

    facts = {"collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "sitemaps": [], "robots_txt": ""}
    try:
        children = urlset._locs(urlset._fetch(index_url))
    except (requests.RequestException, OSError) as e:
        logger.error(f"sitemap index 取得失敗: {e}")
        return facts
    for child in children:
        try:
            xml = urlset._fetch(child)
        except requests.RequestException as e:
            logger.error(f"sitemap 取得失敗 {child}: {e}")
            continue
        locs = urlset._locs(xml)
        facts["sitemaps"].append({
            "name": child.rsplit("/", 1)[-1],
            "url": child,
            "urls": len(locs),
            "lastmod": len(re.findall(r"<lastmod>", xml, re.I)),
            "param_urls": sum(1 for u in locs if has_params(u)),
            "examples": locs[:3],
        })
    try:
        r = requests.get(urljoin(index_url, "/robots.txt"), timeout=30, headers={"User-Agent": UA})
        facts["robots_txt"] = r.text if r.status_code == 200 else ""
    except requests.RequestException as e:
        logger.error(f"robots.txt 取得失敗: {e}")
    with SITE_FACTS_PATH.open("w", encoding="utf-8") as f:
        json.dump(facts, f, ensure_ascii=False, indent=1)
    return facts


def load_site_facts() -> dict:
    if not SITE_FACTS_PATH.exists():
        return {}
    with SITE_FACTS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def googlebot_allowed(robots_txt: str, url: str) -> bool:
    """robots.txt で Googlebot がそのURLを取得できるか。

    標準の robotparser はワイルドカード（`*`）を解釈しないので、Googleの仕様に合わせて
    最長一致・Allow優先で自前判定する。
    """
    from urllib.parse import urlparse
    p = urlparse(url)
    target = (p.path or "/") + (("?" + p.query) if p.query else "")
    groups, cur, agents_open = {}, None, False
    for line in robots_txt.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (s.strip() for s in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if not agents_open:
                cur = []
            agents_open = True
            cur.append(v.lower())
            for a in cur:
                groups.setdefault(a, [])
        elif k in ("allow", "disallow") and cur is not None:
            agents_open = False
            for a in cur:
                groups[a].append((k, v))
    rules = groups.get("googlebot") or groups.get("*") or []
    best = ("allow", "")
    for k, pat in rules:
        if not pat:
            continue
        rx = "^" + re.escape(pat).replace(r"\*", ".*").replace(r"\$", "$")
        if re.match(rx, target) and (len(pat) > len(best[1]) or
                                     (len(pat) == len(best[1]) and k == "allow")):
            best = (k, pat)
    return best[0] == "allow"


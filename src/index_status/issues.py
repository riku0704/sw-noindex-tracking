"""エラーの集計 → 差分 → 改修案 をまとめて作る。

入力は3つだけ：
- state.csv           … URL Inspection API の最新結果（Googleの判定）
- gsc_urls.csv        … GSCのページレポートから取り込んだ問題URL（sitemap外の実体）
- page_checks.csv 他  … pagecheck.py が取った実ページ・サイトマップ・robots.txt（サイト側の原因）

改修案はルールで作る。各ルールは「データで確かめられる条件」でだけ発火し、
根拠には件数と実例をそのまま書く。サンプル取得で確かめた事実は「取得 k件中 j件」と
明記し、型の全件に当てはまると言い切らない。どのルールにも当たらなかった問題URLは
「原因未特定」として件数を必ず出す（黙って落とさない）。

ルールは上から順に評価し、先に当たったルールがそのURLを「引き取る」。
同じURLを2つの改修案で数えないため。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from src.index_status import gsc_export, pagecheck, store
from src.index_status.patterns import (SORT_NAMED, TRACKING_KEYS, clean_canonical, has_params, named_params,
                                       nesting_depth, page_number, query_keys, same_url, template,
                                       trap_keys)
from src.index_status.status_map import PROBLEM_STATUSES, short

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = REPO_ROOT / "index_status"
# 週次NOINDEXパイプラインの対象URL。意図して noindex にしたページなので、エラーとして扱わない。
TARGETS_PATH = REPO_ROOT / "targets.json"
ISSUES_DIR = DATA_DIR / "issues"
OUT_DIR = REPO_ROOT / "docs" / "index-status"

SEV_LABEL = {1: "高", 2: "中", 3: "低"}
GONE_STATUSES = {"not_found", "soft_404", "server_error", "redirect"}
WAITING_STATUSES = {"discovered_not_indexed", "unknown_to_google"}
# ログイン・会員登録・申込フォームなど、検索結果に出す意味がないページ。URL名で判定する。
UTILITY_RE = re.compile(
    r"/(auth|login|logout|users|register|f_register|mypage|bookmarks?|schoolbookmarks)(/|$|\?)"
    r"|/applications/|/reviews/check|_complete(/|$)", re.I)
MAX_EXAMPLES = 5


# ---------------------------------------------------------------- records

def build_records(state: dict, gsc_urls: list) -> list:
    """URLごとの判定材料を1行にまとめる。

    status は URL Inspection の結果を正とし、未照会のときだけ GSC エクスポート時点の
    ステータスで補う（source=gsc のURLは照会が一巡するまで未照会のため）。
    """
    gsc = {}
    for url, st in gsc_urls:
        gsc[url] = st
    out = []
    for url in set(state) | set(gsc):
        r = state.get(url, {})
        st = r.get("status") or "unchecked"
        status_src = "inspection"
        if st == "unchecked" and url in gsc:
            st, status_src = gsc[url], "gsc_export"
        source = r.get("source") or ("gsc" if url not in state else ("both" if url in gsc else "sitemap"))
        in_sitemap = bool(r.get("sitemap")) if url in state else False
        out.append({
            "url": url,
            "template": template(url),
            "group": r.get("group", ""),
            "status": st,
            "status_src": status_src,
            "problem": st in PROBLEM_STATUSES,
            "in_sitemap": in_sitemap,
            "sitemap": r.get("sitemap", ""),
            "source": source,
            "gc": r.get("google_canonical", ""),
            "uc": r.get("user_canonical", ""),
            "last_crawl": r.get("last_crawl_time", ""),
        })
    out.sort(key=lambda r: r["url"])
    return out


# ---------------------------------------------------------------- helpers

def _indexable(c: dict, url: str) -> bool:
    """取得結果から見て、Googleがインデックスしてよい状態か。"""
    if not c or c.get("error") or c.get("http_status") != "200" or c.get("final_url"):
        return False
    if "noindex" in (c.get("robots") or ""):
        return False
    return not c.get("canonical") or same_url(c["canonical"], url)


def _status_breakdown(rows: list) -> str:
    cnt = Counter(r["status"] for r in rows)
    return "、".join(f"{short(k)} {v:,}" for k, v in cnt.most_common())


def _issue_id(rule: str, key: str) -> str:
    return f"{rule}:{hashlib.md5(key.encode('utf-8')).hexdigest()[:8]}"


def _issue(rule, key, sev, title, urls, evidence, fix, verify, owner="エンジニア",
           templates=None, examples=None) -> dict:
    urls = sorted(set(urls))
    return {
        "id": _issue_id(rule, key),
        "rule": rule,
        "severity": sev,
        "severity_label": SEV_LABEL[sev],
        "title": title,
        "count": len(urls),
        "templates": templates or [],
        "evidence": evidence,
        "fix": fix,
        "verify": verify,
        "owner": owner,
        "examples": (examples or urls)[:MAX_EXAMPLES],
        "urls": urls,
    }


def _by_template(rows: list) -> dict:
    out = defaultdict(list)
    for r in rows:
        out[r["template"]].append(r)
    return out


def _checked(rows: list, checks: dict) -> list:
    return [(r, checks[r["url"]]) for r in rows if r["url"] in checks and not checks[r["url"]].get("error")]


# ---------------------------------------------------------------- rules

def rule_param_only_sitemaps(recs, checks, facts, claimed):
    files = [s for s in facts.get("sitemaps", []) if s["urls"] and s["param_urls"] == s["urls"]]
    if not files:
        return []
    names = {s["url"].rsplit("/", 1)[-1].replace(".xml", "") for s in files}
    rows = [r for r in recs if r["in_sitemap"] and r["sitemap"] in names]
    ev = [f"サイトマップ索引 {len(facts['sitemaps'])} 本のうち {len(files)} 本が、パラメータ付きURLだけで構成されている。"]
    for s in files:
        ex = s["examples"][0] if s["examples"] else ""
        c = checks.get(ex, {})
        tail = ""
        if c.get("canonical") and not same_url(c["canonical"], ex):
            tail = f"（ページの canonical は {c['canonical']}）"
        if "noindex" in (c.get("robots") or ""):
            tail += "（ページは noindex）"
        ev.append(f"{s['name']}: {ex}{tail}")
    ev.append("サイトマップに載せたURLは「検索結果に出したい正規URL」としてGoogleに申告したことになる。"
              "パラメータ違いのURLを自分で申告している状態。")
    claimed.update(r["url"] for r in rows)
    return [_issue(
        "sitemap_param_files", "all", 2,
        f"パラメータ付きURLだけのサイトマップが {len(files)} 本ある",
        [r["url"] for r in rows] or [s["examples"][0] for s in files if s["examples"]],
        ev,
        [f"サイトマップ索引（index.xml）から {', '.join(s['name'] for s in files)} を外す。",
         "サイトマップの生成処理で、クエリ（?〜）や名前付きパラメータ（/page:2 等）を含むURLを出力しないようにする。"],
        "index.xml に上記ファイルが含まれていないこと。次回以降このカードが消える。",
        templates=sorted({template(s['examples'][0]) for s in files if s["examples"]}),
    )]


def rule_crawl_trap(recs, checks, facts, claimed):
    rows = [r for r in recs if r["url"] not in claimed and nesting_depth(r["url"]) >= 1]
    if not rows:
        return []
    keys = Counter(k for r in rows for k in trap_keys(r["url"]))
    key = keys.most_common(1)[0][0]
    depth = max(nesting_depth(r["url"]) for r in rows)
    longest = max(len(r["url"]) for r in rows)
    tmpl = Counter(r["template"].split("?")[0] for r in rows)
    ex = min(rows, key=lambda r: len(r["url"]))["url"]
    allowed = pagecheck.googlebot_allowed(facts.get("robots_txt", ""), ex) if facts.get("robots_txt") else None
    ev = [
        f"クエリ {', '.join(f'{k}=' for k, _ in keys.most_common())} の値に、そのページ自身のURLが丸ごと埋め込まれている。"
        f"リンクを辿るたびに1段ずつ入れ子が深くなり、URLが際限なく増える（確認できた最大の深さ {depth} 段・最長 {longest:,} 文字）。",
        f"確認できた件数 {len(rows):,}（{_status_breakdown(rows)}）。"
        "GSCエクスポートは1ステータス1,000件上限なので、実数はこれより多い。",
        "発生している型: " + "、".join(f"{t}（{n}）" for t, n in tmpl.most_common(6)),
    ]
    if allowed is True:
        ev.append(f"robots.txt はこれらのURLのクロールを許可している（{key}= を含むURLを Disallow していない）。")
    cks = _checked(rows, checks)
    if cks:
        ok = sum(1 for r, c in cks if clean_canonical(c.get("canonical", "")))
        nx = sum(1 for r, c in cks if "noindex" in (c.get("robots") or ""))
        ev.append(f"実ページ {len(cks)} 件を取得：canonical が並び替え・クエリ無しのURLを指していた {ok} 件、"
                  f"noindex {nx} 件。ページ単体の設定は概ね正しく、問題はURLが無限に生まれてクロールが浪費されること。")
    claimed.update(r["url"] for r in rows)
    fix = [f"並び替え・絞り込みリンクの生成処理で、{key} パラメータに現在のURLを入れない（入れ子の発生源を断つ）。",
           f"robots.txt に `Disallow: /*{key}=` を追加し、既に生まれたURLをクロール対象から外す。"]
    fix.append("並び替えリンクはボタン（フォーム送信／JS）にするか rel=\"nofollow\" を付け、クローラーに辿らせない。")
    return [_issue("crawl_trap", key, 1 if len(rows) >= 50 else 2,
                   f"並び替えリンクが {key}= に自分のURLを入れ子で埋め込み、URLが無限に増えている",
                   [r["url"] for r in rows], ev, fix,
                   f"GSCの問題URLに占める {key}= 付きURLの件数（取り込み時に自動で数え直す）。robots.txt への追記はこのツールが毎日確認する。",
                   templates=[t for t, _ in tmpl.most_common(6)], examples=[ex])]


def rule_utility_pages(recs, checks, facts, claimed):
    rows = [r for r in recs if r["url"] not in claimed and r["problem"] and UTILITY_RE.search(r["url"])]
    if not rows:
        return []
    bt = _by_template(rows)
    noidx = [r for r, c in _checked(rows, checks) if "noindex" in (c.get("robots") or "")]
    cks = _checked(rows, checks)
    ev = ["URL名から判定（ログイン・会員登録・申込フォーム・投稿確認など、検索結果に出す意味がないページ）。",
          f"該当 {len(rows):,} 件（{_status_breakdown(rows)}）。",
          "型: " + "、".join(f"{t}（{len(v)}）" for t, v in sorted(bt.items(), key=lambda x: -len(x[1]))[:8])]
    if cks:
        ev.append(f"実ページ {len(cks)} 件を取得し、noindex が付いていたのは {len(noidx)} 件。")
    claimed.update(r["url"] for r in rows)
    return [_issue("utility_pages", "all", 2,
                   "ログイン・申込フォーム等のページがインデックス対象として扱われている",
                   [r["url"] for r in rows], ev,
                   ["これらのテンプレートに <meta name=\"robots\" content=\"noindex\"> を付ける"
                    "（robots.txt で塞ぐと noindex が読まれず、URLだけが残るので先に noindex）。",
                    "サイト内からこれらへのリンク（申込ボタン等）に rel=\"nofollow\" を付ける。",
                    "サイトマップに載っていれば外す。"],
                   "該当URLのステータスが「noindex除外」に変わること（巡回一巡＝約9日で確認できる）。",
                   templates=sorted(bt, key=lambda t: -len(bt[t]))[:8])]


def rule_sitemap_gone(recs, checks, facts, claimed):
    rows = []
    for r in recs:
        if r["url"] in claimed or not r["in_sitemap"]:
            continue
        c = checks.get(r["url"], {})
        if r["status"] in GONE_STATUSES or (c and not c.get("error") and
                                             (c.get("http_status") not in ("200", None, "") or c.get("final_url"))):
            rows.append(r)
    if not rows:
        return []
    claimed.update(r["url"] for r in rows)
    redirected = [(r["url"], checks[r["url"]]["final_url"]) for r in rows if checks.get(r["url"], {}).get("final_url")]
    ev = [f"サイトマップ掲載URLのうち {len(rows):,} 件が 404・リダイレクト・サーバーエラー等（{_status_breakdown(rows)}）。"]
    for a, b in redirected[:3]:
        ev.append(f"リダイレクト例: {a} → {b}")
    return [_issue("sitemap_gone", "all", 2, "サイトマップに、存在しない・転送されるURLが載っている",
                   [r["url"] for r in rows], ev,
                   ["削除済みページはサイトマップから外す。", "転送しているURLは転送先のURLに置き換える。"],
                   "該当URLがサイトマップから消えること。",
                   templates=sorted({r["template"] for r in rows}))]


def rule_sitemap_noindex(recs, checks, facts, claimed):
    rows = [r for r in recs if r["url"] not in claimed and r["in_sitemap"] and
            (r["status"] == "excluded_noindex" or "noindex" in (checks.get(r["url"], {}).get("robots") or ""))]
    if not rows:
        return []
    claimed.update(r["url"] for r in rows)
    return [_issue("sitemap_noindex", "all", 2, "noindex のページがサイトマップに載っている",
                   [r["url"] for r in rows],
                   [f"{len(rows):,} 件。サイトマップは「インデックスしてほしいURL」の申告なので、noindex と矛盾する。",
                    "型: " + "、".join(sorted({r['template'] for r in rows})[:8])],
                   ["サイトマップの生成対象から noindex のテンプレートを外す。"],
                   "該当URLがサイトマップから消えること。",
                   templates=sorted({r["template"] for r in rows}))]


def rule_sitemap_noncanonical(recs, checks, facts, claimed):
    out = []
    sm = [r for r in recs if r["in_sitemap"] and r["url"] not in claimed]
    for t, rows in _by_template(sm).items():
        cks = _checked(rows, checks)
        bad = [(r, c) for r, c in cks if c.get("canonical") and not same_url(c["canonical"], r["url"])]
        if not bad:
            continue
        k, j = len(cks), len(bad)
        whole = j == k
        target = rows if whole else [r for r, _ in bad]
        ex_r, ex_c = bad[0]
        ev = [f"サイトマップ掲載 {len(rows):,} 件。実ページを {k} 件取得し、{j} 件で canonical が別のURLを指していた"
              f"（例: {ex_r['url']} → {ex_c['canonical']}）。" + ("" if whole else " 残りの件は未確認のため件数に含めていない。"),
              f"ステータス内訳: {_status_breakdown(rows)}"]
        declared = [r for r in rows if r["uc"] and not same_url(r["uc"], r["url"])]
        ignored = [r for r in declared if r["gc"] and same_url(r["gc"], r["url"])]
        if declared:
            ev.append(f"URL検査でも、Googleが読んだ canonical が別URLになっているもの {len(declared):,} 件。"
                      f"うち {len(ignored):,} 件は Google が宣言を採用せず、そのURL自身を正規として扱っている"
                      "（サイトマップ掲載と canonical が矛盾しているため）。")
        ev.append("サイトマップは「これが正規URL」という申告、canonical は「正規は別のURL」という申告で、互いに打ち消している。")
        claimed.update(r["url"] for r in target)
        out.append(_issue(
            "sitemap_noncanonical", t, 1 if (whole and len(rows) >= 100) else 2,
            f"{t} がサイトマップに載っているのに、canonical は別のページを指している",
            [r["url"] for r in target], ev,
            [f"検索結果に単独で出す必要がないなら、{t} をサイトマップから外す（canonical先のページだけを載せる）。",
             "単独で検索に出したいページなら、canonical を自分自身のURLに変える。どちらに寄せるかは事業判断。"],
            "サイトマップから外した場合：該当URLが「代替ページ（適切なcanonicalあり）」に移る。",
            templates=[t]))
    return out


def rule_canonical_conflict(recs, checks, facts, claimed):
    rows = [r for r in recs if r["url"] not in claimed and r["status"] == "dup_google_chose_different"]
    if not rows:
        return []
    claimed.update(r["url"] for r in rows)
    pairs = [f"{r['url']} を正規と宣言 → Google は {r['gc']} を採用" for r in rows if r["gc"]]
    return [_issue("canonical_conflict", "all", 2 if len(rows) >= 5 else 3,
                   "Google が、こちらの指定とは別のページを正規として選んでいる",
                   [r["url"] for r in rows],
                   [f"{len(rows):,} 件。Google は2つのページを同じ内容とみなし、指定を上書きしている。"] + pairs[:5]
                   + ([] if pairs else ["（GSCエクスポート由来のため、Googleが選んだURLは照会が一巡すると判明する）"]),
                   ["ペアごとにどちらを残すか決め、残さない側から 301 で転送する。",
                    "両方残す場合は、内容を差別化する（同じ一覧・同じ文面になっていないか確認）。"],
                   "該当URLのステータスが「登録済み」か「代替ページ」に変わること。",
                   owner="SEO・エンジニア", templates=sorted({r["template"] for r in rows}))]


def rule_pagination_canonical(recs, checks, facts, claimed):
    """2ページ目以降の canonical が1ページ目を指している型。"""
    rows = [r for r in recs if r["url"] not in claimed and r["problem"] and page_number(r["url"]) >= 2]
    out_urls, tmpls, ex = [], Counter(), None
    k_all = j_all = 0
    for t, g in _by_template(rows).items():
        cks = [(r, c) for r, c in _checked(g, checks) if c.get("canonical")]
        bad = [(r, c) for r, c in cks if page_number(c["canonical"]) == 1]
        if not bad:
            continue
        k_all += len(cks)
        j_all += len(bad)
        target = g if len(bad) == len(cks) else [r for r, _ in bad]
        out_urls += [r["url"] for r in target]
        tmpls[t.split("?")[0]] += len(target)
        ex = ex or bad[0]
    if not out_urls:
        return []
    claimed.update(out_urls)
    return [_issue("pagination_canonical", "all", 2,
                   "一覧の2ページ目以降の canonical が1ページ目を指している",
                   out_urls,
                   [f"{len(out_urls):,} 件（取得 {k_all} 件中 {j_all} 件で確認。例: {ex[0]['url']} → {ex[1]['canonical']}）。",
                    "2ページ目以降は1ページ目と載っている学校が違うので、Google は「別の内容なのに重複と宣言されている」と扱い、"
                    "2ページ目以降の学校ページへのリンクも評価されにくくなる。Google は各ページに自分自身の canonical を付けるよう推奨している。",
                    "同じサイトでも /category/ 系の一覧は page: を canonical に残している（型によって実装が違う）。",
                    "型: " + "、".join(f"{t}（{n}）" for t, n in tmpls.most_common(6))],
                   ["2ページ目以降の canonical を、そのページ自身（page: を残し、並び替え・クエリは除いたURL）にする。"
                    "/category/ 系と同じ実装に揃える。"],
                   "取得し直したときに canonical が page: を含むこと（このツールが14日以内に再取得）。",
                   templates=[t for t, _ in tmpls.most_common(6)])]


def _noindex_targets() -> set:
    if not TARGETS_PATH.exists():
        return set()
    with TARGETS_PATH.open(encoding="utf-8") as f:
        t = json.load(f)
    return {x["url"] for v in t.values() if isinstance(v, list) for x in v if x.get("url")}


def rule_intentional_noindex(recs, checks, facts, claimed):
    """Phase-1 NOINDEX施策（targets.json）で意図して外したページ。"""
    targets = _noindex_targets()
    rows = [r for r in recs if r["url"] not in claimed and r["url"] in targets
            and r["status"] not in ("indexed", "excluded_noindex", "unchecked")]
    if not rows:
        return []
    claimed.update(r["url"] for r in rows)
    nx = sum(1 for r, c in _checked(rows, checks) if "noindex" in (c.get("robots") or ""))
    return [_issue("intentional_noindex", "all", 3, "NOINDEX施策の対象ページ（意図どおり・監視のみ）",
                   [r["url"] for r in rows],
                   [f"targets.json（Phase-1 NOINDEX 114URL）に含まれるURL {len(rows):,} 件（{_status_breakdown(rows)}）。"
                    f"実ページで noindex を確認 {nx} 件。Google が再クロールすると「noindex除外」に移る。"],
                   ["対応不要。効果測定は週次ダッシュボード（docs/index.html）で行う。"],
                   "ステータスが「noindex除外」に移っていくこと。", owner="監視のみ",
                   templates=sorted({r["template"] for r in rows}))]


def rule_already_noindex(recs, checks, facts, claimed):
    """ページ側は既に noindex。Google が再クロールして除外に移すのを待つだけのもの。

    noindex はURLごとに違いうる（施策で一部の学校だけ外す等）ので、型から推し量らず、
    実際に取得して noindex を確認できたURLだけを数える。
    """
    target = [r for r in recs if r["url"] not in claimed and r["problem"]
              and "noindex" in (checks.get(r["url"], {}).get("robots") or "")]
    if not target:
        return []
    claimed.update(r["url"] for r in target)
    tm = Counter(r["template"].split("?")[0] for r in target)
    return [_issue("already_noindex", "all", 3, "既に noindex 済み（Google の再クロール待ち・監視のみ）",
                   [r["url"] for r in target],
                   [f"{len(target):,} 件（{_status_breakdown(target)}）。ページには noindex が付いていて、"
                    "Google が次に取りに来たときに「noindex除外」に移る。",
                    "型: " + "、".join(f"{t}（{n}）" for t, n in tm.most_common(6))],
                   ["対応不要。ただし nofollow も付いているページは、そこから先のリンクが辿られない点に注意。"],
                   "ステータスが「noindex除外」に移っていくこと。", owner="監視のみ",
                   templates=[t for t, _ in tm.most_common(6)])]


def _canonical_defect(url: str, c: dict) -> str:
    """canonical の不備の種類。問題なければ空文字。"""
    from urllib.parse import urlparse
    canon = c.get("canonical", "")
    if not canon:
        return "canonical タグが無い"
    if urlparse(canon).query:
        return "canonical にクエリ（?〜）が残っている"
    if not clean_canonical(canon):
        return "canonical に並び替え（direction: / sort:）が残っている"
    return ""


def rule_param_duplicates(recs, checks, facts, claimed):
    """パラメータ付きURLの重複。canonical の不備はテンプレート単位で直すので、型ごとにまとめる。"""
    # 対象は「並び順・流入元だけが違うURL」。country_id: / area_id: / page: だけのURLは
    # 中身が違う別ページなので、ここでは扱わない（クロール待ち・評価落ちのルールが担当）。
    def _elsewhere(r):
        c = checks.get(r["url"], {})
        return (not r["in_sitemap"] and clean_canonical(c.get("canonical", ""))
                and not same_url(c["canonical"], r["url"]))

    rows = [r for r in recs if r["url"] not in claimed and r["problem"] and
            (query_keys(r["url"]) or set(named_params(r["url"])) & SORT_NAMED or _elsewhere(r))]
    ng, ok = defaultdict(list), []
    for r in rows:
        c = checks.get(r["url"], {})
        # HTML として読めたページだけで判定する（PDF等は canonical を持たない。未取得は判定しない）
        if not c or c.get("error") or not (c.get("title") or c.get("canonical")):
            continue
        d = _canonical_defect(r["url"], c)
        if d:
            ng[(r["template"].split("?")[0], d)].append((r, c))
        else:
            ok.append(r)
    out = []
    if ng:
        urls = [r["url"] for v in ng.values() for r, _ in v]
        ev = [f"{len(urls):,} 件（すべて実ページを取得して確認）。country_id: / area_id: / page: のように"
              "中身が変わるパラメータは canonical に残っていて正しいので、不備に数えていない。"]
        fixes = []
        for (t, d), v in sorted(ng.items(), key=lambda x: -len(x[1]))[:8]:
            keys = Counter(k for r, _ in v for k in query_keys(r["url"]))
            via = "、".join(f"?{k}=" for k, _ in keys.most_common(3))
            ex_c = v[0][1].get("canonical") or "（無し）"
            ev.append(f"{t}: {len(v):,} 件 — {d}" + (f"（流入パラメータ {via}）" if via else "") +
                      f"。例: {v[0][0]['url'][:90]} → {ex_c}")
        tmpl_none = sorted({t for (t, d) in ng if d == "canonical タグが無い"})
        tmpl_q = sorted({t for (t, d) in ng if "クエリ" in d})
        tmpl_s = sorted({t for (t, d) in ng if "並び替え" in d})
        if tmpl_none:
            fixes.append(f"canonical を出していないテンプレート（{', '.join(tmpl_none[:5])}）に、クエリを除いた自己参照 canonical を追加する。")
        if tmpl_q:
            fixes.append(f"canonical にクエリが入るテンプレート（{', '.join(tmpl_q[:5])}）は、クエリを除いて出力する。")
        if tmpl_s:
            fixes.append(f"canonical に並び替えが入るテンプレート（{', '.join(tmpl_s[:5])}）は、direction: / sort: を除いて出力する。")
        out.append(_issue("param_canonical_missing", "ng", 1 if len(urls) >= 50 else 2,
                          "パラメータ付きURLの canonical に不備がある（タグが無い・パラメータが残る）",
                          urls, ev, fixes,
                          "取得し直したときに canonical が直っていること（このツールが14日以内に再取得）。",
                          templates=[t for (t, _d) in sorted(ng, key=lambda k: -len(ng[k]))][:8]))
        claimed.update(urls)
    if ok:
        sig = Counter()
        for r in ok:
            qs = query_keys(r["url"])
            nps = sorted(set(named_params(r["url"])))
            sig["?" + ",".join(qs) if qs else ("/" + ",".join(f"{k}:" for k in nps) if nps else r["template"])] += 1
        ev = [f"{len(ok):,} 件。canonical はクエリ・並び替えを除いたURLを正しく指している（実ページを取得して確認）。"
              "Google が重複として整理している途中で、ページ側の対処は済んでいる。"]
        for k, n in sig.most_common(6):
            tag = "（外部流入のパラメータ）" if k.startswith("?") and any(x in TRACKING_KEYS for x in k[1:].split(",")) else ""
            ev.append(f"{k}: {n:,} 件{tag}")
        out.append(_issue("param_duplicates_ok", "ok", 3,
                          "並び替え・流入パラメータ違いの重複URL（canonical は設定済み・監視のみ）",
                          [r["url"] for r in ok], ev,
                          ["ページ側の追加対応は不要。件数が増え続ける場合だけ、サイト内リンクにパラメータを付けている箇所を探して外す。"],
                          "件数が減っていくこと（Google が再クロールして整理するまで数週間かかる）。",
                          owner="監視のみ", templates=sorted({r["template"] for r in ok})[:8]))
        claimed.update(r["url"] for r in ok)
    return out


BACKLOG_SOLO = 200      # この件数以上クロール待ちがある型は単独のカードにする
CNI_SOLO = 50           # 同じく「クロール済み・未登録」


def _backlog_fix():
    return ["サイトマップに正確な <lastmod>（内容が実際に変わった日時）を出力し、新規・更新ページを優先して取りに来させる。",
            "親ページ（学校詳細・エリア一覧など）から、未登録のページへのリンクを増やしてクロールの入口を作る。",
            "内容の薄いページ（短い投稿など）が多い型なら、一定基準未満を noindex にして全体の評価を上げる。"]


def rule_discovery_backlog(recs, checks, facts, claimed):
    """検出・未登録／Google未認識で、ページ側に不備が無いもの（＝クロールの優先度の問題）。"""
    lastmod = {s["name"].replace(".xml", ""): s["lastmod"] for s in facts.get("sitemaps", [])}
    solo, rest = [], []
    for t, rows in _by_template([r for r in recs if r["in_sitemap"]]).items():
        waiting = [r for r in rows if r["status"] in WAITING_STATUSES and r["url"] not in claimed]
        if not waiting:
            continue
        cks = _checked(waiting, checks)
        if cks:
            waiting = [r for r, c in cks if _indexable(c, r["url"])]  # 取得して不備が無かったものだけ
        else:
            continue  # 未取得の型は判定しない（次回の点検で拾う）
        if not waiting:
            continue
        (solo if len(waiting) >= BACKLOG_SOLO else rest).append((t, rows, waiting))
    out = []
    for t, rows, waiting in solo:
        judged = [r for r in rows if r["status"] != "unchecked"]
        indexed = sum(1 for r in judged if r["status"] == "indexed")
        never = sum(1 for r in waiting if not r["last_crawl"])
        lens = [int(checks[r["url"]].get("text_len") or 0) for r in waiting]
        ev = [f"{t}: サイトマップ掲載 {len(rows):,} 件のうち、登録済み {indexed:,} 件・未登録で待機 {len(waiting):,} 件"
              f"（{_status_breakdown(waiting)}）。",
              f"待機中のページを全件取得し、すべて HTTP 200・自己参照canonical・noindex無し"
              f"（本文 {min(lens):,}〜{max(lens):,} 字）。ページの設定に不備は見当たらない。",
              f"待機中のうち、Googleが一度もクロールしていないURL {never:,} 件。"
              "＝Googleが存在は知っているが、取りに来る優先度を低くしている状態。"]
        no_lm = [s for s in sorted({r["sitemap"] for r in rows if r["sitemap"]}) if lastmod.get(s) == 0]
        if no_lm:
            ev.append(f"このURLを載せているサイトマップ（{', '.join(s + '.xml' for s in no_lm)}）に lastmod が1件も無い。")
        claimed.update(r["url"] for r in waiting)
        out.append(_issue("discovery_backlog", t, 2,
                          f"{t} の {len(waiting):,} 件がクロール待ちのまま（ページに不備は無い）",
                          [r["url"] for r in waiting], ev, _backlog_fix(),
                          "待機件数の推移（このカードの件数）と、型の登録済み率。",
                          owner="エンジニア・SEO", templates=[t]))
    if rest:
        urls = [r["url"] for _, _, w in rest for r in w]
        rest.sort(key=lambda x: -len(x[2]))
        ev = [f"{len(rest)} 個の型にまたがる {len(urls):,} 件（1型あたり {BACKLOG_SOLO} 件未満）。"
              "いずれも実ページを取得し、HTTP 200・自己参照canonical・noindex無しを確認済み。",
              "型: " + "、".join(f"{t}（{len(w)}）" for t, _, w in rest[:10])]
        claimed.update(urls)
        out.append(_issue("discovery_backlog_rest", "rest", 3,
                          f"その他の型の {len(urls):,} 件がクロール待ち（ページに不備は無い）",
                          urls, ev, _backlog_fix(), "待機件数の推移。", owner="エンジニア・SEO",
                          templates=[t for t, _, _ in rest[:10]]))
    return out


def rule_crawled_not_indexed(recs, checks, facts, claimed):
    """クロール済み・未登録で、ページ側に技術的な不備が無いもの（＝内容の評価で落ちている）。"""
    solo, rest = [], []
    for t, rows in _by_template(recs).items():
        cni = [r for r in rows if r["status"] == "crawled_not_indexed" and r["url"] not in claimed]
        cks = [(r, c) for r, c in _checked(cni, checks) if _indexable(c, r["url"])]
        if not cks:
            continue
        (solo if len(cks) >= CNI_SOLO else rest).append((t, rows, cks))
    fix = ["同じ型の登録済みページと比べて、独自の情報（本文・写真・具体的な数字）が足りないものを補う。",
           "ほぼ同じ内容のページが並んでいるなら統合する。価値の低いものは noindex にする。"]
    out = []
    for t, rows, cks in solo:
        ic = [c for _, c in _checked([r for r in rows if r["status"] == "indexed"], checks)]
        ev = [f"{t}: クロール済み・未登録 {len(cks):,} 件。Google はページを読んだうえで、登録する価値が低いと判断している。"
              "実ページを全件取得し、技術的な不備（noindex・canonical・エラー）は無い。"]
        if ic:
            a = statistics.median(int(c.get("text_len") or 0) for _, c in cks)
            b = statistics.median(int(c.get("text_len") or 0) for c in ic)
            ev.append(f"本文の文字数（中央値）: 未登録 {a:,.0f} 字／同じ型の登録済み {b:,.0f} 字（登録済みは {len(ic)} 件取得）。"
                      + ("文字数に大きな差は無い＝量ではなく中身（独自性・重複）で落ちている可能性が高い。" if b and abs(a - b) / b < 0.2 else ""))
        claimed.update(r["url"] for r, _ in cks)
        out.append(_issue("crawled_not_indexed", t, 3,
                          f"{t} の {len(cks):,} 件が「クロール済み・未登録」（内容の評価で落ちている）",
                          [r["url"] for r, _ in cks], ev, fix, "型の登録済み率の推移。",
                          owner="SEO・コンテンツ", templates=[t]))
    if rest:
        urls = [r["url"] for _, _, c in rest for r, _ in c]
        rest.sort(key=lambda x: -len(x[2]))
        claimed.update(urls)
        out.append(_issue("crawled_not_indexed_rest", "rest", 3,
                          f"その他の型の {len(urls):,} 件が「クロール済み・未登録」",
                          urls,
                          [f"{len(rest)} 個の型にまたがる {len(urls):,} 件。実ページを取得し、技術的な不備は無い。",
                           "型: " + "、".join(f"{t}（{len(c)}）" for t, _, c in rest[:10])],
                          fix, "件数の推移。", owner="SEO・コンテンツ", templates=[t for t, _, _ in rest[:10]]))
    return out


def rule_gone_offsitemap(recs, checks, facts, claimed):
    """サイトマップ外で、もう存在しない・転送されるURL。Googleの記録が古いだけなので監視のみ。"""
    rows = []
    for r in recs:
        c = checks.get(r["url"], {})
        if r["url"] in claimed or not r["problem"] or r["in_sitemap"] or not c or c.get("error"):
            continue
        if c.get("http_status") != "200" or c.get("final_url"):
            rows.append((r, c))
    if not rows:
        return []
    claimed.update(r["url"] for r, _ in rows)
    hs = Counter(c.get("http_status") for _, c in rows)
    return [_issue("gone_offsitemap", "all", 3, "既に削除・転送済みのURL（Googleの記録が古いだけ・監視のみ）",
                   [r["url"] for r, _ in rows],
                   [f"{len(rows):,} 件。サイトマップには載っておらず、取得すると " +
                    "、".join(f"HTTP {k} が {v} 件" for k, v in hs.most_common()) + "。"],
                   ["対応不要。転送先が適切か（関係の無いページに飛ばしていないか）だけ確認する。"],
                   "件数が減っていくこと。", owner="監視のみ",
                   templates=sorted({r["template"] for r, _ in rows})[:8])]


def rule_sitemap_no_lastmod(recs, checks, facts, claimed):
    sms = [s for s in facts.get("sitemaps", []) if s["urls"]]
    none = [s for s in sms if s["lastmod"] == 0]
    if not sms or len(none) < len(sms) * 0.5:
        return []
    total = sum(s["urls"] for s in none)
    return [_issue("sitemap_no_lastmod", "all", 3, "サイトマップに更新日時（lastmod）が出ていない",
                   [s["url"] for s in none],
                   [f"子サイトマップ {len(sms)} 本中 {len(none)} 本（掲載 {total:,} URL）に <lastmod> が1件も無い。",
                    "Google は lastmod を「どのページを先に取りに行くか」の手がかりに使う。無いと新規・更新ページの発見が遅れる。"],
                   ["サイトマップ生成時に、各URLの内容が最後に変わった日時を <lastmod> に出力する（生成日時を一律に入れるのは逆効果）。"],
                   "全サイトマップに lastmod が出ていること（このツールが毎日確認する）。",
                   examples=[s["url"] for s in none])]


RULES = [
    rule_intentional_noindex,
    rule_param_only_sitemaps,
    rule_crawl_trap,
    rule_utility_pages,
    rule_sitemap_gone,
    rule_sitemap_noindex,
    rule_sitemap_noncanonical,
    rule_canonical_conflict,
    rule_pagination_canonical,
    rule_already_noindex,
    rule_param_duplicates,
    rule_discovery_backlog,
    rule_crawled_not_indexed,
    rule_gone_offsitemap,
    rule_sitemap_no_lastmod,
]


def diagnose(recs: list, checks: dict, facts: dict) -> tuple:
    claimed = set()
    issues = []
    for rule in RULES:
        issues.extend(rule(recs, checks, facts, claimed))
    issues.sort(key=lambda i: (i["severity"], -i["count"]))
    unexplained = [r for r in recs if r["problem"] and r["url"] not in claimed]
    return issues, unexplained


# ---------------------------------------------------------------- 集計・差分

def template_counts(recs: list) -> dict:
    """型 × 問題ステータス の件数（問題が1件以上ある型だけ）。"""
    out = defaultdict(Counter)
    for r in recs:
        if r["problem"]:
            out[r["template"]][r["status"]] += 1
    return {t: dict(c) for t, c in out.items()}


def load_snapshot(d: date) -> dict:
    fp = ISSUES_DIR / f"{d.isoformat()}.json"
    if not fp.exists():
        return {}
    with fp.open(encoding="utf-8") as f:
        return json.load(f)


def snapshot_dates() -> list:
    if not ISSUES_DIR.exists():
        return []
    out = []
    for fp in ISSUES_DIR.glob("*.json"):
        try:
            out.append(date.fromisoformat(fp.stem))
        except ValueError:
            continue
    return sorted(out)


def _pick_base(run_date: date, days: int):
    """run_date の days 日前以前で最も新しいスナップショット。"""
    cands = [d for d in snapshot_dates() if d <= run_date - timedelta(days=days)]
    return load_snapshot(cands[-1]) if cands else {}


def diff_issues(cur: list, base: dict) -> dict:
    if not base:
        return {"base_date": None, "new": [], "resolved": [], "delta": {}}
    old = {i["id"]: i for i in base.get("issues", [])}
    now = {i["id"]: i for i in cur}
    return {
        "base_date": base.get("date"),
        "new": [i["id"] for i in cur if i["id"] not in old],
        "resolved": [{"id": k, "title": v["title"], "count": v["count"]} for k, v in old.items() if k not in now],
        "delta": {k: now[k]["count"] - old[k]["count"] for k in now if k in old},
    }


def diff_templates(cur: dict, base: dict) -> list:
    if not base:
        return []
    old = base.get("template_counts", {})
    rows = []
    for t in set(cur) | set(old):
        a, b = sum(old.get(t, {}).values()), sum(cur.get(t, {}).values())
        if a != b:
            rows.append({"template": t, "before": a, "after": b, "delta": b - a})
    rows.sort(key=lambda r: -abs(r["delta"]))
    return rows


# 「Google未認識」「その他」は良くも悪くもない状態なので、ここへの出入りは悪化・改善に数えない。
_NEUTRAL = {"unknown_to_google", "other", "unchecked"}


def transitions_by_template(run_date: date, days: int = 7) -> dict:
    """直近 days 日の changes/*.json から、型ごとの悪化・改善件数。

    悪化 = 問題ではなかった状態（登録済み等）→ 問題ステータス
    改善 = 問題ステータス → 登録済み・noindex除外・代替ページ等（＝Googleが結論を出した）
    「Google未認識 → 検出・未登録」は Google がページを見つけた前進なので悪化にせず、「新たに発見」として別に数える。
    未照会からの初回観測は遷移に数えない（PR以前のファイルには混ざっているので、ここで除く）。
    """
    worse, better, found = Counter(), Counter(), Counter()
    worse_urls = defaultdict(list)
    start = run_date - timedelta(days=days - 1)
    for d in store.list_change_dates():
        if not (start <= d <= run_date):
            continue
        for c in store.load_changes(d).get("changes", []):
            f, to = c["from"], c["to"]
            if f == "unchecked":
                continue
            t = template(c["url"])
            fp, tp = f in PROBLEM_STATUSES, to in PROBLEM_STATUSES
            if tp and f in _NEUTRAL:
                found[t] += 1
            elif tp and not fp:
                worse[t] += 1
                worse_urls[t].append({"url": c["url"], "from": short(f), "to": short(to), "date": d.isoformat()})
            elif fp and not tp and to not in _NEUTRAL:
                better[t] += 1
    keys = set(worse) | set(better) | set(found)
    rows = [{"template": t, "worse": worse.get(t, 0), "better": better.get(t, 0), "found": found.get(t, 0),
             "net": worse.get(t, 0) - better.get(t, 0),
             "examples": worse_urls.get(t, [])[:3]} for t in keys]
    rows.sort(key=lambda r: (-r["net"], -r["worse"], -r["found"]))
    return {"start": start.isoformat(), "end": run_date.isoformat(), "rows": rows,
            "worse_total": sum(worse.values()), "better_total": sum(better.values()),
            "found_total": sum(found.values())}


# ---------------------------------------------------------------- 出力

def write_outputs(snap: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "issues.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["issue_id", "優先度", "改修案", "url"])
        for i in snap["issues"]:
            for u in i["urls"]:
                w.writerow([i["id"], i["severity_label"], i["title"], u])
        for u in snap["unexplained_urls"]:
            w.writerow(["unexplained", "-", "原因未特定", u])
    (OUT_DIR / "fixes.md").write_text(render_markdown(snap), encoding="utf-8")


def render_markdown(snap: dict) -> str:
    """エンジニアへそのまま渡せる改修チケット。"""
    d = snap["diff_prev"]
    lines = [f"# schoolwith.me インデックス改修案（{snap['date']}）", "",
             f"問題URL {snap['problem_urls']:,} 件のうち、改修案で説明できたもの "
             f"{snap['problem_urls'] - len(snap['unexplained_urls']):,} 件／原因未特定 {len(snap['unexplained_urls']):,} 件。", ""]
    if d.get("base_date"):
        lines.append(f"前回（{d['base_date']}）から：新規 {len(d['new'])} 件・解消 {len(d['resolved'])} 件。")
        for r in d["resolved"]:
            lines.append(f"- 解消: {r['title']}（前回 {r['count']:,} 件）")
        lines.append("")
    for n, i in enumerate(snap["issues"], 1):
        delta = d.get("delta", {}).get(i["id"])
        tag = "【新規】" if i["id"] in d.get("new", []) and d.get("base_date") else ""
        lines += [f"## {n}. {tag}[優先度 {i['severity_label']}] {i['title']}", "",
                  f"- 件数: {i['count']:,}" + (f"（前回比 {delta:+,}）" if delta else ""),
                  f"- 担当: {i['owner']}"]
        if i["templates"]:
            lines.append("- 型: " + "、".join(f"`{t}`" for t in i["templates"]))
        lines += ["", "**根拠**", ""] + [f"- {e}" for e in i["evidence"]]
        lines += ["", "**対処**", ""] + [f"{k}. {f}" for k, f in enumerate(i["fix"], 1)]
        lines += ["", f"**完了の確認**: {i['verify']}", "", "**例**", ""] + [f"- {u}" for u in i["examples"]] + [""]
    if snap["unexplained_top"]:
        lines += ["## 原因未特定の問題URL（型別）", "",
                  "どの改修案にも当てはまらなかったもの。ルールを追加する候補。", ""]
        lines += [f"- `{t}`: {n:,} 件" for t, n in snap["unexplained_top"]]
    return "\n".join(lines) + "\n"


def run(run_date=None, budget: int = pagecheck.DEFAULT_BUDGET, fetch: bool = True) -> dict:
    run_date = run_date or date.today()
    state = store.load_state()
    recs = build_records(state, gsc_export.load_urls())
    if fetch:
        facts = pagecheck.collect_site_facts() or pagecheck.load_site_facts()
        checks = pagecheck.refresh(recs, budget=budget)
    else:
        facts, checks = pagecheck.load_site_facts(), pagecheck.load_checks()

    issues, unexplained = diagnose(recs, checks, facts)
    tc = template_counts(recs)
    prev = {}
    for d in reversed(snapshot_dates()):
        if d < run_date:
            prev = load_snapshot(d)
            break
    week = _pick_base(run_date, 7)

    snap = {
        "date": run_date.isoformat(),
        "problem_urls": sum(1 for r in recs if r["problem"]),
        "problem_by_status": dict(Counter(r["status"] for r in recs if r["problem"])),
        "status_from_export": sum(1 for r in recs if r["problem"] and r["status_src"] == "gsc_export"),
        "issues": issues,
        "unexplained_urls": sorted(r["url"] for r in unexplained),
        "unexplained_top": Counter(r["template"] for r in unexplained).most_common(15),
        "template_counts": tc,
        "diff_prev": diff_issues(issues, prev),
        "diff_week": diff_issues(issues, week),
        "template_diff_week": diff_templates(tc, week)[:30],
        "transitions": transitions_by_template(run_date),
        "checks_total": len(checks),
        "site_facts_at": facts.get("collected_at"),
    }
    ISSUES_DIR.mkdir(parents=True, exist_ok=True)
    # スナップショットにはURL全件を入れない（件数とIDで差分が取れれば十分。全件は issues.csv）
    slim = dict(snap, issues=[{k: v for k, v in i.items() if k != "urls"} for i in issues],
                unexplained_urls=[])
    slim["unexplained_count"] = len(unexplained)
    with (ISSUES_DIR / f"{run_date.isoformat()}.json").open("w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False, indent=1)
    write_outputs(snap)
    logger.info(f"改修案 {len(issues)} 件・原因未特定 {len(unexplained):,} 件（問題URL {snap['problem_urls']:,}）")
    return snap


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="エラーの集計・差分・改修案を生成")
    p.add_argument("--budget", type=int, default=pagecheck.DEFAULT_BUDGET, help="実ページ取得の上限件数")
    p.add_argument("--no-fetch", action="store_true", help="サイトを取りに行かず、保存済みの点検結果だけで作る")
    p.add_argument("--date", type=str, help="記録日 (YYYY-MM-DD)")
    a = p.parse_args()
    run(date.fromisoformat(a.date) if a.date else None, budget=a.budget, fetch=not a.no_fetch)


if __name__ == "__main__":
    main()

"""URL Inspection API の coverageState を安定キーに正規化する。

API は languageCode に従って coverageState をローカライズして返すため、日本語と
英語の両方を受ける。**未知の文字列は捨てずに "other" + 生文字列として残す**：
GSC は文言を予告なく変えるので、取りこぼしを黙って握りつぶすと件数だけが合わなくなる。
未知が出たら WARN ログに残し、`python -m src.index_status.status_map --unknown` で
確認してここに追記する運用。
"""
from __future__ import annotations

import logging
import unicodedata

logger = logging.getLogger(__name__)

# 表示順 = 深刻度順。ダッシュボードの系列順もこの順に従う。
STATUS_ORDER = [
    "indexed",
    "crawled_not_indexed",
    "discovered_not_indexed",
    "dup_no_user_canonical",
    "dup_google_chose_different",
    "dup_submitted_not_selected",
    "alternate_canonical",
    "excluded_noindex",
    "redirect",
    "blocked_robots",
    "not_found",
    "soft_404",
    "server_error",
    "unknown_to_google",
    "other",
    "unchecked",
]

STATUS_LABEL_JA = {
    "indexed": "インデックス登録済み",
    "crawled_not_indexed": "クロール済み - インデックス未登録",
    "discovered_not_indexed": "検出 - インデックス未登録",
    "dup_no_user_canonical": "重複・ユーザーにより正規ページとして選択されていない",
    "dup_google_chose_different": "重複・Googleが別ページを正規に選択",
    "dup_submitted_not_selected": "重複・送信URLが正規URLとして選択されていない",
    "alternate_canonical": "代替ページ（適切なcanonicalあり）",
    "excluded_noindex": "noindexタグにより除外",
    "redirect": "リダイレクトあり",
    "blocked_robots": "robots.txtによりブロック",
    "not_found": "見つかりません（404）",
    "soft_404": "ソフト404",
    "server_error": "サーバーエラー（5xx）",
    "unknown_to_google": "Googleに認識されていない",
    "other": "その他・未分類",
    "unchecked": "未照会",
}

# 表・カードなど横幅が限られる場所用の短縮名。正式な GSC 文言は STATUS_LABEL_JA 側。
# 画面には用語集を出し、短縮名と GSC の原文が1対1で対応することを明示する。
STATUS_LABEL_SHORT = {
    "indexed": "登録済み",
    "crawled_not_indexed": "クロール済み・未登録",
    "discovered_not_indexed": "検出・未登録",
    "dup_no_user_canonical": "重複・正規未指定",
    "dup_google_chose_different": "重複・Google別選択",
    "dup_submitted_not_selected": "重複・送信URL不採用",
    "alternate_canonical": "代替ページ",
    "excluded_noindex": "noindex除外",
    "redirect": "リダイレクト",
    "blocked_robots": "robotsブロック",
    "not_found": "404",
    "soft_404": "ソフト404",
    "server_error": "5xx",
    "unknown_to_google": "Google未認識",
    "other": "その他",
    "unchecked": "未照会",
}

# 「対処が要る」= 本来インデックスさせたいのに入っていない状態。
# alternate_canonical / excluded_noindex / redirect は意図的な設計であることが多いので含めない。
PROBLEM_STATUSES = [
    "crawled_not_indexed",
    "discovered_not_indexed",
    "dup_no_user_canonical",
    "dup_google_chose_different",
    "dup_submitted_not_selected",
]

# (キー, 判定に使う部分文字列) — 上から順に評価するので、重複系を汎用語より先に置く。
# 部分一致にしているのは、GSC の文言が句読点や送り仮名で揺れても拾うため。
_PATTERNS = [
    ("dup_google_chose_different", [
        "ユーザーがマークしたページとは異なるページ",
        "googlechosedifferentcanonical",
        "googlechosedifferent",
    ]),
    ("dup_submitted_not_selected", [
        "送信されたurlが正規urlとして選択されていません",
        "送信されたurlが正規として選択されていません",
        "submittedurlnotselectedascanonical",
    ]),
    ("dup_no_user_canonical", [
        "ユーザーにより、正規ページとして選択されていません",
        "ユーザーにより正規ページとして選択されていません",
        "duplicatewithoutuser-selectedcanonical",
        "withoutuser-selectedcanonical",
        "withoutuserselectedcanonical",
    ]),
    ("alternate_canonical", [
        "適切なcanonicalタグを持つ代替ページ",
        "代替ページ",
        "alternatepagewithpropercanonicaltag",
    ]),
    ("crawled_not_indexed", [
        "クロール済み-インデックス未登録",
        "クロール済み‐インデックス未登録",
        "クロール済み—インデックス未登録",
        "crawled-currentlynotindexed",
        "crawledcurrentlynotindexed",
    ]),
    ("discovered_not_indexed", [
        "検出-インデックス未登録",
        "検出‐インデックス未登録",
        "検出—インデックス未登録",
        "discovered-currentlynotindexed",
        "discoveredcurrentlynotindexed",
    ]),
    ("excluded_noindex", [
        "noindex",
        "除外されました",
        "excludedby",
    ]),
    ("soft_404", ["ソフト404", "soft404"]),
    ("not_found", ["見つかりませんでした", "404", "notfound"]),
    ("blocked_robots", ["robots.txt", "blockedbyrobots"]),
    ("redirect", ["リダイレクト", "pagewithredirect"]),
    ("server_error", ["サーバーエラー", "servererror", "5xx"]),
    ("unknown_to_google", [
        "googleに認識されていません",
        "urlisunknowntogoogle",
        "unknowntogoogle",
    ]),
    # indexed は必ず最後に評価する。"登録されました" は広めの語なので、
    # 先行する否定系パターン（未登録・除外・重複）を全部すり抜けたものだけが到達する。
    # "送信して登録されました" は本番の history/2026-08-22.json で実際に確認した文言。
    ("indexed", [
        "送信して登録されました",
        "インデックスに登録済み",
        "インデックス登録済み",
        "索引に登録",
        "登録されました",
        "submittedandindexed",
        "indexed,notsubmittedinsitemap",
        "indexednotsubmitted",
    ]),
]

_unknown_seen: set = set()


def _norm(s: str) -> str:
    """全角/半角・空白・大小文字を吸収した比較用の文字列。"""
    s = unicodedata.normalize("NFKC", s)
    return "".join(s.split()).lower()


def normalize(coverage_state, verdict=None, indexing_state=None) -> str:
    """coverageState を STATUS_ORDER のキーへ。判定できなければ "other"。

    coverageState が空のときだけ indexing_state / verdict にフォールバックする。
    coverageState がある限りそちらを正とする（GSC のページレポートと語彙が揃うため）。
    """
    if coverage_state:
        n = _norm(coverage_state)
        for key, pats in _PATTERNS:
            if any(p in n for p in pats):
                return key
        if coverage_state not in _unknown_seen:
            _unknown_seen.add(coverage_state)
            logger.warning(f"未知の coverageState: {coverage_state!r} → other に分類")
        return "other"
    if indexing_state == "BLOCKED_BY_META_TAG":
        return "excluded_noindex"
    if indexing_state == "BLOCKED_BY_ROBOTS_TXT":
        return "blocked_robots"
    if verdict == "PASS":
        return "indexed"
    return "other"


def label(key: str) -> str:
    """GSC の画面文言に合わせた正式名。"""
    return STATUS_LABEL_JA.get(key, key)


def short(key: str) -> str:
    """表やカード用の短縮名。"""
    return STATUS_LABEL_SHORT.get(key, label(key))


def unknown_states() -> list:
    return sorted(_unknown_seen)


# 本番の history/*.json とGSC画面で実際に確認できた文言。推測は入れない。
# 文言の揺れで分類が壊れると件数だけが静かにズレるので、変更時は必ずこれを通す。
_KNOWN = [
    ("送信して登録されました", "indexed"),
    ("noindex タグによって除外されました", "excluded_noindex"),
    ("クロール済み - インデックス未登録", "crawled_not_indexed"),
    ("検出 - インデックス未登録", "discovered_not_indexed"),
    ("重複しています。ユーザーにより、正規ページとして選択されていません",
     "dup_no_user_canonical"),
    ("重複しています。Google により、ユーザーがマークしたページとは異なるページが"
     "正規ページとして選択されました", "dup_google_chose_different"),
    ("Submitted and indexed", "indexed"),
    ("Crawled - currently not indexed", "crawled_not_indexed"),
    ("Discovered - currently not indexed", "discovered_not_indexed"),
    ("Duplicate without user-selected canonical", "dup_no_user_canonical"),
    ("Duplicate, Google chose different canonical than user", "dup_google_chose_different"),
    ("Duplicate, submitted URL not selected as canonical", "dup_submitted_not_selected"),
    ("Page with redirect", "redirect"),
    ("Blocked by robots.txt", "blocked_robots"),
]


def selftest() -> int:
    """既知の文言が期待どおりに分類されるか。戻り値は失敗件数。"""
    bad = 0
    for text, expect in _KNOWN:
        got = normalize(text)
        mark = "OK " if got == expect else "NG "
        if got != expect:
            bad += 1
        print(f"  {mark} {got:28s} (期待 {expect:28s}) <- {text[:44]}")
    print(f"\n{len(_KNOWN) - bad}/{len(_KNOWN)} 一致")
    return bad


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.ERROR)
    p = argparse.ArgumentParser(description="coverageState の分類チェック")
    p.add_argument("--text", help="任意の文字列を分類してみる")
    a = p.parse_args()
    if a.text:
        print(f"{normalize(a.text)}  ({label(normalize(a.text))})")
    else:
        raise SystemExit(1 if selftest() else 0)

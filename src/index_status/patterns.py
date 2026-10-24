"""URL を「型」にまとめる。

エラーはURL1本ずつ直すものではなく、テンプレート（ページの型）単位で直すもの。
`/reviews/35354` と `/reviews/41830` は同じ原因で同じ状態になっているので、
集計・差分・改修案はすべてこの型を単位にする。

型の作り方（すべて機械的。意味の推測はしない）
- 数字だけのパス要素 → `{id}`
- 大文字2文字のパス要素（国コード）→ `{cc}`
- `key:value` 形式のパス要素（CakePHP の名前付きパラメータ）→ `key:*`
- クエリは値を捨ててキー名だけ残す（`?a8=xxx&cid=yyy` → `?a8,cid`）
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse

_ID_RE = re.compile(r"^\d+$")
_CC_RE = re.compile(r"^[A-Z]{2}$")
_NAMED_RE = re.compile(r"^([A-Za-z_]+):.*$")

# 広告・アフィリエイト・SNS流入で外から付くパラメータ。値はユーザーごとに変わるので
# 1つのページから無数の重複URLが生まれる。ここに無いキーは「サイト内部で生成されたもの」として扱う。
TRACKING_KEYS = {
    "a8", "afid", "cid", "p", "fbclid", "gclid", "yclid", "msclkid", "src_user",
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
}


def template(url: str) -> str:
    p = urlparse(url)
    parts = []
    for seg in (p.path or "/").split("/"):
        if _ID_RE.match(seg):
            parts.append("{id}")
        elif _CC_RE.match(seg):
            parts.append("{cc}")
        else:
            m = _NAMED_RE.match(seg)
            parts.append(f"{m.group(1)}:*" if m else seg)
    path = "/".join(parts) or "/"
    keys = query_keys(url)
    return path + ("?" + ",".join(keys) if keys else "")


def query_keys(url: str) -> list:
    return sorted({k for k, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)})


def named_params(url: str) -> list:
    """パス中の `key:value` のキー名（direction / sort / page など）。"""
    out = []
    for seg in urlparse(url).path.split("/"):
        m = _NAMED_RE.match(seg)
        if m:
            out.append(m.group(1))
    return out


def has_params(url: str) -> bool:
    return bool(urlparse(url).query) or bool(named_params(url))


def nesting_depth(url: str) -> int:
    """パラメータの値に、同じパラメータ名が入れ子で埋め込まれている深さ。

    `?sort_type=/areas/...?sort_type%3D%252F...` のように、リンク生成が現在のURLを
    パラメータに丸ごと埋め込むと、辿るたびに1段ずつ深くなってURLが無限に増える。
    値の中に自分のキー名が何回現れるかで深さを測る。
    `?done=/schools/1`（ログイン後の戻り先）のように値がパスでも、自分のキー名を
    含まなければ入れ子ではないので 0。
    """
    depth = 0
    for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True):
        if len(k) >= 3:
            depth = max(depth, v.count(k))
    return depth


def trap_keys(url: str) -> list:
    """値に自分のキー名を含むパラメータ名（＝入れ子の発生源）。"""
    return sorted({k for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True)
                   if len(k) >= 3 and k in v})


# 並び順だけを変える名前付きパラメータ。中身は同じページなので canonical に残してはいけない。
# country_id / area_id / page などは中身が変わるパラメータなので、canonical に残っていて正しい。
SORT_NAMED = {"direction", "sort"}


def page_number(url: str) -> int:
    m = re.search(r"/page:(\d+)", urlparse(url).path)
    return int(m.group(1)) if m else 1


def clean_canonical(canonical: str) -> bool:
    """canonical に、クエリや並び替えパラメータが残っていないか。"""
    if not canonical:
        return False
    return not urlparse(canonical).query and not (set(named_params(canonical)) & SORT_NAMED)


def same_url(a: str, b: str) -> bool:
    """canonical の比較用。末尾スラッシュとホストの大小文字だけ吸収する。

    クエリは吸収しない：パラメータが canonical に残っているかどうかが判定の要だから。
    """
    if not a or not b:
        return False
    pa, pb = urlparse(a), urlparse(b)
    return (pa.scheme, pa.netloc.lower(), pa.path.rstrip("/") or "/", pa.query) == \
           (pb.scheme, pb.netloc.lower(), pb.path.rstrip("/") or "/", pb.query)


if __name__ == "__main__":
    cases = [
        ("https://schoolwith.me/reviews/35354", "/reviews/{id}"),
        ("https://schoolwith.me/countries/school/CA/page:2/sort:x/direction:asc?sort_type=/a",
         "/countries/school/{cc}/page:*/sort:*/direction:*?sort_type"),
        ("https://schoolwith.me/?afid=x&cid=y&p=z", "/?afid,cid,p"),
        ("https://schoolwith.me/", "/"),
    ]
    bad = 0
    for u, want in cases:
        got = template(u)
        print(("OK " if got == want else "NG ") + f"{got}  <- {u[:70]}")
        bad += got != want
    assert same_url("https://schoolwith.me", "https://schoolwith.me/")
    assert not same_url("https://schoolwith.me/?a8=1", "https://schoolwith.me/")
    assert nesting_depth("https://x/a?sort_type=/a/b?sort_type%3D%252Fa%252Fb%253Fsort_type%253D%25252Fa") >= 2
    assert nesting_depth("https://x/?a8=abc") == 0
    assert nesting_depth("https://schoolwith.me/users/?done=/schools/50071") == 0
    assert clean_canonical("https://x/category/a/country_id:3/page:2")
    assert not clean_canonical("https://x/provinces/1/direction:desc/page:2")
    assert page_number("https://x/areas/school/1/page:3/sort:a") == 3
    raise SystemExit(1 if bad else 0)

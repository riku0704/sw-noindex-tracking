"""Search Console の「ページ」レポートからエクスポートしたZIPを取り込む。

なぜ必要か
----------
URL Inspection API は「こちらが指定したURLしか答えない」。一方 GSC が問題視している
URLの大半はサイトマップに載っていない（実測: 73.8%）。とくに
「重複・ユーザーにより正規未選択」586件は **1件もサイトマップに無い**（?a8= 等の
パラメータ付きURL）。したがってサイトマップだけを追跡している限り、GSCの件数とは
永久に一致しないし、問題URLの実体も見えない。

GSC が知っているURL一覧を返すAPIは存在しないので、レポート画面からの手動
エクスポートを唯一の入口として取り込む。

ZIPの中身（実物で確認済み）
--------------------------
- `メタデータ.csv`               … プロパティ,値 / サイトマップ・問題（ステータス名）
- `表.csv`                       … URL,前回のクロール （最大1,000行。総数がこれを超える分は取れない）
- `平均読み込み時間のチャート.csv` … 日付,該当ページ （GSC公式の日次件数。名前は実物のまま）

ファイル名は日本語で、ZIPにUTF-8フラグが立っていないことがある。cp437で誤解釈された
名前を復号しないと取り出せない。
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import zipfile
from datetime import date, datetime
from pathlib import Path

from src.index_status.status_map import label, normalize

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "index_status"
EXPORT_DIR = DATA_DIR / "gsc_exports"
GSC_URLS_PATH = DATA_DIR / "gsc_urls.csv"
GSC_OFFICIAL_PATH = DATA_DIR / "gsc_official.csv"

TABLE_CSV = "表.csv"
META_CSV = "メタデータ.csv"
CHART_SUFFIX = "チャート.csv"   # 先頭の語はレポート種別で変わるので接尾辞で拾う

GSC_URL_FIELDS = ["url", "status_at_export", "data_date", "first_seen", "last_seen"]
GSC_OFFICIAL_FIELDS = ["data_date", "status", "pages", "imported_at"]


def _members(z: zipfile.ZipFile) -> dict:
    """ZIP内のファイル名を正しく復号して {名前: bytes} にする。"""
    out = {}
    for info in z.infolist():
        name = info.filename
        if not info.flag_bits & 0x800:
            # UTF-8フラグが無いので cp437 として読まれている。元のUTF-8に戻す。
            name = name.encode("cp437").decode("utf-8", "replace")
        out[Path(name).name] = z.read(info)
    return out


def _rows(blob: bytes) -> list:
    return list(csv.DictReader(io.StringIO(blob.decode("utf-8-sig"))))


def read_zip(path: Path) -> dict:
    """1つのエクスポートZIPを読む。中身が想定と違えば例外で止める（黙って空を返さない）。"""
    with zipfile.ZipFile(path) as z:
        m = _members(z)
    if META_CSV not in m or TABLE_CSV not in m:
        raise ValueError(f"{path.name}: {META_CSV} か {TABLE_CSV} が見つかりません（中身: {sorted(m)}）")

    issue = ""
    for line in m[META_CSV].decode("utf-8-sig").splitlines():
        if line.startswith("問題"):
            issue = line.split(",", 1)[1].strip()
    if not issue:
        raise ValueError(f"{path.name}: メタデータ.csv に「問題」行がありません")

    status = normalize(issue)
    if status == "other":
        logger.warning(f"{path.name}: 未知のステータス文言 {issue!r} → other として取り込みます")

    urls = [r["URL"] for r in _rows(m[TABLE_CSV]) if r.get("URL")]

    daily = []
    chart_key = next((k for k in m if k.endswith(CHART_SUFFIX)), None)
    if chart_key:
        for r in _rows(m[chart_key]):
            d, p = r.get("日付"), r.get("該当ページ")
            if d and p:
                daily.append((d, int(p)))
    else:
        logger.warning(f"{path.name}: チャートCSVが見当たらず、GSC公式の日次件数は取り込めません")

    data_date = daily[-1][0] if daily else date.today().isoformat()
    return {"issue": issue, "status": status, "urls": urls, "daily": daily,
            "data_date": data_date, "members": m}


def _load(path: Path, key_fields: list) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {tuple(r[k] for k in key_fields): r for r in csv.DictReader(f)}


def _save(path: Path, fields: list, rows: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])


def import_zips(paths: list, keep_raw: bool = True) -> dict:
    """ZIP群を取り込み、gsc_urls.csv と gsc_official.csv を更新する。"""
    now = datetime.now().isoformat(timespec="seconds")
    known = _load(GSC_URLS_PATH, ["url"])
    official = _load(GSC_OFFICIAL_PATH, ["data_date", "status"])
    added = 0
    per_status = {}

    for p in paths:
        res = read_zip(Path(p))
        st, dd = res["status"], res["data_date"]
        per_status[st] = len(res["urls"])
        logger.info(f"{Path(p).name}: {label(st)} / URL {len(res['urls']):,}件 / データ日 {dd}")

        if keep_raw:
            # 生CSVを日付つきで残す。取り込みを後から再現・検証できるようにするため。
            d = EXPORT_DIR / dd / st
            d.mkdir(parents=True, exist_ok=True)
            for name, blob in res["members"].items():
                (d / name).write_bytes(blob)

        for u in res["urls"]:
            row = known.get((u,))
            if row:
                row.update({"status_at_export": st, "data_date": dd, "last_seen": now})
            else:
                known[(u,)] = {"url": u, "status_at_export": st, "data_date": dd,
                               "first_seen": now, "last_seen": now}
                added += 1

        for d, pages in res["daily"]:
            official[(d, st)] = {"data_date": d, "status": st, "pages": pages, "imported_at": now}

    _save(GSC_URLS_PATH, GSC_URL_FIELDS, known)
    _save(GSC_OFFICIAL_PATH, GSC_OFFICIAL_FIELDS, official)
    logger.info(f"取り込み完了: URL 新規 {added:,} / 累計 {len(known):,}、公式日次 {len(official):,} 行")
    return {"added": added, "total": len(known), "per_status": per_status}


def load_urls() -> list:
    """取り込み済みのGSC指摘URL。(url, status_at_export) のリスト。"""
    if not GSC_URLS_PATH.exists():
        return []
    with GSC_URLS_PATH.open(encoding="utf-8", newline="") as f:
        return [(r["url"], r["status_at_export"]) for r in csv.DictReader(f)]


def load_official() -> list:
    """GSC公式の日次件数。"""
    if not GSC_OFFICIAL_PATH.exists():
        return []
    with GSC_OFFICIAL_PATH.open(encoding="utf-8", newline="") as f:
        return sorted(csv.DictReader(f), key=lambda r: (r["data_date"], r["status"]))


def latest_official() -> dict:
    """ステータス→最新のGSC公式件数。"""
    out = {}
    for r in load_official():
        out[r["status"]] = {"pages": int(r["pages"]), "data_date": r["data_date"]}
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(
        description="GSCの『ページ』レポートからエクスポートしたZIPを取り込む",
        epilog="例: python -m src.index_status.gsc_export ~/Downloads/*Coverage-Drilldown*.zip")
    p.add_argument("zips", nargs="+", help="エクスポートZIPのパス")
    p.add_argument("--no-keep-raw", action="store_true", help="生CSVをリポジトリに残さない")
    a = p.parse_args()
    import_zips(a.zips, keep_raw=not a.no_keep_raw)


if __name__ == "__main__":
    main()

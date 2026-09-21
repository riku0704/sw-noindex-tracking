# SW NOINDEX Tracking

Phase-1 NOINDEX 対象114URL（国9 + 都市47 + 学校58）の GSC パフォーマンスと Google クロール状況を週次で追跡し、GitHub Pages に公開するダッシュボード。

## 生成されるダッシュボード

- **📊 総合サマリ** — 国/都市/学校の週次 Clicks/Impr/KW数の推移
- **📈 純NOINDEX効果** — 前年同期比較で季節性を除外した純粋な NOINDEX 影響度
- **🕷 クロール状況** — 各URLの最終Googleクロール日 + 前回との差分
- **⚠️ 問題整理** — Clicks喪失/大幅Impr減/順位悪化 KW一覧
- **🔬 KW別内訳** — 消失KW / 新規KW / 継続KWの分解

## セットアップ (5ステップ)

### 1. リポジトリを Git 管理下に

```bash
cd "/Users/mashiro/Documents/書類 - Unknown/Claude/Projects/SW/noindex-tracking-repo"
git init
git add .
git commit -m "Initial commit"

# GitHub にリポジトリ作成後
git remote add origin https://github.com/<your-org>/sw-noindex-tracking.git
git branch -M main
git push -u origin main
```

### 2. GCP でサービスアカウント + GSC 権限付与

`setup_credentials.md` を参照。手順:

1. GCP プロジェクト作成
2. **Google Search Console API** と **Search Console API** を有効化
3. サービスアカウント作成 → JSONキーダウンロード
4. Search Console でサービスアカウントメールを **所有者権限で追加**
5. JSONキーの中身を GitHub Secrets に `GSC_SERVICE_ACCOUNT_JSON` として登録

### 3. GitHub Secrets 設定

Repository → Settings → Secrets and variables → Actions:

| Secret 名 | 値 |
|---|---|
| `GSC_SERVICE_ACCOUNT_JSON` | サービスアカウントJSONキー全文 |
| `GSC_SITE_URL` | `https://schoolwith.me/` |

### 4. GitHub Pages 有効化

Repository → Settings → Pages → Source: **Deploy from a branch** → Branch: `main`, Folder: `/docs` → Save

数分後、下記URLで公開されます:
```
https://<your-org>.github.io/sw-noindex-tracking/
```

### 5. 初回手動実行

```bash
# ローカルで動作確認
pip install -r requirements.txt
export GSC_SERVICE_ACCOUNT_JSON=$(cat path/to/key.json)
export GSC_SITE_URL="https://schoolwith.me/"
python -m src.main --backfill  # 過去2週間分を取得

git add docs/ history/
git commit -m "Initial dashboard generation"
git push
```

## 自動更新

GitHub Actions が毎週 **月曜 AM6:00 JST** に実行し、
- Windsor.ai 経由で GSC データ取得（前後比較データ更新）
- GSC URL Inspection API で114URLの最終クロール日取得
- `docs/index.html` を再生成
- `history/YYYY-MM-DD.json` にスナップショット追加
- 自動 commit & push → GitHub Pages 更新

## ディレクトリ構成

```
sw-noindex-tracking/
├── README.md
├── requirements.txt
├── targets.json                # 114URLリスト (国/都市/学校)
├── setup_credentials.md         # GCPサービスアカウント設定手順
├── CLAUDE.md                    # Claude Code用のコンテキスト
├── src/
│   ├── main.py                  # オーケストレーター (エントリポイント)
│   ├── fetch_gsc.py             # Search Analytics API 呼び出し
│   ├── fetch_crawl_status.py    # URL Inspection API 呼び出し
│   ├── generate_html.py         # HTML 生成
│   └── history_store.py         # スナップショット永続化
├── templates/
│   └── dashboard.html.j2        # Jinja2 ダッシュボードテンプレート
├── history/                     # 週次スナップショット JSON
│   └── YYYY-MM-DD.json
├── docs/                        # GitHub Pages 公開先
│   ├── index.html
│   └── data.json
└── .github/workflows/
    └── weekly.yml               # 毎週月曜自動実行
```

## 手動追加運用

新しいNOINDEX対象URLを追加する場合:

1. `targets.json` を編集
2. `python -m src.main --force-full` でリセット再取得
3. commit & push

## ローカル開発

```bash
python -m src.main --dry-run       # データ取得のみ、HTML生成なし
python -m src.main --html-only     # 既存データから HTML再生成
python -m src.main --inspect-only  # URL Inspection のみ (クロール日更新)
```

## トラブルシューティング

**URL Inspection API がエラー**
→ GSC のサービスアカウント権限を確認。`所有者`権限が必要（`フルユーザー`では不可）

**GitHub Actions がタイムアウト**
→ URL Inspection API は実測で各リクエスト約7秒。161URL（都市は /areas/{id} と /areas/school/{id} の2形式）で約19分（timeout は45分）。クォータは日次2,000件なので無料枠内。

**GitHub Pagesが更新されない**
→ `docs/index.html` が commit されているか確認。`.gitignore` に含まれていないこと。

---

# インデックス状態 日次トラッカー（index_status）

サイト全体のインデックス状態を **日次** で追跡し、「昨日と比べてどのページのステータスが変わったか」を出す。
上の週次NOINDEXダッシュボードとは**データもHTMLも独立**しており、共有しているのは
サービスアカウント認証（`GSC_SERVICE_ACCOUNT_JSON` / `GSC_SITE_URL`）と URL Inspection クライアントだけ。

公開先: `https://riku0704.github.io/sw-noindex-tracking/index-status/`

## 追跡するステータス

Search Console の文言でいう次の4つが主対象（グラフとカードはこれを描く）:

- クロール済み - インデックス未登録
- 検出 - インデックス未登録
- 重複しています。ユーザーにより、正規ページとして選択されていません
- 重複しています。Google により、ユーザーがマークしたページとは異なるページが正規ページとして選択されました

加えて「重複・送信URLが正規URLとして選択されていない」も同じ扱いで追う。
その他（登録済み / 代替ページ / noindex / リダイレクト / 404 など）も件数としては保持する。

## 仕組みと制約

**インデックス カバレッジ（ページ）レポートそのものに API は無い。** 自動取得できるのは
URL Inspection API（1URLずつ照会）だけなので、sitemap 由来のURLリストを日々叩いて差分を取る方式。

| 項目 | 値 |
|---|---|
| 追跡URL数 | 約16,000（sitemap 約14,100 ＋ GSCが問題視したURL 約1,900） |
| APIクォータ | **2,000 URL / 日 / プロパティ**（600/分） |
| 1日の照会数 | 既定 1,800（`--budget`、上限2,000） |
| 一巡にかかる日数 | **約9日** |
| 所要時間 | 10並列で約21分 |

この設計から来る読み方の注意:

- ステータス別の件数は「全URLの**最新既知値**の合算」で、最大8日前の値を含む。GSCのページレポートの数字とは一致しない。
- 「本日の遷移」は、その日に照会した約1,800件の中で変化したものだけ。全URLの変化ではない。
- 初回は全URLが未照会なので、**全体像が揃うまで約8日**かかる。
- 照会に失敗したURLは前回の値を保持する（上書きして値を失わないため）。ローテーション順は
  `last_attempt_at`（試行時刻）で決めるので、失敗し続けるURLが巡回を止めることはない。

## ファイル

```
index_status/
├── urls.csv              # 追跡対象URL (sitemap由来 / url,group,sitemap)
├── state.csv             # URLごとの最新状態1行。URLソート済みでgit差分が行単位で残る
├── daily.csv             # 日次のステータス別件数 (推移グラフの元)
└── changes/YYYY-MM-DD.json   # その日に変化したURLだけ

src/index_status/
├── status_map.py    # coverageState → 安定キー。日英どちらの文言も受ける
├── urlset.py        # sitemap index を辿ってURLリストを組み立て + グループ分類
├── store.py         # 永続化 (state / changes / daily)
├── daily.py         # 日次バッチ本体 (ローテーション + 並列照会 + 差分検出)
└── render.py        # ダッシュボードHTML + ダウンロードCSV 生成

docs/index-status/   # GitHub Pages 公開先 (index.html / data.json / problems.csv / changes.csv)
```

日次の全件スナップショットは保存しない。14,134行×毎日を積むと履歴が肥大するだけで、
知りたいのは「変わったURL」だから。

## 実行

```bash
export GSC_SERVICE_ACCOUNT_JSON=$(cat path/to/key.json)
export GSC_SITE_URL="https://schoolwith.me/"

python -m src.index_status.urlset            # URLリストを sitemap から再生成
python -m src.index_status.urlset --dry-run  # 件数だけ確認 (保存しない)
python -m src.index_status.daily             # 日次バッチ (既定1,800件)
python -m src.index_status.daily --budget 500 --workers 5   # 小さく試す
python -m src.index_status.daily --no-refresh-urls          # sitemapを再取得しない
python -m src.index_status.render            # HTMLだけ再生成 (APIを叩かない)
python -m src.index_status.status_map        # 文言→ステータス分類のセルフテスト
python -m src.fetch_crawl_status --test      # API疎通確認 (1URLだけ照会)
```

自動実行は `.github/workflows/daily-index-status.yml`（毎日 AM7:00 JST）。
手動実行では `budget` と `render_only` を指定できる。

## 運用メモ

- **未知の coverageState が出たら**ダッシュボード上部に警告が出て「その他・未分類」に入る。
  `src/index_status/status_map.py` の `_PATTERNS` に追記する。件数が黙って合わなくなるのを防ぐため、
  未知の文字列は捨てずに生のまま `state.csv` の `coverage_raw` に残している。
- **連続40件エラーで中断する。** クォータ超過か認証失効の可能性が高いので、
  ダッシュボードの警告が出た日は Secrets とGSCの権限を確認する。
- **配色は dataviz の検証済みパレット**（light/dark とも全チェックPASS）。系列色は順序に固定で
  紐づけているので、系列が減っても残りを塗り替えない。

## GSCエクスポートの取り込み（重要）

**サイトマップだけを追跡していると、GSCの件数とは永久に一致しない。**
実測（2026-09-21）で、GSCが問題視しているURLのうちサイトマップに載っているのは **26.2%** だけだった。

| ステータス | GSC公式 | エクスポートで取れたURL | うちサイトマップ内 |
|---|---|---|---|
| 重複・ユーザーにより正規未選択 | 586 | 586（全件） | **0** |
| 重複・Googleが別ページを選択 | 5 | 5（全件） | 1 |
| クロール済み - インデックス未登録 | 4,002 | 1,000（上限） | 140 |
| 検出 - インデックス未登録 | 3,275 | 1,000（上限） | 537 |

サイトマップ外1,913件の内訳は、クエリ付き1,350（`?a8=` / `?utm_` / `?fbclid`）、
パス型パラメータ371（`/sort:` / `/direction:` / `/page:`）、通常パス192。
つまり**アフィリエイトと広告流入のパラメータURLがインデックス対象として扱われている**のが実体。

GSCが知っているURL一覧を返すAPIは無いので、レポート画面からの手動エクスポートが唯一の入口。

### 取り込み手順

1. GSC →「ページ」→ 対象ステータスの行をクリック → 右上「エクスポート」→「CSV をダウンロード」
2. 落ちてきたZIPを取り込む:

```bash
python -m src.index_status.gsc_export ~/Downloads/*Coverage-Drilldown*.zip
python -m src.index_status.daily --sync-only   # 母集団をstateに反映 (APIは叩かない)
python -m src.index_status.render
```

ZIPは4ステータスぶん同時に渡してよい。どのステータスかは `メタデータ.csv` の「問題」行から自動判定する。

### 取り込まれるもの

| ファイル | 中身 |
|---|---|
| `index_status/gsc_urls.csv` | GSCが挙げたURL（累積。一度入ったURLは消さず、直ってもそのまま追跡する） |
| `index_status/gsc_official.csv` | **GSC公式の日次件数**（エクスポートのチャートCSV由来。ダッシュボードの突き合わせに使う） |
| `index_status/gsc_exports/<データ日>/<status>/` | 生CSV。取り込みを後から再現・検証するために残す |

### 制約

- **エクスポートは1ステータスあたり1,000件で頭打ち。** 4,002件のうち取れるのは1,000件だけで、残りは追跡対象に入らない。ダッシュボードでは「URL上限 −3,002」のように明示する。
- **手動エクスポートなので自動更新されない。** 週1回ほど取り直す運用を想定。
- 取り込むたびに母集団が増えてクォータを消費する。現状16,027URLで一巡 約9日。

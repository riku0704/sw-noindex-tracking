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
→ URL Inspection API は各リクエスト2秒程度。114URLで約4分。無料枠内。

**GitHub Pagesが更新されない**
→ `docs/index.html` が commit されているか確認。`.gitignore` に含まれていないこと。

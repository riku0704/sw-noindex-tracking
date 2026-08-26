# GCP サービスアカウント + GSC 権限設定手順

## 1. GCP プロジェクト作成

1. [Google Cloud Console](https://console.cloud.google.com/) にログイン
2. 「プロジェクトを作成」→ 名前: `sw-noindex-tracking` など
3. プロジェクトを選択状態にする

## 2. API を有効化

「API とサービス」→「ライブラリ」で以下を検索して有効化:

- **Google Search Console API** (URL Inspection 用)
- **Search Console API** (Search Analytics 用) ※上記と同じことも多い

## 3. サービスアカウント作成

1. 「IAM と管理」→「サービスアカウント」→「作成」
2. 名前: `gsc-tracker`
3. ロール: 不要（後述の GSC 側で権限付与するため）
4. 作成後、サービスアカウントの詳細 → 「キー」タブ → 「鍵を追加」→ 「新しい鍵」→ **JSON形式**
5. JSONファイルがダウンロードされる（絶対に Git にコミットしない）

## 4. Google Search Console でサービスアカウントを所有者権限で追加

1. [Search Console](https://search.google.com/search-console) を開く
2. 対象プロパティ（`https://schoolwith.me/`）を選択
3. 「設定」→「ユーザーと権限」→「ユーザーを追加」
4. メールアドレス: サービスアカウントのメール（`gsc-tracker@<project>.iam.gserviceaccount.com`）
5. 権限: **所有者** （※重要: 「フルユーザー」では URL Inspection API が使えません）

## 5. ローカルで動作確認

```bash
export GSC_SERVICE_ACCOUNT_JSON=$(cat ~/Downloads/sw-noindex-tracking-*.json)
export GSC_SITE_URL="https://schoolwith.me/"

python -m src.fetch_gsc --test           # Search Analytics 疎通確認
python -m src.fetch_crawl_status --test  # URL Inspection 疎通確認
```

## 6. GitHub Secrets 登録

GitHub リポジトリ → Settings → Secrets and variables → Actions → New repository secret:

| Name | Value |
|---|---|
| `GSC_SERVICE_ACCOUNT_JSON` | JSONキーファイルの中身をコピペ（`{...}` 全体） |
| `GSC_SITE_URL` | `https://schoolwith.me/` |

## API クォータ

| API | クォータ |
|---|---|
| Search Analytics | 1,200 req/min, 30,000 req/day (プロパティ単位) |
| URL Inspection | 2,000 req/day (プロパティ単位) |

114URL × 週1回 = 114 req/週 なので余裕あり。

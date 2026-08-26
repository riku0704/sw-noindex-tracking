# Claude Code 用ガイド

## プロジェクト目的

Phase-1 NOINDEX 処理を実施した 114URL の GSC パフォーマンスと Google クロール状況を週次追跡し、GitHub Pages に公開する。

## 前提コンテキスト

- **NOINDEX処理前ベースライン**: 2026-05-01〜05-07
- **NOINDEX処理開始**: 2026-05-中旬〜下旬
- **対象URL**: `targets.json` (国9 / 都市47 / 学校58)
- **参照元HTML**: 親フォルダの `未契約候補98校_優先順位判定_NOINDEX前提.html`

## コード規約

- Python 3.11+
- 型ヒント積極利用 (`typing`)
- 例外は明示的に catch (bare except 禁止)
- ログは `logging` モジュール使用
- ファイルI/Oはすべて UTF-8

## タスクの進め方

1. `targets.json` の 114URL は不変前提
2. データ取得は `src/fetch_gsc.py` (Search Analytics) と `src/fetch_crawl_status.py` (URL Inspection) の 2 系統
3. 週次スナップショットは `history/YYYY-MM-DD.json` に追加のみ (既存改変禁止)
4. HTML 生成は Jinja2 テンプレート (`templates/dashboard.html.j2`)
5. `docs/index.html` は GitHub Pages 公開先。上書き OK

## テスト実行

```bash
python -m src.main --dry-run       # データ取得のみ
python -m src.main --html-only     # 既存データから HTML 再生成
python -m src.main --inspect-only  # URL Inspection のみ実行
python -m src.main --backfill      # 過去N週間分を一括取得
```

## 注意点

- **絶対にサービスアカウントJSONをコミットしない** (`.gitignore` 設定済)
- **URL Inspection API のクォータ**: 2,000 req/day/property。114URLなら安全
- **HTMLテンプレートの変更**: `templates/dashboard.html.j2` を編集し `--html-only` で確認
- **Windsor.ai は使用しない**: 直接 Google API を叩く（Windsor.ai は URL Inspection 未対応）

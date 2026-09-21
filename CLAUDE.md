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

---

## もう1つのパイプライン: index_status（日次インデックス状態）

このリポジトリには独立した2系統がある。混ぜないこと。

| | 週次NOINDEX (既存) | 日次インデックス状態 (index_status) |
|---|---|---|
| 目的 | NOINDEX処理114URLの効果測定 | サイト全14,134URLの状態変化の日次把握 |
| エントリ | `src/main.py` | `src/index_status/daily.py` → `render.py` |
| データ | `history/YYYY-MM-DD.json` | `index_status/{state,daily}.csv` + `changes/` |
| 出力 | `docs/index.html` | `docs/index-status/index.html` |
| 実行 | 週次 (月AM6 JST) | 日次 (AM7 JST) |

共有しているのは `GSC_SERVICE_ACCOUNT_JSON` / `GSC_SITE_URL` と
`src/fetch_crawl_status.py` の URL Inspection クライアントだけ。

### index_status を触るときの注意

- **クォータは 2,000 URL/日/プロパティ。** 全14,134URLを毎日は照会できない。
  `last_attempt_at` の古い順に1日1,800件ずつ回して約8日で一巡させている。この前提を崩さない。
- **照会失敗時に state を上書きしない。** 失敗したURLは前回値を保持する。
  ローテーション順は `last_attempt_at`（試行）で決め、`checked_at`（成功）とは別に持っている。
- **未知の coverageState は "other" に落とすが、必ず WARN ログとダッシュボード警告を出す。**
  黙って握りつぶすと件数だけが合わなくなる。生文字列は `coverage_raw` に残す。
- **日次の全件スナップショットは作らない。** 保存するのは最新状態1枚と差分だけ。
- **グラフに「インデックス登録済み」を混ぜない。** 桁が違って問題系が潰れる。2軸グラフも作らない。
- **配色は `render.py` の `SERIES_COLORS`**（dataviz検証済み）。順序＝系列の対応を変えない。

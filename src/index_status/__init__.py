"""サイト全URLのインデックス状態を日次で追跡するパイプライン。

既存の週次NOINDEXダッシュボード (src/main.py) とはデータもHTMLも独立している。
共有しているのは認証 (GSC_SERVICE_ACCOUNT_JSON) と URL Inspection クライアントだけ。
"""

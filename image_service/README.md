# 画像サービス

署名付きQZQSM URLを受け取り、PNG／可逆WebPを生成するWSGIアプリです。
受信機とは別の依存環境・プロセスで動作します。

- [仕様・対応する通報](../docs/specification.md)
- [開発環境・プレビュー・テスト](../docs/development.md)
- [Vercelへの公開](../docs/deployment.md)
- [固定地図・フォント・出典](../vendor/README.md)

入口は`app.py`（HTTP）と`__main__.py`（オフライン描画）です。
`render.py`と`render_v*.py`は公開版、`render_preview.py`と`preview_*.py`は次の変更を確認する作業版です。
公開版の出力を維持しながら変更してください。

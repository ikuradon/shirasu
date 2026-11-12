# 固定した外部データ

画像に使う地図・フォントを取得元別に管理します。地理データは加工済みスナップショット、
フォントは未加工の原本です。取得元・加工条件・入力のハッシュ・ライセンスは各manifestとNOTICEに記録しています。

| ディレクトリ | 内容 |
| --- | --- |
| `jma/` | 気象庁の区域・地点・流路と加工記録 |
| `natural-earth/` | Natural Earthの地理データと変換記録 |
| `noto-cjk/2.004/` | Noto Sans CJK JP Regular／Bold、OFL、著作権表示 |

`v1`・`v2`・`v3`・`preview`はアプリの配布先を区別する名前で、提供元の版番号ではありません。
複数の取得元を束ねる索引は`image_service/assets/v1/manifest.json`にあります。

## 配置

```sh
python3 -m tools.assets.prepare_vendor_assets
python3 -m tools.assets.prepare_vendor_assets --check
```

`vendor/manifest.json`のSHA-256とsizeを検証し、`image_service/assets/`へ配置します。
通信やGIS再加工は行いません。既存ファイルが一致すれば保持し、不一致なら上書きせず停止します。
生成先はGit管理外です。

## データ更新

1. `tools/assets/prepare_*assets.py`へ元データを渡し、新規の作業先へ出力する。
   例: `python -m tools.assets.prepare_flood_assets --source /saved/source.zip --output dist/vendor-update/flood`
2. 元データのハッシュ、区域コード、出典・ライセンス、描画差分を確認する。
3. 対象のvendorファイルと、`vendor/manifest.json`の対応するSHA-256・sizeを更新する。
4. 更新した生成先ファイルだけを削除して再配置するか、新規checkoutで配置を確認する。
5. アセット検証と既存公開版の画像比較を行う。同じ公開URLの出力を変える場合は描画版を分ける。

通常CIで提供元からの自動更新は行いません。
洪水流路の出典・加工内容・配布上の制約は[`FLOOD_NOTICE.txt`](jma/preview/FLOOD_NOTICE.txt)を参照してください。

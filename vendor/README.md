# 固定した外部データ

画像に使う外部データを取得元ごとに保存します。フォントは未加工の原本、
地理データの多くは既に検証した加工済みスナップショットです。
全てを上流の原本ZIPと呼ぶものではありません。

| ディレクトリ | 内容 |
| --- | --- |
| `jma/` | 気象庁の区域・地点・流路を固定したデータ、出典、加工内容・入力のSHA-256 |
| `natural-earth/` | Natural Earthの50m GeoJSONと10m landの変換済みGeoJSON、出典・変換記録 |
| `noto-cjk/2.004/` | Noto Sans CJK JP Regular／Boldの未加工フォント、OFL、著作権表示 |

`v1`、`v2`、`v3`、`preview`は既存の配布先を区別する名前です。
データ提供元のバージョンと同じ意味ではありません。
提供元URL・原本のハッシュ・座標変換等の記録は各フォルダの既存manifestとNOTICEに残しています。
複数の取得元を束ねる`image_service/assets/v1/manifest.json`は、アプリ側の索引としてGit管理を続けます。

## 配布用アセットの組み立て

```sh
python3 -B scripts/prepare_vendor_assets.py
python3 -B scripts/prepare_vendor_assets.py --check
```

`vendor/manifest.json`のsource・destination・SHA-256・sizeを照合して、
39ファイルを従来の`image_service/assets/`へ配置します。通信もGIS再変換も行いません。
配布パッケージのパス、地理データ、フォントのバイト列は従来どおりです。
生成先はGit管理外で、GHAとローカルのどちらでも同じコマンドを使えます。

初回は不足するファイルだけを生成します。既に同じファイルがあれば保持し、
異なるファイル・symlink・checksum不一致は上書きせず停止します。
失敗時に`.env`、設定、DB、他の出力を掃除する処理はありません。

## データ更新

通常のCIでは提供元からの自動更新を行いません。以下はデータ更新を決めたときだけ行います。

1. 対象の`prepare_*assets.py`を、元ZIP等を指定して新規の作業ディレクトリへ出力する。
   例: `python scripts/prepare_flood_assets.py --source /saved/source.zip --output dist/vendor-update/flood`
2. 元データのSHA-256、コード・地形・出典・ライセンスと描画差分を確認する。
3. 更新対象のvendorファイルだけを置き換え、`vendor/manifest.json`の当該行のSHA-256とsizeを更新する。
   配布先pathは既存のパッケージallowlistと照合されるため、任意のファイルを追加できない。
4. 新規checkoutでmaterializeするか、更新する生成先のファイルだけを明示的に削除して再実行する。
   materializerには異なる既存ファイルを強制上書きするオプションを設けていない。
5. アセット検証と、既存公開版を含む画像比較を実行してからcommitする。

公開済みURLの描画を変える更新は、配布タグだけでなく描画版・既存経路の維持も判断します。
`scripts/prepare_*assets.py`は再生成・更新用として残し、通常デプロイの外部サービス依存にはしません。

洪水流路は、ユーザーが選択した現行の出典付きデータを`jma/preview/`で管理します。
個別GISの無条件の再配布許諾が確認できたという意味ではありません。
詳しい出典・加工内容・配布方針は`jma/preview/FLOOD_NOTICE.txt`を参照してください。

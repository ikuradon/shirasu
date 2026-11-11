# 可逆WebP method 4の採用検証

`84cf987`時点の未公開レンダラーについて、PNG経由の変換とRGBAからの直接出力を
比較する検証です。公開済みv1〜v4と、以前に固定したPNGの公開候補は変更しません。
この資料はエンコード方式の検証時点の記録です。その後の署名URL・HTTP・投稿への接続は
[WebPの接続検証](webp-integration.md)を参照してください。

## 結果と採用判断

**生成コストの増加を許容し、配信容量を減らす目的で、直接RGBAからの可逆WebPを採用する案を推奨します。**
49画像の容量合計はPNG 12.131 MiBからWebP 4.334 MiBへ、64.27%減りました。
全49入力・ギャラリーの86使用箇所でRGBA全画素・寸法・表示判定が一致し、
直接出力とPNG経由のWebPも全49画像で同じバイト列になっています。

Linux aarch64・論理4CPU、Python 3.12.16、Matplotlib 3.11.2、Pillow 13.0.0、
libwebp 1.6.0で測定した再描画3回の中央値です。

| 入力 | 現行PNG | PNG経由WebP | 直接WebP |
| --- | ---: | ---: | ---: |
| 気象 | 1.107秒 | 2.531秒 | 2.506秒 |
| 緊急地震速報 | 3.137秒 | 4.354秒 | 4.384秒 |
| 南海トラフ | 0.197秒 | 0.607秒 | 0.473秒 |
| 気象取消・元報参照 | 1.411秒 | 1.918秒 | 1.750秒 |

直接出力でも、現行PNGより約0.28〜1.40秒長くなりました。PNG経由との比較では4例中3例で
短縮し、緊急地震速報は約0.03秒増えています。中間PNGを省けても常に速くなるとは言えません。
方式の選定は容量と処理経路の単純さを根拠にし、速度向上を採用理由にしません。
ここでの値はローカルの生成時間であり、Vercelの実応答時間ではありません。

代表4例の反復でも出力バイト列は安定し、全描画後のRendererAgg残存数は0でした。
公開版用45ファイルは`84cf987`とバイト単位で一致し、通常配布には実験用モジュールを含めません。
回帰テスト29件が成功しました。新規8件で透明画素を含む可逆性・直接RGBA出力・例外時の
バッファ解放・一時フックの復元・入力照合・既存出力保護を確認し、既存21件でPNGの署名URL・
HTTP契約・未公開プレビューと通常配布の境界を確認しています。
HTTP・署名・投稿への接続は、下記の契約を実装してから別の通し検証を行います。

## 固定する設定

`lossless=True, quality=100, method=4, exact=True`を使用します。
可逆圧縮時のqualityは圧縮に費やす努力量で、exactは透明画素の隠れたRGBも保持する設定です。
[Pillow公式のWebP仕様](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#webp)。

直接出力にはMatplotlibの`savefig(format='webp', metadata=None, pil_kwargs=...)`を使います。
Aggが描いたRGBAをPillowへ渡し、中間PNGを作らずに圧縮できます。
[MatplotlibのWebP出力](https://matplotlib.org/stable/api/backend_agg_api.html#matplotlib.backends.backend_agg.FigureCanvasAgg.print_webp)。
検証用ヘルパーは既存`_finish`をそのまま呼び、対象Figureのsavefigだけを一時的に切り替えます。
文字の効果・DPI・背景色・表示判定は共通にし、描画ロックの内側で切り替えます。

## 比較方法

`scripts/benchmark_webp.py`で、承認済みギャラリーと既存のPNG計測記録の入力・画像ハッシュ・
表示判定を照合してから測定します。入力や参照元が変わっていれば中断し、既存出力も上書きしません。

- 全49入力：直接WebPを生成し、元PNGと復号後のRGBA全画素・寸法・表示判定を比較します。
  元PNGを可逆WebPへ変換した結果とも、画素とWebPバイト列を比較します。
- 気象・緊急地震速報・南海トラフ・元報参照付き気象取消の4例：現行PNG、PNG経由WebP、
  直接WebPをそれぞれ独立プロセスで初回1回・再描画3回測定します。形式の測定順を入れ替え、
  空のフォントキャッシュを使います。OSのファイルキャッシュは消去しません。
- 時間は描画・エンコード・後始末を含む総時間です。ヘルパーとMatplotlibは事前にimportし、
  **初回値には未公開レンダラーの遅延importを含みます**。画素照合・RSS取得は除外します。
  `savefig_ms`は最終描画を含むため、圧縮だけの時間とは扱いません。
- 強制GCは使いません。全RendererAggを弱参照で追跡し、描画後の残存数0を確認します。
  エンコード失敗時の解放も別のテストで検証します。

記録したRSSには画素比較用の配列も含まれます。本番レンダラー単体のメモリ量としては扱いません。
計測後のレビューで初回の遅延importに関するmetadata表記を訂正しました。
元のmanifestと実行ソースを保持し、時間・容量・画素の実測値は変更していません。

```sh
docker run --rm --network none \
  --volume "$PWD:/source:ro" --volume "$PWD/dist:/output" --workdir /source \
  shirasu-images:preview python -B scripts/benchmark_webp.py \
  --output /output/webp-adoption \
  --baseline /source/dist/benchmark --gallery /source/dist/preview
```

`--case weather --timing-case weather`で対象を絞れます。`--repeats`は再描画回数1〜10、
`--timeout`は各workerの制限秒数です。失敗時はmanifestへエラーを残し、終了コード1にします。
生成物は`manifest.json`、`results.csv`、入力を記録した`plan.json`、`timings/`、`corpus/`です。
実測時のソースと公開版の不変確認も`dist/webp-adoption/`へ保存します。

## nostterの実画面での確認

2026-11-11に、公開中の`nostter.app`をChromium 158.0.8127.18で読み込みました。
Browserプラグインがないため既存Playwrightを使い、PC 1440×1000とスマホ幅390×844で
「合成投稿の表示→WebP画像クリック→拡大→閉じる」を実行しています。
画面本体のGET/HEADだけをネットワークへ通し、リレーと画像応答はブラウザ内で差し替えました。
投稿・ログイン・外部リレー接続は行っていません。

画像には今回の直接RGBA出力を使い、本文と`imeta`に提案形状のHTTPS URL
`/v5/test/<payload>.<signature>.w1.webp`を付けました。
この検証では画像サービスのHMACを認証しておらず、URL形状とクライアントの表示処理を検証しています。

| 確認 | 結果 |
| --- | --- |
| 投稿画面・本文・画像が表示される | PC・スマホとも成功、空画面やフレームワークエラーなし |
| WebPの復号 | 1920×1080、ブラウザで取得したRGBAもPNGと完全一致 |
| クリック・拡大・閉じる | PC・スマホとも成功、横方向のはみ出しなし |
| 画像最適化サービスが503 | 元のWebPへ戻り、表示・拡大に成功 |
| JavaScript | ページ例外0。遮断したCloudflareの計測・challengeと、注入した503のconsoleエラーは想定どおり |

非フォロー作者の画像にぼかしを付ける挙動と、`img alt`にURLを使い`imeta alt`を
反映しない挙動はnostter側の現状です。WebPへの変更に起因するものではありません。
画像最適化を任意で有効にすると再圧縮されるため、配信元が可逆でも表示先で常に画素一致するとは限りません。
既定の最適化は無効です。
[Img.svelte](https://github.com/SnowCait/nostter/blob/78e22b7a7e79825290ee779a958a368eb90833f7/web/src/lib/components/content/Img.svelte)、
[Thumbnail.svelte](https://github.com/SnowCait/nostter/blob/78e22b7a7e79825290ee779a958a368eb90833f7/web/src/lib/components/content/Thumbnail.svelte)、
[Preference.ts](https://github.com/SnowCait/nostter/blob/78e22b7a7e79825290ee779a958a368eb90833f7/web/src/lib/stores/Preference.ts)。

記録は`dist/webp-client-check/browserresult.json`、`sources-and-limits.json`、
`check_nostter_webp.py`とスクリーンショット6枚です。公開サイトのscript URLとハッシュも記録し、
調査したGit commitと実デプロイのcommitが同じだとは推定しません。
実TLS・CDN・HMAC・本番投稿、他のNostrクライアント、Safari・Firefoxは今回の確認範囲外です。

![nostter PC拡大表示](../dist/webp-client-check/desktop-enlarged.png)
![nostter スマホ幅の拡大表示](../dist/webp-client-check/mobile-enlarged.png)

## 配信へ接続する際の契約

描画版とエンコード設定の版を分け、WebPには固定プロファイル`w1`を持たせる案です。

```text
既存PNG: /v1/<key>/<payload>.<signature>.png （v2〜v4も維持）
新規候補: /v5/<key>/<payload>.<signature>.w1.webp
```

`w1`は上記の圧縮設定と検証済みの符号器を固定します。符号器変更でバイト列が変わる場合は、
既存URLの結果を書き換えず別プロファイルにします。圧縮設定だけの変更で、全描画の版を
上げる必要はありません。これは採用時の設計案で、現行のURLとして発行・公開していません。

現在の署名はPNGのみを許可しているため、単に拡張子の許可を増やしてはいけません。
既存PNGの署名計算を維持し、WebP専用のHMACドメインで描画版・キーID・圧縮payload・
プロファイル・拡張子を認証します。認証後に展開し、未知の設定や任意のquality指定は拒否します。
Acceptによる内容交渉は行わず、同じURLは常に同じ形式を返す設計です。

接続実装後に必要な通し検証は次のとおりです。

1. 実HTTPのGET・HEAD・304で`image/webp`、実WebPのContent-Length・ETagを確認する。
2. 実Serviceが作るURLとNostrの`imeta`を`m image/webp`で一致させる。本文・ALT・原電文は維持する。
3. 形式・プロファイル・版の改ざんを展開前に拒否する。PNGの保存済みURL・画像・ETagと
   保存済み投稿イベントの再送が変わらないことを確認する。
4. WebP生成失敗時に、同じWebP URLでPNGを返さない。受信側と画像側の固定プロファイルを揃える。
5. 検証済みの候補を公開した後、実TLS・CDN・署名URL・SNS表示まで確認する。

`imeta`のURLは本文中のURLと一致させ、形式にはMIMEを使います。
[NIP-92](https://github.com/nostr-protocol/nips/blob/master/92.md)。
エンコードとクライアント表示の検証だけで、この配信接続まで完了したとは扱いません。

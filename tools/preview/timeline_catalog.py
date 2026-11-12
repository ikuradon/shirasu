"""Synthetic arrival schedules over the gallery's reproducible training inputs.

The elapsed seconds describe a made-up reception sequence, not timing captured
by a receiver. Original decode date anchors are preserved independently. This
catalog neither changes the static gallery nor reads receiver configuration.
"""

from dataclasses import dataclass

from tools.preview.gallery_catalog import _field_variant, load_cases


@dataclass(frozen=True)
class TimelineScenario:
    slug: str
    title: str
    description: str
    records: tuple[tuple[str, int], ...]
    arrival_seconds: tuple[int, ...]
    until_seconds: int
    receipt_labels: tuple[str, ...]
    sources: tuple[dict, ...]


def _variant(record, information):
    sentence, milliseconds = record
    return _field_variant(sentence, ((41, 2, information),)), milliseconds


def _source(case, transformation):
    return ({
        'kind': 'synthetic',
        'label': '同梱例から派生した合成訓練例（受信時系列は仮定）',
        'file': case.source_file,
        'sha256': case.source_sha256,
        'url': case.source_url,
        'transformation': transformation,
    },)


def load_scenarios():
    """Return four finite scenarios; every frame is explicitly a training report."""
    cases = {case.slug: case for case in load_cases()}
    nankai_case = cases['nankai-advisory']
    pages = nankai_case.records
    typhoon_case = cases['typhoon-cancel-reference']
    track = typhoon_case.cancel_reference
    # These counts make each human-readable arrival label unambiguous. A change
    # to its source fixture must be accompanied by a review of this scenario.
    if len(pages) != 5 or len(track) != 6:
        raise ValueError('Timeline source fixture page/forecast count changed')
    preserved = ('同梱の南海トラフ合成訓練電文5ページを使用。'
                 '電文と復号の基準日時を保持し、受信の間隔だけを仮定しています。')
    corrected = tuple(_variant(record, 1) for record in pages)
    cancelled = _variant(pages[0], 2)
    return (
        TimelineScenario(
            slug='nankai-late',
            title='南海トラフ：未完了通知から後着完成',
            description='1ページだけ届いた状態で180秒待ち、本文未受信の通知を投稿。'
                        'その後に残りが揃うと、全文と見出し画像を追加投稿します。',
            records=pages,
            arrival_seconds=(0, 240, 250, 260, 270),
            until_seconds=450,
            receipt_labels=tuple(f'発表報 {page}/5ページ' for page in range(1, 6)),
            sources=_source(nankai_case, preserved),
        ),
        TimelineScenario(
            slug='nankai-correction',
            title='南海トラフ：受信途中の訂正',
            description='元の発表報を集めている間に訂正報を受信。訂正の5ページが揃うと投稿し、'
                        '未完成の元報や後から届いた古いページは投稿しません。',
            records=(*pages[:2], *corrected, pages[2]),
            arrival_seconds=(0, 20, 60, 70, 80, 90, 100, 130),
            until_seconds=360,
            receipt_labels=('元の発表報 1/5ページ', '元の発表報 2/5ページ',
                            *(f'訂正報 {page}/5ページ' for page in range(1, 6)),
                            '古い発表報 3/5ページ（後着）'),
            sources=_source(nankai_case, preserved +
                            '訂正報は元の全5ページのビット41〜42をIt=1へ変更し、'
                            'CRC-24QとNMEAチェックサムを再計算。本文は同じ訓練文です。'),
        ),
        TimelineScenario(
            slug='nankai-cancellation',
            title='南海トラフ：受信途中の取消',
            description='元の発表報が未完成の間に取消を受信すると、その時点で取消を投稿。'
                        '古いページが後着しても元報や未完了通知は投稿しません。',
            records=(*pages[:2], cancelled, pages[2]),
            arrival_seconds=(0, 20, 60, 120),
            until_seconds=300,
            receipt_labels=('元の発表報 1/5ページ', '元の発表報 2/5ページ',
                            '取消報 1/5ページ', '古い発表報 3/5ページ（後着）'),
            sources=_source(nankai_case, preserved +
                            '取消報は元の1ページ目のビット41〜42をIt=2へ変更し、'
                            'CRC-24QとNMEAチェックサムを再計算。実受信の取消ではありません。'),
        ),
        TimelineScenario(
            slug='typhoon-additional',
            title='台風：初回投稿のあとに予報が追加',
            description='最初の2電文を180秒でまとめて投稿。後から届いた4電文は、'
                        '追加分の最初の受信から180秒待ち、受信済みの全6時点で追加更新します。',
            records=track,
            arrival_seconds=(0, 30, 240, 250, 260, 270),
            until_seconds=600,
            receipt_labels=('実況', '12時間先の予報', '24時間先の予報（追加）',
                            '48時間先の予報（追加）', '72時間先の予報（追加）',
                            '96時間先の予報（追加）'),
            sources=_source(typhoon_case,
                            '同梱typhoon.jsonから作成された合成訓練の元報6電文を使用。'
                            'ビット14〜16がRc=7、41〜42がIt=0で、CRC-24Qと'
                            'NMEAチェックサムは再計算済み。日時・台風番号・実況予報を保持し、'
                            '受信間隔だけを仮定しています。取消報は入力しません。'),
        ),
    )

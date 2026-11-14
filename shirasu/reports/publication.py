"""Source notice and controlled hashtags shared by posts and offline checks.

Only the footer created here becomes Nostr hashtag metadata. Hashtags quoted
inside a bulletin, attachment URL or raw NMEA record are not indexed.
"""


ATTRIBUTION = '出典・利用条件：https://github.com/ikuradon/shirasu/blob/main/licenses/README.md'
_DCR_TAGS = {
    'Typhoon': '台風',
    'Hypocenter': '地震', 'SeismicIntensity': '地震', 'EarthquakeEarlyWarning': '地震',
    'Tsunami': '津波', 'NorthwestPacificTsunami': '津波',
    'Weather': '気象情報', 'Volcano': '火山', 'AshFall': '降灰',
    'Flood': '洪水', 'Marine': '海上警報', 'NankaiTroughEarthquake': '南海トラフ',
}
_DCX_TAGS = {'LAlert': 'Lアラート', 'JAlert': 'Jアラート',
             'MTInfo': '自治体情報', 'OutsideJapan': '海外災害情報'}
_DISPLAY_TAGS = {value.lower(): value for value in
                 ('QZSS', *_DCR_TAGS.values(), *_DCX_TAGS.values(), '訓練')}


def report_hashtags(report):
    """Choose a report-family tag without inferring a hazard from free text."""
    # Footer inspection does not need the decoder in image/release packages.
    from azarashi.reports import dcr, dcx

    kind = type(report)
    dcr_tags = {getattr(dcr, name): label for name, label in _DCR_TAGS.items()}
    dcx_tags = {getattr(dcx, name): label for name, label in _DCX_TAGS.items()}
    if isinstance(report, dcr.Base):
        training = type(report.report_classification_no) is int and report.report_classification_no == 7
        label = dcr_tags.get(kind)
        return ('QZSS',) + ((label,) if label else ()) + (('訓練',) if training else ())
    if kind in dcx_tags:
        camf = report.camf
        known_version = (type(camf.vn) is int and camf.vn in (0, 1)
                         and report.dcx_version == camf.vn)
        training = known_version and type(camf.a1) is int and camf.a1 == 0
        return ('QZSS', dcx_tags[kind]) + (('訓練',) if training else ())
    return ('QZSS',)


def _hashtags(values):
    result = []
    for value in values:
        if not isinstance(value, str) or value.lower() not in _DISPLAY_TAGS:
            raise ValueError('Unknown posting hashtag')
        canonical = _DISPLAY_TAGS[value.lower()]
        if canonical not in result:
            result.append(canonical)
    if result and (result[0] != 'QZSS' or len(result) > 3
                   or ('訓練' in result and result[-1] != '訓練')
                   or len([value for value in result if value not in ('QZSS', '訓練')]) > 1):
        raise ValueError('Use QZSS, at most one report type, and an optional training tag')
    return tuple(result)


def post_message(text, sentences, *, hashtags=()):
    """Append metadata while retaining the bulletin and original record order."""
    tags = _hashtags(hashtags)
    if tags:
        # A supplied source notice is already ours; keep one notice and insert
        # tags before it. Do not strip or normalize received body whitespace.
        if text == ATTRIBUTION:
            text = ''
        elif text.endswith('\n\n' + ATTRIBUTION):
            text = text[:-len('\n\n' + ATTRIBUTION)]
        elif text.endswith('\n' + ATTRIBUTION):
            text = text[:-len('\n' + ATTRIBUTION)]
        text += '\n\n' + ' '.join('#' + tag for tag in tags) + '\n\n' + ATTRIBUTION
    elif text != ATTRIBUTION and not text.endswith('\n' + ATTRIBUTION):
        text += '\n\n' + ATTRIBUTION
    return '\n'.join((text, '---', *(f'NMEA: {sentence}' for sentence in sentences)))


def message_hashtags(message):
    """Read only our final footer, returning lowercase NIP-24 tag values."""
    body, separator, records = message.rpartition('\n---\n')
    if not separator or not records or not all(line.startswith('NMEA: ') for line in records.split('\n')):
        return ()
    notice = '\n\n' + ATTRIBUTION
    if not body.endswith(notice):
        return ()
    _, separator, line = body[:-len(notice)].rpartition('\n\n')
    if not separator:
        return ()
    tokens = line.split(' ')
    if not tokens or any(not token.startswith('#') for token in tokens):
        return ()
    try:
        tags = _hashtags(token[1:] for token in tokens)
    except ValueError:
        return ()
    return tuple(tag.lower() for tag in tags)

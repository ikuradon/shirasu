"""Structured decisions for offline review; never drawn into the PNG."""

from dataclasses import asdict, dataclass

from image_service.preview_diagnostic_errors import known_error_code


MESSAGES = {
    'cancelled': '取消通報として表示',
    'cancel_reference_map': '別途確認して指定された元情報の画角を使用',
    'cancel_area_map': '取消報に記載された地域を背景に表示',
    'cancel_reference_unavailable': '指定された元情報の地図を参照できません',
    'unsupported_report': 'この種別の地図表示は未対応',
    'unsupported_version': '未対応の通報バージョン',
    'unknown_hazard': '未対応の災害種別',
    'invalid_fields': '受信値が不正、欠落、または矛盾',
    'no_geometry': '有効な地点・範囲の指定なし',
    'no_area': '独立して使える対象区域の指定なし',
    'unsupported_geometry': '追加の位置・範囲表現が未対応',
    'unknown_area': '未対応または不正な対象区域コード',
    'missing_boundary': '対象区域の境界データが未収録',
    'missing_position': '対応する地点データが未収録',
    'projection_unavailable': '受信した範囲を対応投影で描画できない',
    'layout_unavailable': '全情報を読める配置にできない',
    'unclassified_fallback': '分類できない制約により表示を切替',
}
STAGES = frozenset(('selection', 'geometry', 'area', 'layout', 'render'))
MODES = frozenset(('map', 'geometry', 'area', 'topic', 'summary'))


@dataclass(frozen=True)
class Reason:
    code: str
    stage: str
    message: str
    detail: str = ''


def reason(code, stage='selection', detail=''):
    """Details are authored field/code descriptions, never raw exceptions."""
    if code not in MESSAGES or stage not in STAGES:
        raise ValueError('Unknown preview diagnostic')
    if not isinstance(detail, str) or len(detail) > 240 or any(ord(c) < 32 for c in detail):
        raise ValueError('Invalid preview diagnostic detail')
    return Reason(code, stage, MESSAGES[code], detail)


def unique_reasons(reasons):
    return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class Decision:
    mode: str
    reasons: tuple[Reason, ...] = ()

    def __post_init__(self):
        if self.mode not in MODES or not all(isinstance(item, Reason) for item in self.reasons):
            raise ValueError('Invalid preview rendering decision')

    def as_dict(self):
        return {'mode': self.mode, 'reasons': [asdict(item) for item in self.reasons]}


class PreviewFallback(ValueError):
    """A rendering constraint with an explicit stable diagnostic code."""
    def __init__(self, code, *, stage='render', detail='', technical='', previous_reasons=()):
        self.reason = reason(code, stage, detail)
        self.reasons = unique_reasons((*previous_reasons, self.reason))
        super().__init__(technical or self.reason.message)


def error_reason(error, *, stage='render'):
    if isinstance(error, PreviewFallback):
        return error.reason
    # Existing renderers also use ValueError for malformed source data. An
    # unknown exception must not be guessed to be a layout failure from words
    # in its text, nor have an arbitrary error string copied into a gallery.
    return reason(known_error_code(error) or 'unclassified_fallback', stage)


def error_reasons(error, *, stage='render'):
    """Keep previous attempts when a DCX fallback itself needs another try."""
    if isinstance(error, PreviewFallback):
        return error.reasons
    return (error_reason(error, stage=stage),)

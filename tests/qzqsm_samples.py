"""Offline public recordings with pinned provenance and independent expectations."""

from contextlib import ExitStack
from datetime import datetime
import json
from pathlib import Path
from unittest.mock import patch

import azarashi
from azarashi.reports.dcr import NankaiTroughEarthquake


CORPUS = Path(__file__).resolve().parent / 'fixtures' / 'qzqsm'


def manifest():
    return json.loads((CORPUS / 'manifest.json').read_text(encoding='utf-8'))


def recording_path(name):
    return CORPUS / manifest()['recordings'][name]['file']


def frames(name):
    """Return original NMEA messages in recorded order, without source comments."""
    return [line for line in recording_path(name).read_text(encoding='utf-8').splitlines()
            if line.startswith('$QZQSM,')]


def reference_time(name):
    """A fixed date anchor, not an individual message's reception timestamp."""
    return datetime.fromisoformat(manifest()['recordings'][name]['reference_time'])


def decode_reports(name):
    """Decode without leaking Nankai class state into another test.

    Returned reports retain their individual fields. Nankai's decoder-global
    text assembly exists only during this call; use the application's collector
    to assemble the returned pages, not extract_text_information() afterwards.
    """
    with ExitStack() as stack:
        for attribute, value in (('completed', False), ('reports', {}),
                                 ('announcement', None)):
            stack.enter_context(patch.object(NankaiTroughEarthquake, attribute, value))
        received_at = reference_time(name)
        return [azarashi.decode(frame, timestamp=received_at) for frame in frames(name)]


def official_scenarios():
    file = manifest()['official_scenarios_file']
    return json.loads((CORPUS / file).read_text(encoding='utf-8'))


def expected_nankai_text(name='dcr_receiver_20260924'):
    file = manifest()['recordings'][name]['nankai']['expected_text_file']
    # The file's final newline is an editorial convention, not transmitted text.
    return (CORPUS / file).read_text(encoding='utf-8').removesuffix('\n')

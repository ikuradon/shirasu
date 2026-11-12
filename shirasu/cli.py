"""Receive QZSS reports and deliver them through a durable Nostr outbox."""

import argparse
import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import logging
import os
import sys
import time

from shirasu import service
from shirasu.health import healthy
from shirasu.outbox import event_id_prefix, read_event, read_history, read_status


def show_id(value):
    try:
        return event_id_prefix(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def backup_path(value):
    if not value:
        raise argparse.ArgumentTypeError('バックアップのファイルパスを指定してください')
    return value


def received_at(value):
    try:
        timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if timestamp.utcoffset() is None:
            raise ValueError('timezone is required')
        return timestamp.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError('use an ISO 8601 date/time with a timezone, e.g. 2026-11-08T12:00:00+09:00') from error


def run_preview(args):
    from shirasu.preview import preview_stream

    timestamp = args.received_at or datetime.now(timezone.utc)
    total = dict(decoded=0, shown=0, ignored=0, errors=0)
    for filename in args.preview:
        try:
            # Standard input belongs to the caller; only close files opened here.
            source = nullcontext(sys.stdin.buffer) if filename == '-' else open(filename, 'rb')
            with source as stream:
                counts = preview_stream(
                    stream, input_format=args.input_format or 'nmea', received_at=timestamp,
                    message_formatter=service.report_message, report_filter=service.is_publishable,
                    stdout=sys.stdout, stderr=sys.stderr, source_name=filename,
                )
            for name in total:
                total[name] += counts[name]
        except (OSError, ValueError) as error:
            total['errors'] += 1
            message = ' '.join(str(error).split())
            message = ''.join(char for char in message if char.isprintable())
            print(f'入力を読み取れません: {message}', file=sys.stderr)
    print(f"プレビュー: 読取 {total['decoded']}件 / 表示 {total['shown']}件 / "
          f"除外 {total['ignored']}件 / エラー {total['errors']}件", file=sys.stderr)
    if not total['decoded']:
        print('通報データが見つかりません。入力ファイルと --input-format を確認してください。', file=sys.stderr)
    return 1 if total['errors'] or not total['decoded'] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--status', action='store_true', help='print saved runtime state (default: JSON)')
    group.add_argument('--health', action='store_true', help='exit successfully only when healthy')
    group.add_argument('--history', action='store_true', help='list saved delivery history (default: text)')
    group.add_argument('--show', type=show_id, metavar='EVENT_ID',
                       help='show a saved event by its ID or unique 12+ character prefix (default: text)')
    group.add_argument('--preview', nargs='+', metavar='FILE', help='preview reports from files; use - for standard input')
    group.add_argument('--backup', type=backup_path, metavar='FILE',
                       help='create and validate a consistent state backup without overwriting FILE')
    group.add_argument('--verify-backup', type=backup_path, metavar='FILE',
                       help='validate a standalone state backup without changing it')
    parser.add_argument('--format', choices=('text', 'json'), help='output format for inspection and backup commands')
    parser.add_argument('--state', choices=('pending', 'sent', 'expired'), help='filter --history by delivery state')
    parser.add_argument('--limit', type=int, help='maximum history rows (default: 20, maximum: 1000)')
    parser.add_argument('--input-format', choices=('nmea', 'ubx'), help='preview input format (default: nmea)')
    parser.add_argument('--received-at', type=received_at, help='preview reference date/time with timezone (default: current UTC time)')
    args = parser.parse_args(argv)
    if args.format and not (args.status or args.history or args.show
                            or args.backup is not None or args.verify_backup is not None):
        parser.error('--format requires --status, --history, --show, --backup or --verify-backup')
    if (args.state is not None or args.limit is not None) and not args.history:
        parser.error('--state and --limit require --history')
    if args.limit is not None and not 1 <= args.limit <= 1000:
        parser.error('--limit must be between 1 and 1000')
    if (args.input_format is not None or args.received_at is not None) and not args.preview:
        parser.error('--input-format and --received-at require --preview')
    if args.preview and args.preview.count('-') > 1:
        parser.error('standard input (-) can only be used once')
    logging.basicConfig(level=os.environ.get('SHIRASU_LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    if args.backup is not None or args.verify_backup is not None:
        try:
            from shirasu.backup import create_backup, verify_backup
            from shirasu.inspection import format_backup
            if args.backup is not None:
                path = os.environ.get('SHIRASU_STATE_PATH', 'state/shirasu.sqlite3')
                summary = create_backup(path, args.backup)
            else:
                summary = verify_backup(args.verify_backup)
            if args.format == 'json':
                print(json.dumps(summary, ensure_ascii=True, indent=2))
            else:
                print(format_backup(summary, created=args.backup is not None))
            return 0
        except Exception as error:
            operation = '作成' if args.backup is not None else '検証'
            message = ' '.join(''.join(char if char.isprintable() else ' '
                                       for char in str(error)).split())
            print(f'バックアップを{operation}できません: {message}', file=sys.stderr)
            return 1
    if args.preview:
        return run_preview(args)
    if args.status or args.health or args.history or args.show:
        # Inspection commands never load config, start workers, or maintain state.
        try:
            path = os.environ.get('SHIRASU_STATE_PATH', 'state/shirasu.sqlite3')
            if args.show:
                from shirasu.inspection import format_event, prepare_event
                detail = prepare_event(read_event(path, args.show))
                if args.format == 'json':
                    # Preserve stored content exactly, escaping terminal controls.
                    print(json.dumps(detail, ensure_ascii=True, indent=2))
                else:
                    print(format_event(detail))
                return 0
            if args.history:
                from shirasu.inspection import format_history, prepare_history
                history = prepare_history(read_history(path, state=args.state, limit=args.limit or 20))
                if args.format == 'json':
                    print(json.dumps(history, ensure_ascii=False, indent=2))
                else:
                    print(format_history(history))
                return 0
            now = time.time()
            snapshot = read_status(path, now=now)
            ok = healthy(snapshot, now)
            if args.status:
                snapshot = dict(snapshot, healthy=ok)
                if args.format == 'text':
                    from shirasu.inspection import format_status
                    print(format_status(snapshot, now))
                else:
                    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            return 0 if args.status or ok else 1
        except Exception as error:
            print(f'保存状態を読み取れません: {error}', file=sys.stderr)
            return 1
    try:
        import config
        asyncio.run(service.run(service.Settings.load(config)))
        return 0
    except Exception:
        service.logger.exception('Shirasu stopped with an error')
        return 1

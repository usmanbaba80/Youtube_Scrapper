from __future__ import annotations

import argparse
import logging
import logging.handlers
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from app.config import PROJECT_ROOT, load_settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.pipeline import (
    print_status,
    run_all,
    run_download,
    run_export_json,
    run_load_excel,
    run_metadata,
    run_reconcile_bunny,
    run_scrape,
    run_thumbnails,
    run_transfer,
    run_upload,
)
from app.utils import parse_channel_ids, parse_media_types


_LOG_MAX_BYTES = 20 * 1024 * 1024
_LOW_DISK_BYTES = 2 * 1024 * 1024 * 1024


def _oserror_is_full(exc: OSError) -> bool:
    if getattr(exc, "errno", None) in {28, 112}:
        return True
    return "no space left" in str(exc).lower()


class _DiskSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Drop a log line when the disk is full instead of dumping a logging traceback."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except OSError as exc:
            if not _oserror_is_full(exc):
                raise


class _DiskSafeStreamHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except OSError as exc:
            if not _oserror_is_full(exc):
                raise


def _keep_log_tail(path: Path, keep: int) -> None:
    """Shrink a huge log in place. Reading the tail first works even at 0 bytes free."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - keep))
        tail = handle.read(keep)
    newline = tail.find(b"\n")
    if newline != -1 and newline < len(tail) - 1:
        tail = tail[newline + 1 :]
    with path.open("wb") as handle:
        handle.write(tail)
        handle.truncate()


def _reclaim_log_space(logs_dir: Path, log_path: Path) -> None:
    """A full disk makes every later log line throw Errno 28. Free the log first."""
    try:
        free = shutil.disk_usage(logs_dir).free
    except OSError:
        return
    if free >= _LOW_DISK_BYTES:
        return
    try:
        if log_path.is_file() and log_path.stat().st_size > _LOG_MAX_BYTES:
            _keep_log_tail(log_path, _LOG_MAX_BYTES)
        for backup in logs_dir.glob("pipeline.log.*"):
            backup.unlink(missing_ok=True)
    except OSError as exc:
        try:
            print(f"Could not shrink {log_path}: {exc}", file=sys.stderr)
        except OSError:
            pass
        return
    try:
        print(
            f"Disk had under 2 GiB free. Shrunk {log_path.name} so logging can continue. "
            "Also clear temp_downloads/ if downloads still fail.",
            file=sys.stderr,
        )
    except OSError:
        pass


def setup_logging() -> None:
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "pipeline.log"
    _reclaim_log_space(logs_dir, log_path)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            _DiskSafeStreamHandler(),
            _DiskSafeRotatingFileHandler(
                log_path,
                maxBytes=_LOG_MAX_BYTES,
                backupCount=2,
                encoding="utf-8",
            ),
        ],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape Popular YouTube videos, fetch metadata, download by channel, upload to Bunny."
    )
    parser.add_argument(
        "command",
        choices=[
            "load-excel",
            "scrape",
            "metadata",
            "download",
            "upload",
            "transfer",
            "thumbnails",
            "reconcile-bunny",
            "export-json",
            "run",
            "status",
        ],
        help="Pipeline stage to run",
    )
    parser.add_argument("--excel", type=Path, help="Override Excel workbook path")
    parser.add_argument(
        "--channel-id",
        type=str,
        help=(
            "Only process these creators.id values (DB row PKs). "
            "One id or comma/space-separated list, e.g. 3 or 3,4,5"
        ),
    )
    parser.add_argument(
        "--media",
        type=str,
        help=(
            "For scrape, metadata, transfer, thumbnails, and reconcile-bunny: only these "
            "media types — videos, shorts, playlists, podcasts (comma-separated). Default: all. "
            "Category 5 (Podcasts - PodVerse) uses podcasts instead of playlists. "
            "Examples: --media shorts or --media videos,playlists"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="For export-json: folder to write creator JSON packs (default data/exports)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry channels/videos that previously failed",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-scrape channels; re-transfer videos already on Bunny; "
            "re-upload thumbnails. With metadata, refetch one creator: "
            "metadata --channel-id ID --force"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="For reconcile-bunny: list matches / duplicate deletes without changing DB or Bunny",
    )
    return parser


def main() -> None:
    setup_logging()
    args = build_parser().parse_args()
    settings = load_settings()
    if args.excel:
        excel = args.excel if args.excel.is_absolute() else PROJECT_ROOT / args.excel
        settings = replace(settings, excel_path=excel)

    try:
        channel_ids = parse_channel_ids(args.channel_id)
        media_types = parse_media_types(args.media)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if media_types is not None and args.command not in {
        "scrape",
        "metadata",
        "transfer",
        "thumbnails",
        "reconcile-bunny",
        "run",
    }:
        print(
            "error: --media is only supported for scrape, metadata, transfer, thumbnails, reconcile-bunny, and run",
            file=sys.stderr,
        )
        raise SystemExit(2)

    engine = make_engine(settings)
    init_db(engine)
    factory = make_session_factory(engine)

    if args.command == "run":
        run_all(
            factory,
            settings,
            retry_failed=args.retry_failed,
            force=args.force,
            channel_ids=channel_ids,
            media_types=media_types,
        )
        return

    with session_scope(factory) as session:
        if args.command == "load-excel":
            run_load_excel(session, settings)
        elif args.command == "scrape":
            run_scrape(
                session,
                settings,
                retry_failed=args.retry_failed,
                force=args.force,
                channel_ids=channel_ids,
                media_types=media_types,
            )
        elif args.command == "metadata":
            if args.force and not channel_ids:
                print(
                    "error: metadata --force requires --channel-id (creators.id)",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            run_metadata(
                session,
                settings,
                retry_failed=args.retry_failed,
                force=args.force,
                channel_ids=channel_ids,
                media_types=media_types,
            )
        elif args.command == "download":
            run_download(
                session,
                settings,
                retry_failed=args.retry_failed,
                channel_ids=channel_ids,
            )
        elif args.command == "upload":
            run_upload(
                session,
                settings,
                retry_failed=args.retry_failed,
                channel_ids=channel_ids,
            )
        elif args.command == "transfer":
            run_transfer(
                session,
                settings,
                retry_failed=args.retry_failed,
                force=args.force,
                channel_ids=channel_ids,
                media_types=media_types,
            )
        elif args.command == "thumbnails":
            run_thumbnails(
                session,
                settings,
                channel_ids=channel_ids,
                media_types=media_types,
                retry_failed=args.retry_failed,
                force=args.force,
            )
        elif args.command == "reconcile-bunny":
            run_reconcile_bunny(
                session,
                settings,
                channel_ids=channel_ids,
                media_types=media_types,
                dry_run=args.dry_run,
            )
        elif args.command == "export-json":
            out = args.output_dir
            if out is not None and not out.is_absolute():
                out = PROJECT_ROOT / out
            run_export_json(
                session,
                settings,
                channel_ids=channel_ids,
                output_dir=out,
            )
        elif args.command == "status":
            print_status(session)


if __name__ == "__main__":
    main()

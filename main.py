from __future__ import annotations

import argparse
import logging
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
    run_scrape,
    run_thumbnails,
    run_transfer,
    run_upload,
)
from app.utils import parse_channel_ids, parse_media_types


def setup_logging() -> None:
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(logs_dir / "pipeline.log", encoding="utf-8"),
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
            "For scrape/transfer/thumbnails: only these media types — videos, shorts, playlists "
            "(comma-separated). Default: all. Examples: --media shorts "
            "or --media videos,playlists"
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
            "Re-scrape channels; re-transfer videos already on Bunny (download+upload again); "
            "re-upload thumbnails even if already present"
        ),
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
        "transfer",
        "thumbnails",
        "run",
    }:
        print(
            "error: --media is only supported for scrape, transfer, thumbnails, and run",
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
            run_metadata(session, settings, retry_failed=args.retry_failed)
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

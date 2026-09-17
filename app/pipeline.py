from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.excel import load_excel
from app.export_json import export_jsons
from app.metadata import fetch_all_metadata
from app.models import Category, Creator, Playlist, PlaylistItem, Short, Video
from app.scraper import scrape_all_creators
from app.thumbnails import transfer_thumbnails
from app.transfer import download_videos, transfer_videos, upload_videos

log = logging.getLogger(__name__)


def run_export_json(session: Session, settings: Settings, **kwargs) -> None:
    paths = export_jsons(session, settings, **kwargs)
    log.info("Export wrote %s creator folder(s)", len(paths))


def run_load_excel(session: Session, settings: Settings) -> None:
    categories, creators = load_excel(session, settings.excel_path)
    log.info("Excel loaded: %s categories, %s new creators", categories, creators)


def run_scrape(session: Session, settings: Settings, **kwargs) -> None:
    scraped, failed = scrape_all_creators(session, settings, **kwargs)
    log.info("Scrape finished: %s creators ok, %s failed", scraped, failed)


def run_metadata(session: Session, settings: Settings, **kwargs) -> None:
    updated, missing = fetch_all_metadata(session, settings, **kwargs)
    log.info("Metadata finished: %s updated, %s missing", updated, missing)


def run_download(session: Session, settings: Settings, **kwargs) -> None:
    downloaded, failed = download_videos(session, settings, **kwargs)
    log.info("Download finished: %s downloaded, %s failed", downloaded, failed)


def run_upload(session: Session, settings: Settings, **kwargs) -> None:
    uploaded, failed = upload_videos(session, settings, **kwargs)
    log.info("Upload finished: %s uploaded, %s failed", uploaded, failed)


def run_transfer(session: Session, settings: Settings, **kwargs) -> None:
    uploaded, failed, _skipped = transfer_videos(session, settings, **kwargs)
    remaining = (
        session.query(func.count(Video.id))
        .filter(Video.is_short.is_(False), Video.transfer_status == "failed")
        .scalar()
        or 0
    )
    still_pending = (
        session.query(func.count(Video.id))
        .filter(
            Video.is_short.is_(False),
            Video.transfer_status.in_(["pending", "downloaded", "downloading", "uploading"]),
        )
        .scalar()
        or 0
    )
    log.info(
        "Transfer finished: %s uploaded (download→upload→delete), %s failed this run; "
        "still not on Bunny: %s failed + %s pending/leftover",
        uploaded,
        failed,
        remaining,
        still_pending,
    )
    if remaining or still_pending:
        log.info(
            "Run again later with: python main.py transfer --retry-failed "
            "(after YouTube rate-limit cools down)"
        )
    # Fill any missing Storage thumbnails (videos/shorts/playlists/playlist-items).
    # Skips rows that already have bunny_thumbnail_url.
    t_up, t_fail, t_skip = transfer_thumbnails(
        session,
        settings,
        channel_id=kwargs.get("channel_id"),
        force=False,
    )
    log.info(
        "Thumbnail backfill after transfer: %s uploaded, %s failed, %s already present",
        t_up,
        t_fail,
        t_skip,
    )


def run_thumbnails(session: Session, settings: Settings, **kwargs) -> None:
    uploaded, failed, skipped = transfer_thumbnails(session, settings, **kwargs)
    log.info(
        "Thumbnails finished: %s uploaded to Bunny Storage, %s failed, %s skipped",
        uploaded,
        failed,
        skipped,
    )


def run_all(
    factory: sessionmaker,
    settings: Settings,
    *,
    retry_failed: bool = False,
    force: bool = False,
    channel_id: int | None = None,
) -> None:
    with factory() as session:
        run_load_excel(session, settings)
        session.commit()
        run_scrape(
            session,
            settings,
            retry_failed=retry_failed,
            force=force,
            channel_id=channel_id,
        )
        session.commit()
        run_metadata(session, settings, retry_failed=retry_failed)
        session.commit()
        run_transfer(
            session,
            settings,
            retry_failed=retry_failed,
            channel_id=channel_id,
        )
        session.commit()
        run_thumbnails(
            session,
            settings,
            channel_id=channel_id,
            force=False,
        )


def _count_by(session: Session, model, column) -> list[tuple]:
    return (
        session.query(column, func.count(model.id))
        .group_by(column)
        .order_by(column.asc())
        .all()
    )


def print_status(session: Session) -> None:
    categories = session.query(func.count(Category.id)).scalar() or 0
    creators = session.query(func.count(Creator.id)).scalar() or 0
    print(f"Categories: {categories}")
    print(f"Creators:   {creators}")
    print("  scrape pending:", session.query(func.count(Creator.id)).filter(Creator.scrape_status == "pending").scalar())
    print("  scrape done:   ", session.query(func.count(Creator.id)).filter(Creator.scrape_status == "done").scalar())
    print("  scrape failed: ", session.query(func.count(Creator.id)).filter(Creator.scrape_status == "failed").scalar())

    videos = session.query(func.count(Video.id)).scalar() or 0
    print(f"Videos:     {videos}")
    for status, count in _count_by(session, Video, Video.transfer_status):
        print(f"  transfer {status or '(null)'}: {count}")
    print(
        "  metadata pending:",
        session.query(func.count(Video.id)).filter(Video.metadata_status == "pending").scalar(),
    )
    print(
        "  metadata fetched:",
        session.query(func.count(Video.id)).filter(Video.metadata_status == "fetched").scalar(),
    )
    print(
        "  bunny thumbnails:",
        session.query(func.count(Video.id))
        .filter(Video.bunny_thumbnail_url.isnot(None), Video.bunny_thumbnail_url != "")
        .scalar(),
    )

    shorts = session.query(func.count(Short.id)).scalar() or 0
    print(f"Shorts:     {shorts}")
    for status, count in _count_by(session, Short, Short.transfer_status):
        print(f"  transfer {status or '(null)'}: {count}")
    print(
        "  metadata pending:",
        session.query(func.count(Short.id)).filter(Short.metadata_status == "pending").scalar(),
    )
    print(
        "  metadata fetched:",
        session.query(func.count(Short.id)).filter(Short.metadata_status == "fetched").scalar(),
    )
    print(
        "  bunny thumbnails:",
        session.query(func.count(Short.id))
        .filter(Short.bunny_thumbnail_url.isnot(None), Short.bunny_thumbnail_url != "")
        .scalar(),
    )

    playlists = session.query(func.count(Playlist.id)).scalar() or 0
    items = session.query(func.count(PlaylistItem.id)).scalar() or 0
    print(f"Playlists:  {playlists}")
    print(f"Playlist items: {items}")
    print(
        "  items linked to videos:",
        session.query(func.count(PlaylistItem.id)).filter(PlaylistItem.reuse_source == "video").scalar(),
    )
    print(
        "  items linked to shorts:",
        session.query(func.count(PlaylistItem.id)).filter(PlaylistItem.reuse_source == "short").scalar(),
    )
    print(
        "  items unlinked (metadata only):",
        session.query(func.count(PlaylistItem.id)).filter(PlaylistItem.reuse_source == "none").scalar(),
    )
    print(
        "  playlist metadata pending:",
        session.query(func.count(Playlist.id)).filter(Playlist.metadata_status == "pending").scalar(),
    )
    print(
        "  playlist metadata fetched:",
        session.query(func.count(Playlist.id)).filter(Playlist.metadata_status == "fetched").scalar(),
    )

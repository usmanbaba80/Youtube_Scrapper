from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.excel import load_excel
from app.metadata import fetch_all_metadata
from app.models import Category, Creator, Video
from app.scraper import scrape_all_creators
from app.transfer import download_videos, transfer_videos, upload_videos

log = logging.getLogger(__name__)


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
    log.info(
        "Transfer finished: %s uploaded (download→upload→delete), %s failed",
        uploaded,
        failed,
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


def print_status(session: Session) -> None:
    categories = session.query(func.count(Category.id)).scalar() or 0
    creators = session.query(func.count(Creator.id)).scalar() or 0
    videos = session.query(func.count(Video.id)).scalar() or 0
    print(f"Categories: {categories}")
    print(f"Creators:   {creators}")
    print("  scrape pending:", session.query(func.count(Creator.id)).filter(Creator.scrape_status == "pending").scalar())
    print("  scrape done:   ", session.query(func.count(Creator.id)).filter(Creator.scrape_status == "done").scalar())
    print("  scrape failed: ", session.query(func.count(Creator.id)).filter(Creator.scrape_status == "failed").scalar())
    print(f"Videos:     {videos}")
    print("  shorts skipped:", session.query(func.count(Video.id)).filter(Video.is_short.is_(True)).scalar())
    print("  metadata pending:", session.query(func.count(Video.id)).filter(Video.metadata_status == "pending").scalar())
    print("  metadata fetched:", session.query(func.count(Video.id)).filter(Video.metadata_status == "fetched").scalar())
    print("  download pending:", session.query(func.count(Video.id)).filter(Video.transfer_status == "pending").scalar())
    print("  downloaded:     ", session.query(func.count(Video.id)).filter(Video.transfer_status == "downloaded").scalar())
    print("  uploaded:       ", session.query(func.count(Video.id)).filter(Video.transfer_status == "uploaded").scalar())
    print("  transfer failed:", session.query(func.count(Video.id)).filter(Video.transfer_status == "failed").scalar())
    print("  skipped:        ", session.query(func.count(Video.id)).filter(Video.transfer_status == "skipped").scalar())

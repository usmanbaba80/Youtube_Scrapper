from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Creator, Video
from app.popular import fetch_popular_videos
from app.utils import build_video_id, channel_handle, utcnow

log = logging.getLogger(__name__)


def scrape_creator_popular_videos(settings: Settings, creator: Creator) -> list[dict]:
    log.info(
        "Scraping Popular videos for %s (creator_id=%s, %s)",
        creator.name,
        creator.creator_id,
        creator.channel_url,
    )
    return fetch_popular_videos(
        creator.channel_url,
        # video_id uses a fixed 3-digit serial (001..999)
        limit=min(settings.max_videos_per_channel, 999),
    )


def scrape_all_creators(
    session: Session,
    settings: Settings,
    *,
    retry_failed: bool = False,
    force: bool = False,
    channel_id: int | None = None,
) -> tuple[int, int]:
    query = session.query(Creator)
    if channel_id is not None:
        # CLI --channel-id is the creators.id row PK (kept for compatibility)
        query = query.filter(Creator.id == channel_id)
    elif force:
        pass
    elif retry_failed:
        query = query.filter(Creator.scrape_status.in_(["pending", "failed"]))
    else:
        query = query.filter(Creator.scrape_status == "pending")

    creators = query.order_by(Creator.id.asc()).all()
    scraped = 0
    failed = 0

    for index, creator in enumerate(creators, start=1):
        creator.scrape_status = "scraping"
        creator.scrape_error = None
        session.commit()
        try:
            items = scrape_creator_popular_videos(settings, creator)
            if not items:
                raise RuntimeError("No long-form Popular videos found")

            first = items[0]
            if first.get("youtube_channel_id"):
                creator.youtube_channel_id = first["youtube_channel_id"]
            handle = first.get("handle") or channel_handle(creator.channel_url)
            if handle:
                creator.handle = handle

            existing = {
                video.youtube_video_id: video
                for video in session.query(Video).filter_by(creator_row_id=creator.id).all()
            }
            new_ids = {item["youtube_video_id"] for item in items}

            removed = 0
            for yt_id, video in list(existing.items()):
                if (
                    yt_id not in new_ids
                    and video.transfer_status
                    in {"pending", "failed", "skipped", "downloading", "uploading"}
                ):
                    session.delete(video)
                    del existing[yt_id]
                    removed += 1

            inserted = 0
            for rank, item in enumerate(items, start=1):
                try:
                    business_video_id = build_video_id(
                        creator.category_id,
                        creator.creator_id,
                        rank,
                    )
                except ValueError as exc:
                    raise RuntimeError(
                        f"Cannot assign video_id for {creator.name} rank={rank}: {exc}"
                    ) from exc

                video = existing.get(item["youtube_video_id"])
                if video is not None:
                    video.popular_rank = rank
                    video.category_id = creator.category_id
                    video.creator_id = creator.creator_id
                    video.video_id = business_video_id
                    video.url = item["url"]
                    if item.get("title") and not video.title:
                        video.title = item["title"]
                    continue
                session.add(
                    Video(
                        creator_row_id=creator.id,
                        category_id=creator.category_id,
                        creator_id=creator.creator_id,
                        video_id=business_video_id,
                        youtube_video_id=item["youtube_video_id"],
                        url=item["url"],
                        popular_rank=rank,
                        title=item.get("title"),
                        is_short=False,
                        metadata_status="pending",
                        transfer_status="pending",
                    )
                )
                inserted += 1

            creator.scrape_status = "done"
            creator.scraped_at = utcnow()
            session.commit()
            scraped += 1
            log.info(
                "[%s/%s] %s (creator_id=%s): stored %s Popular videos (%s new, %s removed)",
                index,
                len(creators),
                creator.name,
                creator.creator_id,
                len(items),
                inserted,
                removed,
            )
        except Exception as exc:
            session.rollback()
            # Re-load creator after rollback so we can mark it failed.
            creator = session.get(Creator, creator.id)
            if creator is not None:
                creator.scrape_status = "failed"
                creator.scrape_error = str(exc)[:2000]
                session.commit()
            failed += 1
            log.exception("Failed scraping %s", creator.name if creator else "?")

        if index < len(creators) and settings.scrape_delay_seconds:
            time.sleep(settings.scrape_delay_seconds)

    return scraped, failed


# Backward-compatible alias
scrape_all_channels = scrape_all_creators

from __future__ import annotations

import json
import logging
import time

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Creator, Playlist, PlaylistItem, Short, Video
from app.playlists import fetch_channel_playlists, fetch_playlist_items
from app.popular import (
    detect_channel_tabs_and_id,
    fetch_channel_shorts,
    fetch_popular_videos,
)
from app.utils import (
    normalize_channel_ids,
    build_playlist_id,
    build_short_id,
    build_video_id,
    channel_handle,
    utcnow,
)

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
        limit=min(settings.max_videos_per_channel, 999),
    )


def _store_popular_videos(
    session: Session,
    creator: Creator,
    items: list[dict],
) -> tuple[int, int]:
    existing = {
        video.youtube_video_id: video
        for video in session.query(Video).filter_by(creator_row_id=creator.id).all()
    }
    new_ids = {item["youtube_video_id"] for item in items}
    claimed_ids = {
        build_video_id(creator.category_id, creator.creator_id, rank)
        for rank in range(1, len(items) + 1)
    }

    removed = 0
    for yt_id, video in list(existing.items()):
        if yt_id in new_ids:
            continue
        if video.transfer_status in {
            "pending",
            "failed",
            "skipped",
            "downloading",
            "uploading",
        }:
            session.delete(video)
            del existing[yt_id]
            removed += 1
        elif video.video_id in claimed_ids:
            # Keep uploaded/downloaded orphans, but free the business id.
            video.video_id = f"legacy-{video.id}-{video.youtube_video_id}"

    # Phase 1: only rows that will be re-ranked — avoid unique collisions on swap.
    for yt_id, video in existing.items():
        if yt_id in new_ids:
            video.video_id = f"tmp-v-{video.id}-{video.youtube_video_id}"
    session.flush()

    inserted = 0
    for rank, item in enumerate(items, start=1):
        business_video_id = build_video_id(
            creator.category_id,
            creator.creator_id,
            rank,
        )
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
    session.flush()
    return inserted, removed


def _purge_all_creator_shorts(session: Session, creator: Creator) -> int:
    """
    Delete every Short row for this creator (including uploaded/downloaded).

    Used when the channel has no Shorts tab — prior scrapes may have stored
    long-form videos as fake shorts. Unlinks playlist_items that pointed at them.
    Does not delete files already on Bunny Stream.
    """
    shorts = (
        session.query(Short).filter(Short.creator_row_id == creator.id).all()
    )
    if not shorts:
        return 0
    short_ids = [s.id for s in shorts]
    for item in (
        session.query(PlaylistItem)
        .filter(PlaylistItem.short_row_id.in_(short_ids))
        .all()
    ):
        item.short_row_id = None
        if item.reuse_source == "short":
            item.reuse_source = "none"
            if item.transfer_status == "skipped":
                item.transfer_status = "pending"
    removed = 0
    for short in shorts:
        log.warning(
            "Purging short %s (status=%s) — channel has no Shorts tab: %s",
            short.youtube_video_id,
            short.transfer_status,
            (short.title or "")[:80],
        )
        session.delete(short)
        removed += 1
    session.flush()
    return removed


def _store_shorts(
    session: Session,
    creator: Creator,
    items: list[dict],
) -> tuple[int, int]:
    existing = {
        short.youtube_video_id: short
        for short in session.query(Short).filter_by(creator_row_id=creator.id).all()
    }
    new_ids = {item["youtube_video_id"] for item in items}
    claimed_ids = {
        build_short_id(creator.category_id, creator.creator_id, rank)
        for rank in range(1, len(items) + 1)
    }

    removed = 0
    for yt_id, short in list(existing.items()):
        if yt_id in new_ids:
            continue
        if short.transfer_status in {
            "pending",
            "failed",
            "skipped",
            "downloading",
            "uploading",
        }:
            session.delete(short)
            del existing[yt_id]
            removed += 1
        elif short.short_id in claimed_ids:
            short.short_id = f"legacy-{short.id}-{short.youtube_video_id}"

    for yt_id, short in existing.items():
        if yt_id in new_ids:
            short.short_id = f"tmp-s-{short.id}-{short.youtube_video_id}"
    session.flush()

    inserted = 0
    for rank, item in enumerate(items, start=1):
        business_id = build_short_id(creator.category_id, creator.creator_id, rank)
        short = existing.get(item["youtube_video_id"])
        if short is not None:
            short.shorts_rank = rank
            short.category_id = creator.category_id
            short.creator_id = creator.creator_id
            short.short_id = business_id
            short.url = item["url"]
            if item.get("title") and not short.title:
                short.title = item["title"]
            continue
        session.add(
            Short(
                creator_row_id=creator.id,
                category_id=creator.category_id,
                creator_id=creator.creator_id,
                short_id=business_id,
                youtube_video_id=item["youtube_video_id"],
                url=item["url"],
                shorts_rank=rank,
                title=item.get("title"),
                metadata_status="pending",
                transfer_status="pending",
            )
        )
        inserted += 1
    session.flush()
    return inserted, removed


def _link_playlist_item(
    *,
    videos_by_yt: dict[str, Video],
    shorts_by_yt: dict[str, Short],
    youtube_video_id: str,
) -> tuple[str, int | None, int | None, str]:
    """
    Return (reuse_source, video_row_id, short_row_id, transfer_status).

    Prefer long-form videos, then shorts. Linked items skip transfer.
    """
    video = videos_by_yt.get(youtube_video_id)
    if video is not None:
        return "video", video.id, None, "skipped"
    short = shorts_by_yt.get(youtube_video_id)
    if short is not None:
        return "short", None, short.id, "skipped"
    return "none", None, None, "skipped"


def _store_playlists(
    session: Session,
    settings: Settings,
    creator: Creator,
    playlist_rows: list[dict],
) -> tuple[int, int, int]:
    """
    Upsert playlists + items. Items already in videos/shorts are linked (reuse).
    Returns (playlists_upserted, items_linked, items_unlinked).
    """
    videos_by_yt = {
        v.youtube_video_id: v
        for v in session.query(Video).filter_by(creator_row_id=creator.id).all()
    }
    shorts_by_yt = {
        s.youtube_video_id: s
        for s in session.query(Short).filter_by(creator_row_id=creator.id).all()
    }

    existing = {
        p.youtube_playlist_id: p
        for p in session.query(Playlist).filter_by(creator_row_id=creator.id).all()
    }
    keep_ids = {row["youtube_playlist_id"] for row in playlist_rows}

    for yt_id, playlist in list(existing.items()):
        if yt_id not in keep_ids:
            session.delete(playlist)
            del existing[yt_id]

    claimed_ids = {
        build_playlist_id(creator.category_id, creator.creator_id, rank)
        for rank in range(1, len(playlist_rows) + 1)
    }
    # Avoid playlist_id unique collisions when ranks shuffle.
    for playlist in existing.values():
        if playlist.playlist_id in claimed_ids or playlist.youtube_playlist_id in keep_ids:
            playlist.playlist_id = f"tmp-p-{playlist.id}-{playlist.youtube_playlist_id}"
    session.flush()

    upserted = 0
    linked = 0
    unlinked = 0

    for rank, row in enumerate(playlist_rows, start=1):
        business_id = build_playlist_id(creator.category_id, creator.creator_id, rank)
        playlist = existing.get(row["youtube_playlist_id"])
        if playlist is None:
            playlist = Playlist(
                creator_row_id=creator.id,
                category_id=creator.category_id,
                creator_id=creator.creator_id,
                playlist_id=business_id,
                youtube_playlist_id=row["youtube_playlist_id"],
                url=row["url"],
                playlist_rank=rank,
                title=row.get("title"),
                description=row.get("description"),
                published_at=row.get("published_at"),
                item_count=row.get("item_count"),
                thumbnail_url=row.get("thumbnail_url"),
                metadata_json=json.dumps(row.get("raw"), ensure_ascii=False)
                if row.get("raw")
                else None,
                metadata_status="fetched" if row.get("raw") else "pending",
                metadata_fetched_at=utcnow() if row.get("raw") else None,
            )
            session.add(playlist)
            session.flush()
            existing[row["youtube_playlist_id"]] = playlist
            upserted += 1
        else:
            playlist.playlist_rank = rank
            playlist.playlist_id = business_id
            playlist.category_id = creator.category_id
            playlist.creator_id = creator.creator_id
            playlist.url = row["url"]
            playlist.title = row.get("title") or playlist.title
            playlist.description = row.get("description")
            playlist.published_at = row.get("published_at")
            playlist.item_count = row.get("item_count")
            playlist.thumbnail_url = row.get("thumbnail_url") or playlist.thumbnail_url
            if row.get("raw"):
                playlist.metadata_json = json.dumps(row["raw"], ensure_ascii=False)
                playlist.metadata_status = "fetched"
                playlist.metadata_fetched_at = utcnow()
                playlist.metadata_error = None
            upserted += 1

        log.info(
            "Storing playlist %s/%s: %r (%s items reported)",
            rank,
            len(playlist_rows),
            row.get("title"),
            row.get("item_count"),
        )

        # Replace items for a clean position map
        session.query(PlaylistItem).filter_by(playlist_row_id=playlist.id).delete()
        session.flush()

        try:
            entries = fetch_playlist_items(
                settings.youtube_api_key,
                row["youtube_playlist_id"],
                limit=settings.max_playlist_items,
                title=row.get("title"),
            )
        except Exception:
            log.exception(
                "Failed fetching items for playlist %s (%s)",
                row.get("title"),
                row["youtube_playlist_id"],
            )
            continue

        seen_yt: set[str] = set()
        position = 0
        for entry in entries:
            yt_id = entry["youtube_video_id"]
            # YouTube playlists may list the same video more than once — keep first.
            if yt_id in seen_yt:
                continue
            seen_yt.add(yt_id)
            position += 1
            reuse_source, video_row_id, short_row_id, transfer_status = _link_playlist_item(
                videos_by_yt=videos_by_yt,
                shorts_by_yt=shorts_by_yt,
                youtube_video_id=yt_id,
            )
            if reuse_source == "none":
                unlinked += 1
                meta_status = "pending"
                transfer_status = "pending"
                transfer_error = "Playlist-only item; upload to Creator/playlists"
            else:
                linked += 1
                meta_status = "reused"
                transfer_status = "skipped"
                transfer_error = f"Reuses {reuse_source} in Creator/{reuse_source}s; skip re-upload"

            session.add(
                PlaylistItem(
                    playlist_row_id=playlist.id,
                    creator_row_id=creator.id,
                    position=position,
                    youtube_video_id=yt_id,
                    url=entry["url"],
                    video_row_id=video_row_id,
                    short_row_id=short_row_id,
                    reuse_source=reuse_source,
                    title=entry.get("title"),
                    description=entry.get("description"),
                    published_at=entry.get("published_at"),
                    thumbnail_url=entry.get("thumbnail_url"),
                    metadata_status=meta_status,
                    transfer_status=transfer_status,
                    transfer_error=transfer_error,
                )
            )

        playlist.item_count = position

    return upserted, linked, unlinked


def scrape_all_creators(
    session: Session,
    settings: Settings,
    *,
    retry_failed: bool = False,
    force: bool = False,
    channel_ids: list[int] | None = None,
    channel_id: int | None = None,
    media_types: frozenset[str] | set[str] | list[str] | str | None = None,
) -> tuple[int, int]:
    from app.utils import parse_media_types

    channel_ids = normalize_channel_ids(channel_ids, channel_id=channel_id)
    if isinstance(media_types, str):
        media_types = parse_media_types(media_types)
    elif media_types is not None:
        media_types = frozenset(media_types)
    want_videos = media_types is None or "videos" in media_types
    want_shorts = media_types is None or "shorts" in media_types
    want_playlists = media_types is None or "playlists" in media_types
    log.info(
        "Scrape media filter: %s",
        "all" if media_types is None else ",".join(sorted(media_types)),
    )

    query = session.query(Creator)
    if channel_ids is not None:
        query = query.filter(Creator.id.in_(channel_ids))
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
        creator_pk = creator.id
        creator_label = creator.name
        creator.scrape_status = "scraping"
        creator.scrape_error = None
        session.commit()
        try:
            creator = session.get(Creator, creator_pk)
            if creator is None:
                failed += 1
                continue

            tabs, yt_channel_id, tab_handle = detect_channel_tabs_and_id(creator.channel_url)
            if yt_channel_id:
                creator.youtube_channel_id = creator.youtube_channel_id or yt_channel_id
            if tab_handle:
                creator.handle = creator.handle or tab_handle
            log.info(
                "Scraping %s with tabs=%s",
                creator_label,
                ",".join(sorted(tabs)) or "(none)",
            )

            # --- Videos ---
            items: list[dict] = []
            v_ins = v_rem = 0
            if want_videos and "videos" in tabs:
                items = scrape_creator_popular_videos(settings, creator)
                if not items:
                    log.warning(
                        "Videos tab present for %s but Popular/Videos listing was empty",
                        creator_label,
                    )
                else:
                    first = items[0]
                    if first.get("youtube_channel_id"):
                        creator.youtube_channel_id = first["youtube_channel_id"]
                    handle = first.get("handle") or channel_handle(creator.channel_url)
                    if handle:
                        creator.handle = handle
                    v_ins, v_rem = _store_popular_videos(session, creator, items)
                    session.commit()
                    creator = session.get(Creator, creator_pk)
            elif want_videos:
                log.info("Skipping videos for %s — no Videos tab on channel", creator_label)
            else:
                log.info("Skipping videos for %s — media filter", creator_label)

            if creator is None:
                failed += 1
                continue

            # --- Shorts ---
            s_ins = s_rem = 0
            shorts_count = 0
            if want_shorts and settings.max_shorts_per_channel > 0 and "shorts" in tabs:
                try:
                    short_items = fetch_channel_shorts(
                        creator.channel_url,
                        limit=settings.max_shorts_per_channel,
                        tabs=tabs,
                    )
                    shorts_count = len(short_items)
                    s_ins, s_rem = _store_shorts(session, creator, short_items)
                    if short_items and short_items[0].get("youtube_channel_id"):
                        creator.youtube_channel_id = (
                            creator.youtube_channel_id
                            or short_items[0]["youtube_channel_id"]
                        )
                    session.commit()
                    creator = session.get(Creator, creator_pk)
                except Exception:
                    session.rollback()
                    creator = session.get(Creator, creator_pk)
                    log.exception(
                        "Shorts scrape failed for %s (continuing)",
                        creator_label,
                    )
            elif want_shorts and settings.max_shorts_per_channel > 0:
                log.info(
                    "No Shorts tab for %s — deleting all rows in shorts table "
                    "(including uploaded/downloaded)",
                    creator_label,
                )
                try:
                    s_rem = _purge_all_creator_shorts(session, creator)
                    session.commit()
                    creator = session.get(Creator, creator_pk)
                except Exception:
                    session.rollback()
                    creator = session.get(Creator, creator_pk)
                    log.exception(
                        "Failed purging shorts for %s (continuing)",
                        creator_label,
                    )
            elif want_shorts:
                log.info("Skipping shorts for %s — MAX_SHORTS_PER_CHANNEL=0", creator_label)
            else:
                log.info("Skipping shorts for %s — media filter", creator_label)

            if creator is None:
                failed += 1
                continue

            # --- Playlists ---
            p_up = linked = unlinked = 0
            if want_playlists and settings.max_playlists_per_channel > 0 and "playlists" in tabs:
                if not settings.youtube_api_key:
                    log.warning(
                        "Skipping playlists for %s — YOUTUBE_API_KEY missing",
                        creator_label,
                    )
                elif not creator.youtube_channel_id:
                    log.warning(
                        "Skipping playlists for %s — youtube_channel_id unknown",
                        creator_label,
                    )
                else:
                    try:
                        playlist_rows = fetch_channel_playlists(
                            settings.youtube_api_key,
                            creator.youtube_channel_id,
                            limit=settings.max_playlists_per_channel,
                        )
                        p_up, linked, unlinked = _store_playlists(
                            session, settings, creator, playlist_rows
                        )
                        session.commit()
                        creator = session.get(Creator, creator_pk)
                    except Exception:
                        session.rollback()
                        creator = session.get(Creator, creator_pk)
                        log.exception(
                            "Playlist scrape failed for %s (continuing)",
                            creator_label,
                        )
            elif want_playlists and settings.max_playlists_per_channel > 0:
                log.info(
                    "Skipping playlists for %s — no Playlists tab on channel",
                    creator_label,
                )
            elif want_playlists:
                log.info(
                    "Skipping playlists for %s — MAX_PLAYLISTS_PER_CHANNEL=0",
                    creator_label,
                )
            else:
                log.info("Skipping playlists for %s — media filter", creator_label)

            if creator is None:
                failed += 1
                continue

            if media_types is None and not items and shorts_count == 0 and p_up == 0:
                raise RuntimeError(
                    "Nothing scraped: channel has no usable Videos/Shorts/Playlists content"
                )

            creator.scrape_status = "done"
            creator.scraped_at = utcnow()
            session.commit()
            scraped += 1
            log.info(
                "[%s/%s] %s (creator_id=%s): tabs=%s; videos=%s (+%s/-%s), shorts=%s (+%s/-%s), "
                "playlists=%s (items linked=%s, unlinked=%s)",
                index,
                len(creators),
                creator_label,
                creator.creator_id,
                ",".join(sorted(tabs)) or "-",
                len(items),
                v_ins,
                v_rem,
                shorts_count,
                s_ins,
                s_rem,
                p_up,
                linked,
                unlinked,
            )
        except Exception as exc:
            session.rollback()
            creator = session.get(Creator, creator_pk)
            if creator is not None:
                creator.scrape_status = "failed"
                creator.scrape_error = str(exc)[:2000]
                session.commit()
            failed += 1
            log.exception("Failed scraping %s", creator_label)

        if index < len(creators) and settings.scrape_delay_seconds:
            time.sleep(settings.scrape_delay_seconds)

    return scraped, failed



# Backward-compatible alias
scrape_all_channels = scrape_all_creators

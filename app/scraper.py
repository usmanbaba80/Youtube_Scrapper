from __future__ import annotations

import json
import logging
import time

from sqlalchemy.orm import Session

from app.config import PODCAST_CATEGORY_ID, Settings
from app.models import Creator, Playlist, PlaylistItem, Podcast, PodcastEpisode, Short, Video
from app.playlists import fetch_channel_playlists, fetch_playlist_items
from app.podcasts import fetch_channel_podcasts
from app.popular import (
    detect_channel_tabs_and_id,
    fetch_channel_shorts,
    fetch_popular_videos,
)
from app.utils import (
    normalize_channel_ids,
    build_playlist_id,
    build_podcast_id,
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


def _clear_creator_playlists(session: Session, creator: Creator) -> tuple[int, int]:
    """Drop this creator's playlists and items. Podcasts - PodVerse does not use them."""
    items = (
        session.query(PlaylistItem)
        .filter(PlaylistItem.creator_row_id == creator.id)
        .delete(synchronize_session="fetch")
    )
    playlists = (
        session.query(Playlist)
        .filter(Playlist.creator_row_id == creator.id)
        .delete(synchronize_session="fetch")
    )
    session.flush()
    return int(playlists or 0), int(items or 0)


def _store_podcast_episodes(
    session: Session,
    settings: Settings,
    creator: Creator,
    podcast: Podcast,
    *,
    videos_by_yt: dict[str, Video],
    shorts_by_yt: dict[str, Short],
) -> tuple[int, int]:
    """
    Replace this show's episodes. At most ``max_podcast_episodes`` are stored.

    Returns (linked, unlinked). Linked episodes reuse an existing video or short.
    """
    if not settings.youtube_api_key:
        log.warning(
            "Skipping episodes for podcast %r — YOUTUBE_API_KEY missing",
            podcast.title,
        )
        return 0, 0

    episode_limit = settings.max_podcast_episodes
    entries = fetch_playlist_items(
        settings.youtube_api_key,
        podcast.youtube_podcast_id,
        limit=episode_limit,
        title=podcast.title,
    )
    entries = entries[:episode_limit]
    session.query(PodcastEpisode).filter_by(podcast_row_id=podcast.id).delete(
        synchronize_session="fetch"
    )
    session.flush()
    seen_yt: set[str] = set()
    position = 0
    linked = 0
    unlinked = 0
    for entry in entries:
        yt_id = entry["youtube_video_id"]
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
            transfer_error = "Podcast episode; upload to Creator/podcasts"
        else:
            linked += 1
            meta_status = "reused"
            transfer_status = "skipped"
            transfer_error = f"Reuses {reuse_source} in Creator/{reuse_source}s; skip re-upload"
        session.add(
            PodcastEpisode(
                podcast_row_id=podcast.id,
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
    return linked, unlinked


def _refresh_stored_podcast_episodes(
    session: Session,
    settings: Settings,
    creator: Creator,
) -> int:
    """
    Store episodes for podcast shows already in the database.

    Used when the channel page does not list a Podcasts tab, which happens
    when YouTube returns a consent or bot page. Each show keeps at most
    ``max_podcast_episodes`` episodes.
    """
    podcasts = (
        session.query(Podcast)
        .filter_by(creator_row_id=creator.id)
        .order_by(Podcast.podcast_rank.asc(), Podcast.id.asc())
        .all()
    )
    keep = podcasts[: settings.max_podcasts_per_channel]
    for podcast in podcasts[settings.max_podcasts_per_channel :]:
        session.delete(podcast)
    if not keep:
        return 0

    videos_by_yt = {
        video.youtube_video_id: video
        for video in session.query(Video).filter_by(creator_row_id=creator.id).all()
    }
    shorts_by_yt = {
        short.youtube_video_id: short
        for short in session.query(Short).filter_by(creator_row_id=creator.id).all()
    }
    log.info(
        "Refreshing episodes for %s stored podcast(s) on %s (max %s each)",
        len(keep),
        creator.name,
        settings.max_podcast_episodes,
    )
    stored = 0
    for podcast in keep:
        try:
            linked, unlinked = _store_podcast_episodes(
                session,
                settings,
                creator,
                podcast,
                videos_by_yt=videos_by_yt,
                shorts_by_yt=shorts_by_yt,
            )
        except Exception:
            log.exception(
                "Failed fetching episodes for podcast %s (%s)",
                podcast.title,
                podcast.youtube_podcast_id,
            )
            continue
        stored += 1
        log.info(
            "Stored episodes for %r: linked=%s unlinked=%s",
            podcast.title,
            linked,
            unlinked,
        )
    return stored


def _store_podcasts(
    session: Session,
    settings: Settings,
    creator: Creator,
    rows: list[dict],
) -> int:
    """Upsert podcast shows and their episodes. Returns how many shows were stored."""
    rows = rows[: settings.max_podcasts_per_channel]
    existing = {
        podcast.youtube_podcast_id: podcast
        for podcast in session.query(Podcast).filter_by(creator_row_id=creator.id).all()
    }
    keep_ids = {row["youtube_podcast_id"] for row in rows}
    for youtube_id, podcast in list(existing.items()):
        if youtube_id not in keep_ids:
            session.delete(podcast)
            del existing[youtube_id]

    claimed_ids = {
        build_podcast_id(creator.category_id, creator.creator_id, rank)
        for rank in range(1, len(rows) + 1)
    }
    for podcast in existing.values():
        if podcast.podcast_id in claimed_ids or podcast.youtube_podcast_id in keep_ids:
            podcast.podcast_id = f"tmp-d-{podcast.id}"
    session.flush()

    videos_by_yt = {
        video.youtube_video_id: video
        for video in session.query(Video).filter_by(creator_row_id=creator.id).all()
    }
    shorts_by_yt = {
        short.youtube_video_id: short
        for short in session.query(Short).filter_by(creator_row_id=creator.id).all()
    }

    stored = 0
    for rank, row in enumerate(rows, start=1):
        business_id = build_podcast_id(creator.category_id, creator.creator_id, rank)
        podcast = existing.get(row["youtube_podcast_id"])
        if podcast is None:
            podcast = Podcast(
                creator_row_id=creator.id,
                category_id=creator.category_id,
                creator_id=creator.creator_id,
                podcast_id=business_id,
                youtube_podcast_id=row["youtube_podcast_id"],
                url=row["url"],
                podcast_rank=rank,
                title=row.get("title"),
                episode_count=row.get("episode_count"),
                thumbnail_url=row.get("thumbnail_url"),
                metadata_status="pending",
            )
            session.add(podcast)
            session.flush()
            existing[row["youtube_podcast_id"]] = podcast
        else:
            podcast.podcast_rank = rank
            podcast.podcast_id = business_id
            podcast.category_id = creator.category_id
            podcast.creator_id = creator.creator_id
            podcast.url = row["url"]
            podcast.title = row.get("title") or podcast.title
            if row.get("episode_count") is not None:
                podcast.episode_count = row["episode_count"]
            podcast.thumbnail_url = row.get("thumbnail_url") or podcast.thumbnail_url
        stored += 1
        try:
            linked, unlinked = _store_podcast_episodes(
                session,
                settings,
                creator,
                podcast,
                videos_by_yt=videos_by_yt,
                shorts_by_yt=shorts_by_yt,
            )
        except Exception:
            log.exception(
                "Failed fetching episodes for podcast %s (%s)",
                row.get("title"),
                row["youtube_podcast_id"],
            )
            linked = unlinked = 0
        log.info(
            "Storing podcast %s/%s: %r (reported %s, stored linked=%s unlinked=%s)",
            rank,
            len(rows),
            row.get("title"),
            row.get("episode_count"),
            linked,
            unlinked,
        )
    return stored


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
    want_podcasts = media_types is None or "podcasts" in media_types
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

            # --- Playlists (every category except Podcasts - PodVerse) ---
            # Category 5 stores podcast shows instead, capped at 3 per channel.
            p_up = linked = unlinked = 0
            d_up = 0
            is_podcast_category = creator.category_id == PODCAST_CATEGORY_ID
            if is_podcast_category and (want_playlists or want_podcasts):
                log.info(
                    "Skipping playlists for %s — category %s uses podcasts",
                    creator_label,
                    PODCAST_CATEGORY_ID,
                )
            elif want_playlists and settings.max_playlists_per_channel > 0 and "playlists" in tabs:
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

            # --- Podcasts (Podcasts - PodVerse only, max 3 shows) ---
            if (
                is_podcast_category
                and want_podcasts
                and settings.max_podcasts_per_channel > 0
            ):
                try:
                    if "podcasts" in tabs:
                        podcast_rows = fetch_channel_podcasts(
                            creator.channel_url,
                            limit=settings.max_podcasts_per_channel,
                            tabs=tabs,
                        )
                        d_up = _store_podcasts(session, settings, creator, podcast_rows)
                    else:
                        if not tabs:
                            log.warning(
                                "No channel tabs detected for %s; "
                                "fetching episodes from stored podcasts",
                                creator_label,
                            )
                        d_up = _refresh_stored_podcast_episodes(
                            session, settings, creator
                        )
                        if d_up == 0:
                            log.info(
                                "Skipping podcasts for %s — no Podcasts tab and no stored shows",
                                creator_label,
                            )
                    removed_playlists, removed_items = _clear_creator_playlists(
                        session, creator
                    )
                    if removed_playlists or removed_items:
                        log.info(
                            "Removed playlists for %s after podcast scrape: "
                            "playlists=%s items=%s",
                            creator_label,
                            removed_playlists,
                            removed_items,
                        )
                    session.commit()
                    creator = session.get(Creator, creator_pk)
                except Exception:
                    session.rollback()
                    creator = session.get(Creator, creator_pk)
                    log.exception(
                        "Podcast scrape failed for %s (continuing)",
                        creator_label,
                    )
            elif is_podcast_category and want_podcasts:
                log.info(
                    "Skipping podcasts for %s — MAX_PODCASTS_PER_CHANNEL=0",
                    creator_label,
                )
            elif is_podcast_category:
                log.info("Skipping podcasts for %s — media filter", creator_label)
            elif want_podcasts and media_types is not None:
                log.info(
                    "Skipping podcasts for %s — only category %s uses podcasts",
                    creator_label,
                    PODCAST_CATEGORY_ID,
                )

            if creator is None:
                failed += 1
                continue

            if media_types is None and not items and shorts_count == 0 and p_up == 0 and d_up == 0:
                raise RuntimeError(
                    "Nothing scraped: channel has no usable Videos/Shorts/Playlists/Podcasts content"
                )

            creator.scrape_status = "done"
            creator.scraped_at = utcnow()
            session.commit()
            scraped += 1
            log.info(
                "[%s/%s] %s (creator_id=%s): tabs=%s; videos=%s (+%s/-%s), shorts=%s (+%s/-%s), "
                "playlists=%s (items linked=%s, unlinked=%s), podcasts=%s",
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
                d_up,
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

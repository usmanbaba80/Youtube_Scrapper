from __future__ import annotations

import json
import logging
import time

import requests
from sqlalchemy.orm import Session, joinedload

from app.config import PODCAST_CATEGORY_ID, Settings
from app.models import Creator, Playlist, PlaylistItem, Podcast, PodcastEpisode, Short, Video
from app.utils import normalize_channel_ids, parse_iso8601_duration, parse_media_types, utcnow

log = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/youtube/v3/videos"
PLAYLISTS_API_URL = "https://www.googleapis.com/youtube/v3/playlists"
BATCH_SIZE = 50
PARTS = "snippet,contentDetails,statistics,status,localizations"
# Ask for English in snippet.localized. The original title stays in snippet.title.
METADATA_HL = "en"


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def fetch_videos_metadata(api_key: str, video_ids: list[str]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for batch in _chunks(video_ids, BATCH_SIZE):
        response = requests.get(
            API_URL,
            params={
                "part": PARTS,
                "id": ",".join(batch),
                "hl": METADATA_HL,
                "key": api_key,
            },
            timeout=60,
        )
        if response.status_code == 403:
            raise RuntimeError(f"YouTube API rejected the request: {response.text[:500]}")
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("items", []):
            video_id = item.get("id")
            if video_id:
                results[video_id] = item
        time.sleep(0.15)
    return results


def _int_or_none(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _english_localization(localizations: dict) -> dict | None:
    """Creator-provided English title/description, if any language code is en or en-*."""
    if not isinstance(localizations, dict):
        return None
    folded = {
        str(code).lower(): value
        for code, value in localizations.items()
        if isinstance(value, dict)
    }
    for code in ("en", "en-us", "en-gb", "en-in", "en-ca", "en-au"):
        hit = folded.get(code)
        if hit and (hit.get("title") or hit.get("description")):
            return hit
    for code, hit in folded.items():
        if code.startswith("en-") or code.startswith("en_"):
            if hit.get("title") or hit.get("description"):
                return hit
    return None


def preferred_title_description(item: dict) -> tuple[str | None, str | None]:
    """
    English title and description when YouTube has them.

    Dubbed videos with no English metadata keep snippet title/description,
    which is the original upload language.
    """
    snippet = item.get("snippet") or {}
    original_title = snippet.get("title")
    original_description = snippet.get("description")

    english = _english_localization(item.get("localizations") or {})
    if english:
        title = (english.get("title") or "").strip() or original_title
        if english.get("description") is not None:
            return title, english.get("description")
        return title, original_description

    localized = snippet.get("localized") or {}
    loc_title = localized.get("title")
    loc_description = localized.get("description")
    title_differs = bool(loc_title) and loc_title != original_title
    description_differs = loc_description is not None and loc_description != original_description
    if title_differs or description_differs:
        return (
            loc_title or original_title,
            loc_description if loc_description is not None else original_description,
        )
    return original_title, original_description


def _thumb_url(snippet: dict) -> str | None:
    thumbnails = snippet.get("thumbnails") or {}
    thumb = (
        thumbnails.get("maxres")
        or thumbnails.get("standard")
        or thumbnails.get("high")
        or thumbnails.get("medium")
        or thumbnails.get("default")
        or {}
    )
    url = thumb.get("url")
    if not url:
        return None
    # YouTube placeholder covers 404 — do not persist them.
    lower = url.lower()
    if "no_thumbnail" in lower or "/img/no_" in lower:
        return None
    return url


def apply_metadata(video: Video, item: dict) -> None:
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    stats = item.get("statistics") or {}
    duration_iso = details.get("duration")
    duration_seconds = parse_iso8601_duration(duration_iso)
    tags = snippet.get("tags") or []
    title, description = preferred_title_description(item)

    video.title = title or video.title
    video.description = description
    video.published_at = snippet.get("publishedAt")
    video.duration_iso = duration_iso
    video.duration_seconds = duration_seconds
    video.view_count = _int_or_none(stats.get("viewCount"))
    video.like_count = _int_or_none(stats.get("likeCount"))
    video.comment_count = _int_or_none(stats.get("commentCount"))
    video.thumbnail_url = _thumb_url(snippet)
    video.tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
    video.youtube_category_id = snippet.get("categoryId")
    video.metadata_json = json.dumps(item, ensure_ascii=False)
    video.metadata_status = "fetched"
    video.metadata_error = None
    video.metadata_fetched_at = utcnow()


def apply_short_metadata(short: Short, item: dict) -> None:
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    stats = item.get("statistics") or {}
    duration_iso = details.get("duration")
    tags = snippet.get("tags") or []
    title, description = preferred_title_description(item)

    short.title = title or short.title
    short.description = description
    short.published_at = snippet.get("publishedAt")
    short.duration_iso = duration_iso
    short.duration_seconds = parse_iso8601_duration(duration_iso)
    short.view_count = _int_or_none(stats.get("viewCount"))
    short.like_count = _int_or_none(stats.get("likeCount"))
    short.comment_count = _int_or_none(stats.get("commentCount"))
    short.thumbnail_url = _thumb_url(snippet)
    short.tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
    short.youtube_category_id = snippet.get("categoryId")
    short.metadata_json = json.dumps(item, ensure_ascii=False)
    short.metadata_status = "fetched"
    short.metadata_error = None
    short.metadata_fetched_at = utcnow()


def apply_playlist_item_metadata(item_row: PlaylistItem, item: dict) -> None:
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    duration_iso = details.get("duration")

    title, description = preferred_title_description(item)
    item_row.title = title or item_row.title
    item_row.description = description
    item_row.published_at = snippet.get("publishedAt") or item_row.published_at
    item_row.duration_iso = duration_iso
    item_row.duration_seconds = parse_iso8601_duration(duration_iso)
    item_row.thumbnail_url = _thumb_url(snippet) or item_row.thumbnail_url
    item_row.metadata_json = json.dumps(item, ensure_ascii=False)
    item_row.metadata_status = "fetched"
    item_row.metadata_error = None
    item_row.metadata_fetched_at = utcnow()


def apply_podcast_metadata(podcast: Podcast, item: dict) -> None:
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    title, description = preferred_title_description(item)
    podcast.title = title or podcast.title
    podcast.description = description
    podcast.published_at = snippet.get("publishedAt")
    item_count = details.get("itemCount")
    if item_count is not None:
        podcast.episode_count = int(item_count)
    podcast.thumbnail_url = _thumb_url(snippet) or podcast.thumbnail_url
    podcast.metadata_json = json.dumps(item, ensure_ascii=False)
    podcast.metadata_status = "fetched"
    podcast.metadata_error = None
    podcast.metadata_fetched_at = utcnow()


def _exclude_podcast_category(query, model):
    """Playlists are not used for Podcasts - PodVerse (category 5)."""
    podcast_creators = query.session.query(Creator.id).filter(
        Creator.category_id == PODCAST_CATEGORY_ID
    )
    return query.filter(model.creator_row_id.notin_(podcast_creators))


def apply_playlist_metadata(playlist: Playlist, item: dict) -> None:
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    title, description = preferred_title_description(item)
    playlist.title = title or playlist.title
    playlist.description = description
    playlist.published_at = snippet.get("publishedAt")
    playlist.item_count = details.get("itemCount")
    playlist.thumbnail_url = _thumb_url(snippet) or playlist.thumbnail_url
    playlist.metadata_json = json.dumps(item, ensure_ascii=False)
    playlist.metadata_status = "fetched"
    playlist.metadata_error = None
    playlist.metadata_fetched_at = utcnow()


def _fetch_playlists_metadata(api_key: str, playlist_ids: list[str]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for batch in _chunks(playlist_ids, BATCH_SIZE):
        response = requests.get(
            PLAYLISTS_API_URL,
            params={
                "part": "snippet,contentDetails,status,localizations",
                "id": ",".join(batch),
                "hl": METADATA_HL,
                "key": api_key,
            },
            timeout=60,
        )
        if response.status_code == 403:
            raise RuntimeError(f"YouTube API rejected playlists.list: {response.text[:500]}")
        response.raise_for_status()
        for item in response.json().get("items", []):
            pid = item.get("id")
            if pid:
                results[pid] = item
        time.sleep(0.15)
    return results


def _purge_oversized_shorts(
    session: Session,
    settings: Settings,
    channel_ids: list[int] | None = None,
) -> int:
    """
    Delete Short rows that are actually long-form VODs.

    Scrape walks every videoId in the channel /shorts Innertube payload and
    stamps them as https://youtube.com/shorts/{id}. That URL works for any
    video id (it redirects to /watch), so channels with no Shorts tab can still
    produce 30–60 minute "shorts".
    """
    limit = settings.max_short_duration_seconds
    query = session.query(Short).filter(
        Short.duration_seconds.isnot(None),
        Short.duration_seconds > limit,
    )
    if channel_ids is not None:
        query = query.filter(Short.creator_row_id.in_(channel_ids))
    fakes = query.all()
    removed = 0
    for short in fakes:
        log.warning(
            "Dropping fake short %s (%ss > max %ss): %s",
            short.youtube_video_id,
            short.duration_seconds,
            limit,
            (short.title or "")[:80],
        )
        # Unlink playlist items that pointed at this fake short.
        for item in (
            session.query(PlaylistItem)
            .filter(PlaylistItem.short_row_id == short.id)
            .all()
        ):
            item.short_row_id = None
            if item.reuse_source == "short":
                item.reuse_source = "none"
                if item.transfer_status == "skipped":
                    item.transfer_status = "pending"
        session.delete(short)
        removed += 1
    if removed:
        session.flush()
    return removed


def fetch_all_metadata(
    session: Session,
    settings: Settings,
    *,
    retry_failed: bool = False,
    force: bool = False,
    channel_ids: list[int] | None = None,
    channel_id: int | None = None,
    media_types: frozenset[str] | set[str] | list[str] | str | None = None,
) -> tuple[int, int]:
    if not settings.youtube_api_key:
        raise RuntimeError("YOUTUBE_API_KEY is missing. Add it to .env")

    channel_ids = normalize_channel_ids(channel_ids, channel_id=channel_id)
    if isinstance(media_types, str):
        media_types = parse_media_types(media_types)
    elif media_types is not None:
        media_types = frozenset(media_types)
    do_videos = media_types is None or "videos" in media_types
    do_shorts = media_types is None or "shorts" in media_types
    do_playlists = media_types is None or "playlists" in media_types
    do_podcasts = media_types is None or "podcasts" in media_types
    log.info(
        "Metadata media filter: %s",
        "all" if media_types is None else ",".join(sorted(media_types)),
    )
    if force and not channel_ids:
        raise ValueError(
            "metadata --force requires --channel-id (creators.id) so only that creator is refreshed"
        )
    if channel_ids:
        creators = (
            session.query(Creator)
            .filter(Creator.id.in_(channel_ids))
            .order_by(Creator.id.asc())
            .all()
        )
        found = {creator.id for creator in creators}
        missing_ids = [cid for cid in channel_ids if cid not in found]
        if missing_ids:
            raise ValueError(
                "No creator row for --channel-id "
                + ", ".join(str(cid) for cid in missing_ids)
            )
        log.info(
            "Metadata %s for %s",
            "force refresh" if force else "fetch",
            ", ".join(f"id={creator.id} {creator.name}" for creator in creators),
        )

    # force ignores metadata_status and rewrites title/description already stored.
    statuses = None if force else (["pending", "failed"] if retry_failed else ["pending"])
    updated = 0
    missing = 0

    def _scoped(query, model):
        if statuses is not None:
            query = query.filter(model.metadata_status.in_(statuses))
        if channel_ids is not None:
            query = query.filter(model.creator_row_id.in_(channel_ids))
        return query

    # --- Long-form videos ---
    videos = []
    if do_videos:
        videos = (
            _scoped(
                session.query(Video).options(joinedload(Video.creator)),
                Video,
            )
            .filter(Video.is_short.is_(False))
            .order_by(Video.id.asc())
            .all()
        )
    if videos:
        ids = list(dict.fromkeys(video.youtube_video_id for video in videos))
        log.info("Fetching metadata for %s videos in batches of %s", len(ids), BATCH_SIZE)
        payload = fetch_videos_metadata(settings.youtube_api_key, ids)
        for video in videos:
            item = payload.get(video.youtube_video_id)
            if not item:
                video.metadata_status = "failed"
                video.metadata_error = "Video not returned by YouTube Data API"
                missing += 1
                continue
            apply_metadata(video, item)
            if video.creator and not video.creator.youtube_channel_id:
                yt_channel_id = (item.get("snippet") or {}).get("channelId")
                if yt_channel_id:
                    video.creator.youtube_channel_id = yt_channel_id
            updated += 1
    elif do_videos:
        log.info("No videos waiting for metadata")

    # --- Shorts ---
    shorts = []
    if do_shorts:
        shorts = (
            _scoped(
                session.query(Short).options(joinedload(Short.creator)),
                Short,
            )
            .order_by(Short.id.asc())
            .all()
        )
    if shorts:
        ids = list(dict.fromkeys(s.youtube_video_id for s in shorts))
        log.info("Fetching metadata for %s shorts", len(ids))
        payload = fetch_videos_metadata(settings.youtube_api_key, ids)
        for short in shorts:
            item = payload.get(short.youtube_video_id)
            if not item:
                short.metadata_status = "failed"
                short.metadata_error = "Short not returned by YouTube Data API"
                missing += 1
                continue
            apply_short_metadata(short, item)
            updated += 1
    elif do_shorts:
        log.info("No shorts waiting for metadata")

    # Drop fake "shorts": channels without a Shorts tab can still return long-form
    # videoIds from /shorts browse JSON; /shorts/{id} then redirects to watch.
    removed_fake = 0
    if do_shorts:
        removed_fake = _purge_oversized_shorts(session, settings, channel_ids)
    if removed_fake:
        log.info(
            "Removed %s fake shorts longer than %ss (not real YouTube Shorts)",
            removed_fake,
            settings.max_short_duration_seconds,
        )

    # --- Playlists (only if still pending; scrape often already filled them) ---
    # Category 5 (Podcasts - PodVerse) has podcast shows instead of playlists.
    playlists = []
    if do_playlists:
        playlists = (
            _exclude_podcast_category(_scoped(session.query(Playlist), Playlist), Playlist)
            .order_by(Playlist.id.asc())
            .all()
        )
    if playlists:
        ids = list(dict.fromkeys(p.youtube_playlist_id for p in playlists))
        log.info("Fetching metadata for %s playlists", len(ids))
        payload = _fetch_playlists_metadata(settings.youtube_api_key, ids)
        for playlist in playlists:
            item = payload.get(playlist.youtube_playlist_id)
            if not item:
                playlist.metadata_status = "failed"
                playlist.metadata_error = "Playlist not returned by YouTube Data API"
                missing += 1
                continue
            apply_playlist_metadata(playlist, item)
            updated += 1
    elif do_playlists:
        log.info("No playlists waiting for metadata")

    # --- Podcasts (Podcasts - PodVerse). YouTube stores each show as a playlist id. ---
    podcasts = []
    if do_podcasts:
        podcasts = (
            _scoped(session.query(Podcast), Podcast)
            .order_by(Podcast.id.asc())
            .all()
        )
    if podcasts:
        ids = list(dict.fromkeys(podcast.youtube_podcast_id for podcast in podcasts))
        log.info("Fetching metadata for %s podcasts", len(ids))
        payload = _fetch_playlists_metadata(settings.youtube_api_key, ids)
        for podcast in podcasts:
            item = payload.get(podcast.youtube_podcast_id)
            if not item:
                podcast.metadata_status = "failed"
                podcast.metadata_error = "Podcast not returned by YouTube Data API"
                missing += 1
                continue
            apply_podcast_metadata(podcast, item)
            updated += 1
    elif do_podcasts:
        log.info("No podcasts waiting for metadata")

    # Podcast episodes not already covered by a video or short.
    episode_items = []
    if do_podcasts:
        episode_query = session.query(PodcastEpisode).filter(
            PodcastEpisode.reuse_source == "none"
        )
        if statuses is not None:
            episode_query = episode_query.filter(
                PodcastEpisode.metadata_status.in_(statuses)
            )
        if channel_ids is not None:
            episode_query = episode_query.filter(
                PodcastEpisode.creator_row_id.in_(channel_ids)
            )
        episode_items = episode_query.order_by(PodcastEpisode.id.asc()).all()
    if episode_items:
        ids = list(dict.fromkeys(row.youtube_video_id for row in episode_items))
        log.info("Fetching metadata for %s podcast episodes", len(ids))
        payload = fetch_videos_metadata(settings.youtube_api_key, ids)
        for row in episode_items:
            item = payload.get(row.youtube_video_id)
            if not item:
                row.metadata_status = "failed"
                row.metadata_error = "Episode not returned by YouTube Data API"
                missing += 1
                continue
            apply_playlist_item_metadata(row, item)
            updated += 1
    elif do_podcasts:
        log.info("No podcast episodes waiting for metadata")

    # --- Playlist items NOT linked to videos/shorts ---
    orphan_items = []
    if do_playlists:
        orphan_query = _exclude_podcast_category(
            session.query(PlaylistItem).filter(PlaylistItem.reuse_source == "none"),
            PlaylistItem,
        )
        if statuses is not None:
            orphan_query = orphan_query.filter(PlaylistItem.metadata_status.in_(statuses))
        if channel_ids is not None:
            orphan_query = orphan_query.filter(PlaylistItem.creator_row_id.in_(channel_ids))
        orphan_items = orphan_query.order_by(PlaylistItem.id.asc()).all()
    if orphan_items:
        ids = list(dict.fromkeys(i.youtube_video_id for i in orphan_items))
        log.info(
            "Fetching metadata for %s unlinked playlist items (no download/upload)",
            len(ids),
        )
        payload = fetch_videos_metadata(settings.youtube_api_key, ids)
        for row in orphan_items:
            item = payload.get(row.youtube_video_id)
            if not item:
                row.metadata_status = "failed"
                row.metadata_error = "Video not returned by YouTube Data API"
                missing += 1
                continue
            apply_playlist_item_metadata(row, item)
            updated += 1
    elif do_playlists:
        log.info("No unlinked playlist items waiting for metadata")

    # Linked items copy the refreshed video/short text. No extra YouTube call.
    reused = []
    if do_playlists:
        reused_query = _exclude_podcast_category(
            session.query(PlaylistItem).filter(
                PlaylistItem.reuse_source.in_(["video", "short"])
            ),
            PlaylistItem,
        )
        if not force:
            reused_query = reused_query.filter(
                PlaylistItem.metadata_status.in_(["pending", "failed"])
            )
        if channel_ids is not None:
            reused_query = reused_query.filter(PlaylistItem.creator_row_id.in_(channel_ids))
        reused = reused_query.all()
    for row in reused:
        source = row.video if row.reuse_source == "video" else row.short
        if source is not None:
            row.title = source.title
            row.description = source.description
            row.published_at = source.published_at
            row.thumbnail_url = source.thumbnail_url or row.thumbnail_url
            row.duration_iso = source.duration_iso
            row.duration_seconds = source.duration_seconds
        row.metadata_status = "reused"
        row.metadata_error = None
        row.metadata_fetched_at = utcnow()
        if not force:
            row.transfer_status = "skipped"
            row.transfer_error = f"Reuses {row.reuse_source} row; skip download/upload"
        updated += 1

    session.commit()
    log.info("Metadata fetched: %s updated, %s missing/unavailable", updated, missing)
    return updated, missing

from __future__ import annotations

import json
import logging
import time

import requests
from sqlalchemy.orm import Session, joinedload

from app.config import Settings
from app.models import Playlist, PlaylistItem, Short, Video
from app.utils import parse_iso8601_duration, utcnow

log = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/youtube/v3/videos"
PLAYLISTS_API_URL = "https://www.googleapis.com/youtube/v3/playlists"
BATCH_SIZE = 50
PARTS = "snippet,contentDetails,statistics,status"


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
    return thumb.get("url")


def apply_metadata(video: Video, item: dict) -> None:
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    stats = item.get("statistics") or {}
    duration_iso = details.get("duration")
    duration_seconds = parse_iso8601_duration(duration_iso)
    tags = snippet.get("tags") or []

    video.title = snippet.get("title") or video.title
    video.description = snippet.get("description")
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

    short.title = snippet.get("title") or short.title
    short.description = snippet.get("description")
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

    item_row.title = snippet.get("title") or item_row.title
    item_row.description = snippet.get("description")
    item_row.published_at = snippet.get("publishedAt") or item_row.published_at
    item_row.duration_iso = duration_iso
    item_row.duration_seconds = parse_iso8601_duration(duration_iso)
    item_row.thumbnail_url = _thumb_url(snippet) or item_row.thumbnail_url
    item_row.metadata_json = json.dumps(item, ensure_ascii=False)
    item_row.metadata_status = "fetched"
    item_row.metadata_error = None
    item_row.metadata_fetched_at = utcnow()


def apply_playlist_metadata(playlist: Playlist, item: dict) -> None:
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    playlist.title = snippet.get("title") or playlist.title
    playlist.description = snippet.get("description")
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
                "part": "snippet,contentDetails,status",
                "id": ",".join(batch),
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


def fetch_all_metadata(
    session: Session,
    settings: Settings,
    *,
    retry_failed: bool = False,
) -> tuple[int, int]:
    if not settings.youtube_api_key:
        raise RuntimeError("YOUTUBE_API_KEY is missing. Add it to .env")

    statuses = ["pending", "failed"] if retry_failed else ["pending"]
    updated = 0
    missing = 0

    # --- Long-form videos ---
    videos = (
        session.query(Video)
        .options(joinedload(Video.creator))
        .filter(Video.metadata_status.in_(statuses), Video.is_short.is_(False))
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
    else:
        log.info("No videos waiting for metadata")

    # --- Shorts ---
    shorts = (
        session.query(Short)
        .options(joinedload(Short.creator))
        .filter(Short.metadata_status.in_(statuses))
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
    else:
        log.info("No shorts waiting for metadata")

    # --- Playlists (only if still pending; scrape often already filled them) ---
    playlists = (
        session.query(Playlist)
        .filter(Playlist.metadata_status.in_(statuses))
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
    else:
        log.info("No playlists waiting for metadata")

    # --- Playlist items NOT linked to videos/shorts ---
    orphan_items = (
        session.query(PlaylistItem)
        .filter(
            PlaylistItem.reuse_source == "none",
            PlaylistItem.metadata_status.in_(statuses),
        )
        .order_by(PlaylistItem.id.asc())
        .all()
    )
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
    else:
        log.info("No unlinked playlist items waiting for metadata")

    # Linked items: mark reused metadata without API calls
    reused = (
        session.query(PlaylistItem)
        .filter(
            PlaylistItem.reuse_source.in_(["video", "short"]),
            PlaylistItem.metadata_status.in_(["pending", "failed"]),
        )
        .all()
    )
    for row in reused:
        row.metadata_status = "reused"
        row.metadata_error = None
        row.transfer_status = "skipped"
        row.transfer_error = f"Reuses {row.reuse_source} row; skip download/upload"
        updated += 1

    session.commit()
    log.info("Metadata fetched: %s updated, %s missing/unavailable", updated, missing)
    return updated, missing

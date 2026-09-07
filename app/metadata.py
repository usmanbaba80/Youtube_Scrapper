from __future__ import annotations

import json
import logging
import time

import requests
from sqlalchemy.orm import Session, joinedload

from app.config import Settings
from app.models import Video
from app.utils import parse_iso8601_duration, utcnow

log = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/youtube/v3/videos"
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


def apply_metadata(video: Video, item: dict) -> None:
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    stats = item.get("statistics") or {}
    thumbnails = snippet.get("thumbnails") or {}
    thumb = (
        thumbnails.get("maxres")
        or thumbnails.get("standard")
        or thumbnails.get("high")
        or thumbnails.get("medium")
        or thumbnails.get("default")
        or {}
    )
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
    video.thumbnail_url = thumb.get("url")
    video.tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
    video.youtube_category_id = snippet.get("categoryId")
    video.metadata_json = json.dumps(item, ensure_ascii=False)
    video.metadata_status = "fetched"
    video.metadata_error = None
    video.metadata_fetched_at = utcnow()


def fetch_all_metadata(
    session: Session,
    settings: Settings,
    *,
    retry_failed: bool = False,
) -> tuple[int, int]:
    if not settings.youtube_api_key:
        raise RuntimeError("YOUTUBE_API_KEY is missing. Add it to .env")

    statuses = ["pending", "failed"] if retry_failed else ["pending"]
    videos = (
        session.query(Video)
        .options(joinedload(Video.creator))
        .filter(Video.metadata_status.in_(statuses), Video.is_short.is_(False))
        .order_by(Video.id.asc())
        .all()
    )
    if not videos:
        log.info("No videos waiting for metadata")
        return 0, 0

    ids = list(dict.fromkeys(video.youtube_video_id for video in videos))
    log.info("Fetching metadata for %s videos in batches of %s", len(ids), BATCH_SIZE)
    payload = fetch_videos_metadata(settings.youtube_api_key, ids)

    updated = 0
    missing = 0
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

    session.commit()
    log.info("Metadata fetched: %s updated, %s missing/unavailable", updated, missing)
    return updated, missing

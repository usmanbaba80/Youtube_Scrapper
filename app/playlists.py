from __future__ import annotations

import logging
import time

import requests

from app.utils import extract_video_id, playlist_url, watch_url

log = logging.getLogger(__name__)

PLAYLISTS_URL = "https://www.googleapis.com/youtube/v3/playlists"
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"


def fetch_channel_playlists(
    api_key: str,
    channel_id: str,
    *,
    limit: int = 10,
) -> list[dict]:
    """List up to `limit` playlists for a channel via YouTube Data API."""
    if limit <= 0:
        return []
    if not api_key:
        raise RuntimeError("YOUTUBE_API_KEY is required to scrape playlists")
    if not channel_id:
        raise RuntimeError("youtube_channel_id is required to scrape playlists")

    items: list[dict] = []
    page_token: str | None = None
    while len(items) < limit:
        params = {
            "part": "snippet,contentDetails,status",
            "channelId": channel_id,
            "maxResults": min(50, limit - len(items)),
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        response = requests.get(PLAYLISTS_URL, params=params, timeout=60)
        if response.status_code == 403:
            raise RuntimeError(f"YouTube API rejected playlists.list: {response.text[:500]}")
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("items") or []:
            yt_id = item.get("id")
            if not yt_id:
                continue
            snippet = item.get("snippet") or {}
            details = item.get("contentDetails") or {}
            thumbs = snippet.get("thumbnails") or {}
            thumb = (
                thumbs.get("maxres")
                or thumbs.get("standard")
                or thumbs.get("high")
                or thumbs.get("medium")
                or thumbs.get("default")
                or {}
            )
            thumb_url = thumb.get("url")
            if thumb_url and ("no_thumbnail" in thumb_url.lower() or "/img/no_" in thumb_url.lower()):
                thumb_url = None
            items.append(
                {
                    "youtube_playlist_id": yt_id,
                    "url": playlist_url(yt_id),
                    "title": snippet.get("title"),
                    "description": snippet.get("description"),
                    "published_at": snippet.get("publishedAt"),
                    "item_count": details.get("itemCount"),
                    "thumbnail_url": thumb_url,
                    "raw": item,
                }
            )
            if len(items) >= limit:
                break
        page_token = payload.get("nextPageToken")
        time.sleep(0.1)
        if not page_token:
            break

    log.info("Fetched %s playlists for channel %s", len(items), channel_id)
    return items


def fetch_playlist_items(
    api_key: str,
    youtube_playlist_id: str,
    *,
    limit: int = 200,
    title: str | None = None,
) -> list[dict]:
    """Fetch playlist video entries (youtube video ids + position)."""
    if not api_key:
        raise RuntimeError("YOUTUBE_API_KEY is required to scrape playlist items")

    label = title or youtube_playlist_id
    items: list[dict] = []
    page_token: str | None = None
    position = 0
    page = 0
    log.info(
        "Fetching items for playlist %r (%s), up to %s…",
        label,
        youtube_playlist_id,
        limit,
    )
    while len(items) < limit:
        page += 1
        params = {
            # contentDetails is enough for videoId; snippet for title/thumb.
            "part": "snippet,contentDetails",
            "playlistId": youtube_playlist_id,
            "maxResults": min(50, limit - len(items)),
            "key": api_key,
            "fields": (
                "nextPageToken,"
                "items(contentDetails/videoId,"
                "snippet(title,description,publishedAt,resourceId/videoId,thumbnails))"
            ),
        }
        if page_token:
            params["pageToken"] = page_token
        response = requests.get(PLAYLIST_ITEMS_URL, params=params, timeout=45)
        if response.status_code == 403:
            raise RuntimeError(
                f"YouTube API rejected playlistItems.list: {response.text[:500]}"
            )
        response.raise_for_status()
        payload = response.json()
        batch = payload.get("items") or []
        for item in batch:
            snippet = item.get("snippet") or {}
            details = item.get("contentDetails") or {}
            video_id = details.get("videoId") or (snippet.get("resourceId") or {}).get(
                "videoId"
            )
            video_id = extract_video_id(video_id)
            if not video_id:
                continue
            position += 1
            thumbs = snippet.get("thumbnails") or {}
            thumb = (
                thumbs.get("maxres")
                or thumbs.get("standard")
                or thumbs.get("high")
                or thumbs.get("medium")
                or thumbs.get("default")
                or {}
            )
            # Keep description short in memory — full text is huge for kids channels.
            desc = snippet.get("description") or ""
            if len(desc) > 500:
                desc = desc[:500]
            items.append(
                {
                    "youtube_video_id": video_id,
                    "url": watch_url(video_id),
                    "position": position,
                    "title": snippet.get("title"),
                    "description": desc,
                    "published_at": snippet.get("publishedAt") or details.get("videoPublishedAt"),
                    "thumbnail_url": thumb.get("url"),
                    "raw": None,
                }
            )
            if len(items) >= limit:
                break
        page_token = payload.get("nextPageToken")
        log.info(
            "  playlist %r page %s: %s items so far",
            label,
            page,
            len(items),
        )
        time.sleep(0.05)
        if not page_token or not batch:
            break

    log.info("Fetched %s items for playlist %r", len(items), label)
    return items

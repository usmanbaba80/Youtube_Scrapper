from __future__ import annotations

import json
import logging
import re

from app.popular import (
    _innertube_context_from_html,
    _pagination_token,
    _session,
    _tab_page_url,
    _walk,
    detect_channel_tabs,
)
from app.utils import playlist_url

log = logging.getLogger(__name__)

# Innertube params for the channel Podcasts tab.
PODCASTS_TAB_PARAMS = "Eghwb2RjYXN0c_IGBQoDugEA"
_EPISODE_COUNT_RE = re.compile(r"([\d,]+)\s+episodes?", re.I)
_ARTWORK_ID_RE = re.compile(r"podcasts_artwork/([A-Za-z0-9_-]+)/")


def fetch_channel_podcasts(
    channel_url: str,
    *,
    limit: int = 10,
    tabs: frozenset[str] | None = None,
) -> list[dict]:
    """
    Fetch podcast shows from the channel Podcasts tab.

    Each show is a YouTube playlist id (PL...). Titles, artwork, and episode
    counts come from the tab. Official description is filled later by metadata.
    """
    if limit <= 0:
        return []

    available = tabs if tabs is not None else detect_channel_tabs(channel_url)
    if "podcasts" not in available:
        log.info(
            "Skipping podcasts for %s — no Podcasts tab on channel",
            channel_url,
        )
        return []

    page_url = _tab_page_url(channel_url, "podcasts")
    http = _session()
    response = http.get(page_url, timeout=45)
    response.raise_for_status()
    if "/podcasts" not in (response.url or "").lower():
        log.info(
            "Skipping podcasts for %s — /podcasts redirected to %s",
            page_url,
            response.url,
        )
        return []

    api_key, context, headers, channel_id, _handle = _innertube_context_from_html(
        response.text, channel_url
    )
    if not channel_id:
        raise RuntimeError(f"Could not resolve channel ID for podcasts scrape: {page_url}")

    endpoint = "https://www.youtube.com/youtubei/v1/browse"
    browse = http.post(
        endpoint,
        params={"key": api_key},
        headers=headers,
        json={
            "context": context,
            "browseId": channel_id,
            "params": PODCASTS_TAB_PARAMS,
        },
        timeout=45,
    )
    browse.raise_for_status()
    payload = browse.json()

    podcasts: list[dict] = []
    seen: set[str] = set()
    saw_lockup = _append_podcasts(payload, podcasts=podcasts, seen=seen, limit=limit)
    pages = 1
    token = _pagination_token(payload)
    while token and len(podcasts) < limit and pages < 20:
        pages += 1
        cont = http.post(
            endpoint,
            params={"key": api_key},
            headers=headers,
            json={"context": context, "continuation": token},
            timeout=45,
        )
        cont.raise_for_status()
        payload = cont.json()
        saw_lockup = (
            _append_podcasts(payload, podcasts=podcasts, seen=seen, limit=limit) or saw_lockup
        )
        token = _pagination_token(payload)

    if saw_lockup and not podcasts:
        raise RuntimeError(
            f"Podcasts tab for {page_url} had entries, but no show ids could be read"
        )

    log.info("Fetched %s podcasts from %s (%s pages)", len(podcasts), page_url, pages)
    return podcasts


def _append_podcasts(
    payload: dict,
    *,
    podcasts: list[dict],
    seen: set[str],
    limit: int,
) -> bool:
    """Append shows. Returns True when the payload contained a podcast lockup."""
    saw_lockup = False
    for node in _walk(payload):
        if not isinstance(node, dict):
            continue
        lock = node.get("lockupViewModel")
        if not isinstance(lock, dict):
            continue
        saw_lockup = True
        if len(podcasts) >= limit:
            continue
        youtube_id = _podcast_id_from_lock(lock)
        if not youtube_id or youtube_id in seen:
            continue
        seen.add(youtube_id)
        podcasts.append(
            {
                "youtube_podcast_id": youtube_id,
                "url": playlist_url(youtube_id),
                "title": _title_from_lock(lock),
                "thumbnail_url": _largest_thumb(lock),
                "episode_count": _episode_count(lock),
            }
        )
        if len(podcasts) >= limit:
            return saw_lockup
    return saw_lockup


def _podcast_id_from_lock(lock: dict) -> str | None:
    content_id = lock.get("contentId")
    if isinstance(content_id, str) and content_id.startswith("PL"):
        return content_id
    for node in _walk(lock):
        if not isinstance(node, dict):
            continue
        playlist_id = node.get("playlistId")
        if isinstance(playlist_id, str) and playlist_id.startswith("PL"):
            return playlist_id
    match = _ARTWORK_ID_RE.search(json.dumps(lock))
    if match and match.group(1).startswith("PL"):
        return match.group(1)
    return None


def _title_from_lock(lock: dict) -> str | None:
    meta = (lock.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    title = meta.get("title") or {}
    if isinstance(title, dict) and title.get("content"):
        return str(title["content"]).strip() or None
    return None


def _largest_thumb(lock: dict) -> str | None:
    best_url: str | None = None
    best_width = -1
    for node in _walk(lock):
        if not isinstance(node, dict):
            continue
        sources = node.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            url = source.get("url")
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            width = int(source.get("width") or 0)
            if width >= best_width:
                best_width = width
                best_url = url.replace("\\u0026", "&")
    return best_url


def _episode_count(lock: dict) -> int | None:
    for node in _walk(lock):
        if not isinstance(node, dict):
            continue
        text = node.get("text")
        if not isinstance(text, str):
            continue
        match = _EPISODE_COUNT_RE.search(text)
        if match:
            return int(match.group(1).replace(",", ""))
    return None

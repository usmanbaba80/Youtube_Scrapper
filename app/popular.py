from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from app.utils import channel_handle, extract_video_id, is_short_url, normalize_channel_url, watch_url

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
CHIP_TOKEN_RE = re.compile(
    r'"chipViewModel":\{"text":"(Latest|Popular|Oldest)"[\s\S]*?'
    r'"continuationCommand":\{"token":"([^"]+)"'
)
LABEL_RE = re.compile(r'"(Latest|Popular|Oldest)"')
CONTINUATION_TOKEN_RE = re.compile(r'"continuationCommand":\{"token":"([^"]+)"')
API_KEY_RE = re.compile(r'"INNERTUBE_API_KEY":"([^"]+)"')
CLIENT_VERSION_RE = re.compile(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"')
CHANNEL_ID_PATTERNS = (
    re.compile(r'"channelId":"(UC[\w-]{22})"'),
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
    re.compile(r'"browseId":"(UC[\w-]{22})"'),
)
HANDLE_RE = re.compile(r'"canonicalBaseUrl":"/(@[\w.-]+)"')
HANDLE_VANITY_RE = re.compile(
    r'"vanityChannelUrl":"http[s]?://www\.youtube\.com/(@[\w.-]+)"'
)
# Sort discriminator embedded in Videos-tab continuation tokens.
LATEST_SORT_MARKER = "VlCQSUzRCUzRA"
POPULAR_SORT_MARKER = "VlBZyUzRCUzRA"
# Innertube params for the channel Videos tab (default/Latest order).
VIDEOS_TAB_PARAMS = "EgZ2aWRlb3PyBgQKAjoA"


def _videos_page_url(channel_url: str) -> str:
    base = normalize_channel_url(channel_url)
    parsed = urlparse(base)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/videos", "", "", "")
    )


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _extract_video_ids(payload: dict) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for node in _walk(payload):
        video_id = node.get("videoId")
        if (
            isinstance(video_id, str)
            and extract_video_id(video_id)
            and len(node) > 1
            and video_id not in seen
        ):
            seen.add(video_id)
            ids.append(video_id)
    return ids


def _pagination_token(payload: dict) -> str | None:
    for node in _walk(payload):
        renderer = node.get("continuationItemRenderer")
        if not isinstance(renderer, dict):
            continue
        token = (
            ((renderer.get("continuationEndpoint") or {}).get("continuationCommand") or {}).get(
                "token"
            )
            or ((renderer.get("continuationCommand") or {}).get("token"))
        )
        if token:
            return token
    return None


def _extract_sort_tokens(html: str) -> dict[str, str]:
    """Pull Latest/Popular/Oldest continuation tokens from Videos-tab HTML."""
    tokens = dict(CHIP_TOKEN_RE.findall(html))

    # Newer layouts sometimes only expose chipViewModel for Latest, while
    # Popular/Oldest appear later as plain labels near continuationCommand.
    for match in LABEL_RE.finditer(html):
        label = match.group(1)
        if label in tokens:
            continue
        window = html[match.start() : match.start() + 2500]
        token_match = CONTINUATION_TOKEN_RE.search(window)
        if token_match:
            tokens[label] = token_match.group(1)

    # Last-resort: derive Popular from Latest by swapping the sort marker.
    if "Popular" not in tokens and "Latest" in tokens:
        latest = tokens["Latest"]
        if LATEST_SORT_MARKER in latest:
            tokens["Popular"] = latest.replace(LATEST_SORT_MARKER, POPULAR_SORT_MARKER, 1)

    return tokens


def _extract_channel_id(html: str) -> str | None:
    for pattern in CHANNEL_ID_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


def _extract_handle(html: str, channel_url: str) -> str | None:
    match = HANDLE_RE.search(html) or HANDLE_VANITY_RE.search(html)
    if match:
        return match.group(1)
    return channel_handle(channel_url)


def _node_title(node: dict) -> str | None:
    for key in ("title", "headline"):
        value = node.get(key)
        if isinstance(value, dict):
            text = value.get("simpleText") or "".join(
                run.get("text", "") for run in value.get("runs") or []
            )
            if text:
                return text
        elif isinstance(value, str) and value.strip():
            return value.strip()
    metadata = node.get("metadata") or {}
    lockup_meta = metadata.get("lockupMetadataViewModel") or {}
    title = lockup_meta.get("title") or {}
    if isinstance(title, dict) and title.get("content"):
        return str(title["content"])
    return None


def _title_map(payload: dict) -> dict[str, str]:
    titles: dict[str, str] = {}
    for node in _walk(payload):
        video_id = node.get("videoId")
        if not isinstance(video_id, str) or not extract_video_id(video_id):
            continue
        title = _node_title(node)
        if title and video_id not in titles:
            titles[video_id] = title
    return titles


def _append_videos(
    payload: dict,
    *,
    videos: list[dict],
    seen: set[str],
    limit: int,
    channel_id: str | None,
    handle: str | None,
) -> None:
    titles = _title_map(payload)
    for video_id in _extract_video_ids(payload):
        url = watch_url(video_id)
        if is_short_url(url) or video_id in seen:
            continue
        seen.add(video_id)
        videos.append(
            {
                "youtube_video_id": video_id,
                "url": url,
                "title": titles.get(video_id),
                "youtube_channel_id": channel_id,
                "handle": handle,
            }
        )
        if len(videos) >= limit:
            return


def _paginate_continuation(
    http: requests.Session,
    *,
    api_key: str,
    context: dict,
    headers: dict,
    start_token: str,
    limit: int,
    channel_id: str | None,
    handle: str | None,
) -> tuple[list[dict], int]:
    videos: list[dict] = []
    seen: set[str] = set()
    token: str | None = start_token
    pages = 0
    endpoint = "https://www.youtube.com/youtubei/v1/browse"

    while token and len(videos) < limit and pages < 20:
        pages += 1
        browse = http.post(
            endpoint,
            params={"key": api_key},
            headers=headers,
            json={"context": context, "continuation": token},
            timeout=45,
        )
        browse.raise_for_status()
        payload = browse.json()
        _append_videos(
            payload,
            videos=videos,
            seen=seen,
            limit=limit,
            channel_id=channel_id,
            handle=handle,
        )
        token = _pagination_token(payload)

    return videos, pages


def _browse_videos_tab(
    http: requests.Session,
    *,
    api_key: str,
    context: dict,
    headers: dict,
    channel_id: str,
    limit: int,
    handle: str | None,
) -> tuple[list[dict], int]:
    endpoint = "https://www.youtube.com/youtubei/v1/browse"
    browse = http.post(
        endpoint,
        params={"key": api_key},
        headers=headers,
        json={
            "context": context,
            "browseId": channel_id,
            "params": VIDEOS_TAB_PARAMS,
        },
        timeout=45,
    )
    browse.raise_for_status()
    payload = browse.json()

    videos: list[dict] = []
    seen: set[str] = set()
    _append_videos(
        payload,
        videos=videos,
        seen=seen,
        limit=limit,
        channel_id=channel_id,
        handle=handle,
    )
    pages = 1

    token = _pagination_token(payload)
    while token and len(videos) < limit and pages < 20:
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
        _append_videos(
            payload,
            videos=videos,
            seen=seen,
            limit=limit,
            channel_id=channel_id,
            handle=handle,
        )
        token = _pagination_token(payload)

    return videos, pages


def fetch_popular_videos(channel_url: str, limit: int = 100) -> list[dict]:
    """Fetch Popular long-form videos via YouTube Innertube continuation chips.

    yt-dlp currently ignores ?sort=p and always returns Latest. The Popular chip
    uses a continuation token from the Videos tab page. Small channels may not
    expose sort chips; in that case we fall back to the Videos tab listing.
    """
    page_url = _videos_page_url(channel_url)
    http = _session()
    response = http.get(page_url, timeout=45)
    response.raise_for_status()
    html = response.text

    api_key_match = API_KEY_RE.search(html)
    version_match = CLIENT_VERSION_RE.search(html)
    if not api_key_match or not version_match:
        raise RuntimeError(
            "Could not read YouTube Innertube config from Videos tab "
            "(page layout may have changed or a consent wall was returned)"
        )

    api_key = api_key_match.group(1)
    client_version = version_match.group(1)
    channel_id = _extract_channel_id(html)
    handle = _extract_handle(html, channel_url)
    tokens = _extract_sort_tokens(html)
    popular_token = tokens.get("Popular")

    context = {
        "client": {
            "clientName": "WEB",
            "clientVersion": client_version,
            "hl": "en",
            "gl": "US",
        }
    }
    headers = {
        "Content-Type": "application/json",
        "X-Youtube-Client-Name": "1",
        "X-Youtube-Client-Version": client_version,
    }

    if popular_token:
        videos, pages = _paginate_continuation(
            http,
            api_key=api_key,
            context=context,
            headers=headers,
            start_token=popular_token,
            limit=limit,
            channel_id=channel_id,
            handle=handle,
        )
        source = "Popular"
    else:
        if not channel_id:
            raise RuntimeError(
                "Popular sort chips were missing and channel ID could not be resolved "
                f"for {page_url}"
            )
        log.warning(
            "Popular chips missing for %s; falling back to Videos tab listing "
            "(common for channels with very few uploads)",
            page_url,
        )
        videos, pages = _browse_videos_tab(
            http,
            api_key=api_key,
            context=context,
            headers=headers,
            channel_id=channel_id,
            limit=limit,
            handle=handle,
        )
        source = "Videos-tab fallback"

    if not videos:
        raise RuntimeError(f"{source} returned no videos for {page_url}")

    log.info(
        "Fetched %s videos via %s from %s (%s pages)",
        len(videos),
        source,
        page_url,
        pages,
    )
    return videos

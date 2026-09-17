from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse


_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TAB_SUFFIXES = (
    "/videos",
    "/shorts",
    "/streams",
    "/live",
    "/featured",
    "/playlists",
    "/community",
    "/posts",
    "/about",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def slugify(value: str, fallback: str = "item") -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return text[:80] or fallback


def creator_folder_name(creator) -> str:
    """Filesystem/Bunny-safe folder name for a creator row."""
    raw = (getattr(creator, "handle", None) or getattr(creator, "name", None) or f"creator-{getattr(creator, 'id', 'x')}")
    raw = str(raw).lstrip("@").strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", raw)
    cleaned = re.sub(r"\s+", "-", cleaned).strip(" .-_")
    cid = getattr(creator, "id", "x")
    return cleaned[:120] or f"creator-{cid}"


def parse_sheet_category_id(sheet_title: str) -> int | None:
    """Extract category id from sheet title brackets, e.g. 'Kids - MiniMinds (1)' -> 1."""
    match = re.search(r"\((\d+)\)\s*$", (sheet_title or "").strip())
    if not match:
        return None
    return int(match.group(1))


def build_video_id(category_id: int, creator_id: int, serial: int) -> str:
    """
    Join category_id + creator_id + serial (3-digit) into one video_id.

    Example:
      category=1, creator=100, serial=1  -> 1100001
      category=1, creator=100, serial=11 -> 1100011
      category=1, creator=101, serial=1  -> 1101001
    """
    if serial < 1 or serial > 999:
        raise ValueError(f"serial must be 1..999, got {serial}")
    return f"{category_id}{creator_id}{serial:03d}"


def build_short_id(category_id: int, creator_id: int, serial: int) -> str:
    """S{category}{creator}{serial:03d} e.g. S1100001."""
    if serial < 1 or serial > 999:
        raise ValueError(f"serial must be 1..999, got {serial}")
    return f"S{category_id}{creator_id}{serial:03d}"


def build_playlist_id(category_id: int, creator_id: int, serial: int) -> str:
    """P{category}{creator}{serial:02d} e.g. P110001."""
    if serial < 1 or serial > 99:
        raise ValueError(f"serial must be 1..99, got {serial}")
    return f"P{category_id}{creator_id}{serial:02d}"


# Backward-compatible alias
build_app_video_id = build_video_id


def parse_iso8601_duration(value: str | None) -> int | None:
    if not value:
        return None
    match = _ISO_DURATION.fullmatch(value)
    if not match:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return int(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def extract_video_id(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if _VIDEO_ID_RE.fullmatch(text):
        return text
    parsed = urlparse(text)
    host = (parsed.netloc or "").lower()
    if "youtu.be" in host:
        candidate = parsed.path.strip("/").split("/")[0]
        return candidate if _VIDEO_ID_RE.fullmatch(candidate) else None
    if "youtube.com" not in host and "youtube-nocookie.com" not in host:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if parts and parts[0] in {"shorts", "embed", "live", "v"} and len(parts) > 1:
        candidate = parts[1]
        return candidate if _VIDEO_ID_RE.fullmatch(candidate) else None
    query = parsed.query
    for chunk in query.split("&"):
        if chunk.startswith("v="):
            candidate = chunk[2:]
            return candidate if _VIDEO_ID_RE.fullmatch(candidate) else None
    return None


def is_short_url(url: str | None) -> bool:
    if not url:
        return False
    return "/shorts/" in url.lower()


def channel_handle(channel_url: str) -> str | None:
    path = urlparse(channel_url).path.strip("/")
    if path.startswith("@"):
        return path.split("/")[0]
    return None


def normalize_channel_url(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return text
    if text.startswith("//"):
        text = "https:" + text
    elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        text = "https://" + text
    parsed = urlparse(text)
    scheme = "https"
    netloc = (parsed.netloc or "www.youtube.com").lower()
    if netloc.startswith("m.youtube.") or netloc in {"youtube.com", "youtu.be"}:
        netloc = "www.youtube.com"
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    for suffix in _TAB_SUFFIXES:
        if lowered.endswith(suffix):
            path = path[: -len(suffix)]
            break
    path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", "", ""))


def popular_videos_url(channel_url: str) -> str:
    base = normalize_channel_url(channel_url)
    parsed = urlparse(base)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/") + "/videos",
            "",
            "view=0&sort=p&flow=grid",
            "",
        )
    )


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def shorts_url(video_id: str) -> str:
    return f"https://www.youtube.com/shorts/{video_id}"


def playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"

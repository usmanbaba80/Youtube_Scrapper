from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from urllib.parse import quote, unquote, urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# "Podcasts - PodVerse" uses podcast shows instead of channel playlists.
PODCAST_CATEGORY_ID = 5
MAX_PODCASTS_PER_CHANNEL = 3
MAX_PODCAST_EPISODES = 20


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or default).strip()


def normalize_bunny_root_path(raw: str) -> str:
    """
    Normalize Bunny folder prefix, e.g.
      Kids%20Apps%2FVoD%20-%20Roku%20TV  ->  Kids Apps/VoD - Roku TV
    """
    text = unquote((raw or "").strip()).replace("\\", "/")
    parts = [p.strip() for p in text.split("/") if p.strip()]
    return "/".join(parts)


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else default


def _env_height(name: str, default: int) -> int:
    """Parse values like 1080, 1080p, 4k, 2160p."""
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in {"4k", "uhd"}:
        return 2160
    if raw in {"2k", "qhd"}:
        return 1440
    if raw in {"hd", "fhd"}:
        return 1080
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else default


def _optional_path(name: str) -> Path | None:
    raw = _env(name)
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


@dataclass(frozen=True)
class Settings:
    youtube_api_key: str
    bunny_stream_library_id: str
    bunny_stream_api_key: str
    bunny_stream_cdn_hostname: str
    bunny_storage_zone: str
    bunny_storage_password: str
    bunny_storage_hostname: str
    bunny_storage_cdn_hostname: str
    bunny_root_path: str
    upload_concurrency: int
    transfer_concurrency: int
    database_url: str
    excel_path: Path
    download_dir: Path
    max_videos_per_channel: int
    max_shorts_per_channel: int
    max_short_duration_seconds: int
    max_playlists_per_channel: int
    max_playlist_items: int
    max_podcasts_per_channel: int
    max_podcast_episodes: int
    scrape_fetch_limit: int
    cookies_file: Path | None
    cookies_dir: Path | None
    cookies_from_browser: str | None
    ytdlp_format: str
    ytdlp_max_height: int
    ytdlp_min_height: int
    ytdlp_player_clients: str
    ytdlp_js_runtimes: str
    ytdlp_sleep_interval: float
    ytdlp_sleep_requests: float
    ffmpeg_location: str
    scrape_delay_seconds: float
    download_delay_seconds: float
    download_retries: int
    rate_limit_cooldown_seconds: float
    rate_limit_max_cooldowns: int
    transfer_auto_restarts: int
    transfer_restart_delay_seconds: float
    transfer_same_video_host_fails: int
    logs_dir: Path
    # SOCKS URL for YouTube traffic only. Empty means a direct connection.
    youtube_proxy: str


def load_settings() -> Settings:
    excel = _optional_path("EXCEL_PATH") or (PROJECT_ROOT / "data" / "channels.xlsx")
    download_dir = _optional_path("DOWNLOAD_DIR") or (PROJECT_ROOT / "temp_downloads")
    db_url = _env("DATABASE_URL", "sqlite:///data/scraper.db")
    if db_url.startswith("sqlite:///"):
        db_path = db_url.removeprefix("sqlite:///")
        if db_path != ":memory:" and not Path(db_path).is_absolute():
            db_url = "sqlite:///" + (PROJECT_ROOT / db_path).as_posix()

    max_videos = _env_int("MAX_VIDEOS_PER_CHANNEL", 100)
    max_shorts = _env_int("MAX_SHORTS_PER_CHANNEL", 50)
    max_playlists = _env_int("MAX_PLAYLISTS_PER_CHANNEL", 10)
    max_playlist_items = _env_int("MAX_PLAYLIST_ITEMS", 100)
    max_podcasts = _env_int("MAX_PODCASTS_PER_CHANNEL", MAX_PODCASTS_PER_CHANNEL)
    max_podcast_episodes = _env_int("MAX_PODCAST_EPISODES", MAX_PODCAST_EPISODES)
    # YouTube Shorts max length is 3 minutes; longer = mis-scraped long-form.
    max_short_duration = _env_int("MAX_SHORT_DURATION_SECONDS", 180)
    return Settings(
        youtube_api_key=_env("YOUTUBE_API_KEY"),
        bunny_stream_library_id=_env("BUNNY_STREAM_LIBRARY_ID"),
        bunny_stream_api_key=_env("BUNNY_STREAM_API_KEY"),
        bunny_stream_cdn_hostname=_env("BUNNY_STREAM_CDN_HOSTNAME"),
        bunny_storage_zone=_env("BUNNY_STORAGE_ZONE"),
        bunny_storage_password=_env("BUNNY_STORAGE_PASSWORD"),
        bunny_storage_hostname=_env("BUNNY_STORAGE_HOSTNAME", "storage.bunnycdn.com"),
        bunny_storage_cdn_hostname=_env("BUNNY_STORAGE_CDN_HOSTNAME"),
        bunny_root_path=normalize_bunny_root_path(
            _env("BUNNY_ROOT_PATH", "Kids Apps/VoD - Roku TV")
        ),
        upload_concurrency=max(1, _env_int("UPLOAD_CONCURRENCY", 3)),
        # How many videos may be mid download+upload at once (limits disk use).
        transfer_concurrency=max(1, _env_int("TRANSFER_CONCURRENCY", 2)),
        database_url=db_url,
        excel_path=excel,
        download_dir=download_dir,
        max_videos_per_channel=max_videos,
        max_shorts_per_channel=max(0, min(max_shorts, 999)),
        max_short_duration_seconds=max(15, min(max_short_duration, 600)),
        max_playlists_per_channel=max(0, min(max_playlists, 99)),
        max_playlist_items=max(1, min(max_playlist_items, 500)),
        max_podcasts_per_channel=max(0, min(max_podcasts, MAX_PODCASTS_PER_CHANNEL)),
        max_podcast_episodes=max(1, min(max_podcast_episodes, MAX_PODCAST_EPISODES)),
        scrape_fetch_limit=max(max_videos + 80, 180),
        cookies_file=_optional_path("YTDLP_COOKIES"),
        cookies_dir=_optional_path("YTDLP_COOKIES_DIR"),
        cookies_from_browser=_env("YTDLP_COOKIES_FROM_BROWSER") or None,
        # Prefer highest available video+audio (VP9/AV1 ok); ffmpeg remuxes to mp4.
        ytdlp_format=_env("YTDLP_FORMAT", "bv*+ba/b"),
        ytdlp_max_height=_env_height("YTDLP_MAX_HEIGHT", 1080),
        # Reject downloads below this height and retry other clients (0 = allow any).
        ytdlp_min_height=_env_height("YTDLP_MIN_HEIGHT", 720),
        # Comma-separated override. Empty uses web_embedded,tv_downgraded,web
        # with cookies, and visionos,web without.
        ytdlp_player_clients=_env("YTDLP_PLAYER_CLIENTS"),
        ytdlp_js_runtimes=_env("YTDLP_JS_RUNTIMES"),
        # yt-dlp built-in pauses (seconds). Cuts account-level rate limits.
        ytdlp_sleep_interval=float(_env("YTDLP_SLEEP_INTERVAL") or "5"),
        ytdlp_sleep_requests=float(_env("YTDLP_SLEEP_REQUESTS") or "1.5"),
        ffmpeg_location=_env("FFMPEG_LOCATION"),
        scrape_delay_seconds=float(_env("SCRAPE_DELAY_SECONDS") or "2"),
        # Gap between finished downloads (serialized). Raise if you still get limited.
        download_delay_seconds=float(_env("DOWNLOAD_DELAY_SECONDS") or "8"),
        download_retries=_env_int("DOWNLOAD_RETRIES", 3),
        # YouTube says account limit can last up to ~1 hour.
        rate_limit_cooldown_seconds=float(
            _env("RATE_LIMIT_COOLDOWN_SECONDS") or "3700"
        ),
        # How many cooldown+cookie-rotate cycles before aborting the run.
        rate_limit_max_cooldowns=max(1, _env_int("RATE_LIMIT_MAX_COOLDOWNS", 3)),
        # After a YouTube pause, transfer restarts itself this many times (0 = off).
        transfer_auto_restarts=max(0, _env_int("TRANSFER_AUTO_RESTARTS", 24)),
        # Pause before that restart when some videos already uploaded this pass.
        transfer_restart_delay_seconds=float(
            _env("TRANSFER_RESTART_DELAY_SECONDS") or "300"
        ),
        # Same video unavailable on every host this many times, then skip it.
        transfer_same_video_host_fails=max(
            1, _env_int("TRANSFER_SAME_VIDEO_HOST_FAILS", 2)
        ),
        logs_dir=PROJECT_ROOT / "logs",
        youtube_proxy=youtube_proxy_url(),
    )


class _NordProxyPool:
    """Ordered NordVPN SOCKS exits for this process. Other server traffic is untouched."""

    def __init__(self, urls: list[str]) -> None:
        self._urls = urls
        self._index = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._urls)

    def all(self) -> list[str]:
        with self._lock:
            return list(self._urls)

    def current(self) -> str:
        with self._lock:
            if not self._urls:
                return ""
            return self._urls[self._index % len(self._urls)]

    def rotate(self) -> str:
        with self._lock:
            if not self._urls:
                return ""
            if len(self._urls) == 1:
                return self._urls[0]
            self._index = (self._index + 1) % len(self._urls)
            return self._urls[self._index]

    def reset(self) -> None:
        with self._lock:
            self._index = 0


_PROXY_POOL: _NordProxyPool | None = None


def _clean_socks_host(raw: str) -> str:
    host = raw.strip()
    host = host.removeprefix("socks5h://").removeprefix("socks5://").strip("/")
    if "@" in host:
        host = host.split("@", 1)[1]
    if host.startswith("http://") or host.startswith("https://"):
        return ""
    return host


def _build_proxy_urls() -> list[str]:
    explicit = _env("YTDLP_PROXY")
    if explicit:
        return [explicit]
    hosts: list[str] = []
    seen: set[str] = set()
    for part in _env("NORDVPN_SOCKS_HOST").split(","):
        host = _clean_socks_host(part)
        key = host.lower()
        if not host or key in seen:
            continue
        seen.add(key)
        hosts.append(host)
    user = _env("NORDVPN_SOCKS_USER")
    password = _env("NORDVPN_SOCKS_PASSWORD")
    if not hosts or not user or not password:
        return []
    port = _env("NORDVPN_SOCKS_PORT") or "1080"
    user_q = quote(user, safe="")
    pass_q = quote(password, safe="")
    # socks5h resolves YouTube's hostname at the proxy, not on this server.
    return [f"socks5h://{user_q}:{pass_q}@{host}:{port}" for host in hosts]


def _proxy_pool() -> _NordProxyPool:
    global _PROXY_POOL
    if _PROXY_POOL is None:
        _PROXY_POOL = _NordProxyPool(_build_proxy_urls())
    return _PROXY_POOL


def reset_youtube_proxy() -> None:
    """Start the next transfer attempt on the first configured NordVPN exit."""
    _proxy_pool().reset()


def youtube_proxy_url() -> str:
    """
    Current YouTube SOCKS URL for this process.

    NORDVPN_SOCKS_HOST may list several hosts separated by commas. The first
    is used until switch_youtube_proxy() moves to the next. A YTDLP_PROXY
    value is a single fixed URL. Empty means YouTube stays on the server IP.
    """
    return _proxy_pool().current()


def switch_youtube_proxy(tried_labels: set[str]) -> str:
    """
    Mark the current exit as used and return the next host that is not in
    tried_labels. Returns "" when every configured host was already tried.
    """
    pool = _proxy_pool()
    current = pool.current()
    if not current:
        return ""
    tried_labels.add(youtube_proxy_label(current))
    if len(pool) <= 1:
        return ""
    for _ in range(len(pool)):
        nxt = pool.rotate()
        if youtube_proxy_label(nxt) not in tried_labels:
            return nxt
    return ""


def youtube_proxy_label(proxy_url: str) -> str:
    """Host:port only, so logs never include the service password."""
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        return "configured proxy"
    if parsed.port:
        return f"{parsed.hostname}:{parsed.port}"
    return parsed.hostname

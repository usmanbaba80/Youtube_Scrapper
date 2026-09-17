from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or default).strip()


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
    upload_concurrency: int
    transfer_concurrency: int
    database_url: str
    excel_path: Path
    download_dir: Path
    max_videos_per_channel: int
    max_shorts_per_channel: int
    max_playlists_per_channel: int
    max_playlist_items: int
    scrape_fetch_limit: int
    cookies_file: Path | None
    cookies_from_browser: str | None
    ytdlp_format: str
    ytdlp_max_height: int
    ytdlp_js_runtimes: str
    ffmpeg_location: str
    scrape_delay_seconds: float
    download_delay_seconds: float
    download_retries: int
    logs_dir: Path


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
    return Settings(
        youtube_api_key=_env("YOUTUBE_API_KEY"),
        bunny_stream_library_id=_env("BUNNY_STREAM_LIBRARY_ID"),
        bunny_stream_api_key=_env("BUNNY_STREAM_API_KEY"),
        bunny_stream_cdn_hostname=_env("BUNNY_STREAM_CDN_HOSTNAME"),
        bunny_storage_zone=_env("BUNNY_STORAGE_ZONE"),
        bunny_storage_password=_env("BUNNY_STORAGE_PASSWORD"),
        bunny_storage_hostname=_env("BUNNY_STORAGE_HOSTNAME", "storage.bunnycdn.com"),
        bunny_storage_cdn_hostname=_env("BUNNY_STORAGE_CDN_HOSTNAME"),
        upload_concurrency=max(1, _env_int("UPLOAD_CONCURRENCY", 3)),
        # How many videos may be mid download+upload at once (limits disk use).
        transfer_concurrency=max(1, _env_int("TRANSFER_CONCURRENCY", 2)),
        database_url=db_url,
        excel_path=excel,
        download_dir=download_dir,
        max_videos_per_channel=max_videos,
        max_shorts_per_channel=max(0, min(max_shorts, 999)),
        max_playlists_per_channel=max(0, min(max_playlists, 99)),
        max_playlist_items=max(1, min(max_playlist_items, 500)),
        scrape_fetch_limit=max(max_videos + 80, 180),
        cookies_file=_optional_path("YTDLP_COOKIES"),
        cookies_from_browser=_env("YTDLP_COOKIES_FROM_BROWSER") or None,
        # Prefer highest available video+audio (VP9/AV1 ok); ffmpeg remuxes to mp4.
        ytdlp_format=_env("YTDLP_FORMAT", "bv*+ba/b"),
        ytdlp_max_height=_env_height("YTDLP_MAX_HEIGHT", 1080),
        ytdlp_js_runtimes=_env("YTDLP_JS_RUNTIMES"),
        ffmpeg_location=_env("FFMPEG_LOCATION"),
        scrape_delay_seconds=float(_env("SCRAPE_DELAY_SECONDS") or "2"),
        download_delay_seconds=float(_env("DOWNLOAD_DELAY_SECONDS") or "3"),
        download_retries=_env_int("DOWNLOAD_RETRIES", 3),
        logs_dir=PROJECT_ROOT / "logs",
    )

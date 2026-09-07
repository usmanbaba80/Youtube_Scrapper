from __future__ import annotations

import logging
import re
import shutil
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from yt_dlp import YoutubeDL

from app.bunny import BunnyStream
from app.config import Settings
from app.models import Creator, Video
from app.utils import utcnow

log = logging.getLogger(__name__)


def _upload_one_file(
    settings: Settings,
    *,
    local_path: Path,
    title: str,
    collection_id: str,
) -> dict[str, str]:
    """Run in a worker thread with its own Bunny HTTP session."""
    return BunnyStream(settings).upload_video(
        local_path,
        title=title,
        collection_id=collection_id,
    )


def creator_folder_name(creator: Creator) -> str:
    raw = (creator.handle or creator.name or f"creator-{creator.id}").lstrip("@").strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", raw)
    cleaned = re.sub(r"\s+", "-", cleaned).strip(" .-_")
    return cleaned[:120] or f"creator-{creator.id}"


def creator_dir(settings: Settings, creator: Creator) -> Path:
    return settings.download_dir / creator_folder_name(creator)


def _resolve_js_runtimes(settings: Settings) -> dict[str, dict]:
    """YouTube extraction now requires a JS runtime (Deno preferred)."""
    configured = (settings.ytdlp_js_runtimes or "").strip()
    if configured:
        runtimes: dict[str, dict] = {}
        for part in configured.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                name, path = part.split(":", 1)
                runtimes[name.strip()] = {"path": path.strip()}
            else:
                runtimes[part] = {}
        return runtimes

    runtimes = {}
    deno = shutil.which("deno")
    if deno:
        runtimes["deno"] = {"path": deno}
    else:
        # Common winget install location if PATH was not refreshed.
        local = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
        if local.exists():
            matches = sorted(local.glob("DenoLand.Deno*/deno.exe"))
            if matches:
                runtimes["deno"] = {"path": str(matches[-1])}
    node = shutil.which("node")
    if node:
        runtimes["node"] = {"path": node}
    return runtimes


def _resolve_ffmpeg_location(settings: Settings) -> str | None:
    configured = (settings.ffmpeg_location or "").strip()
    if configured:
        return configured

    which = shutil.which("ffmpeg")
    if which:
        return which

    local = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if local.exists():
        matches = sorted(local.glob("Gyan.FFmpeg*/**/ffmpeg.exe"))
        if matches:
            return str(matches[-1])
    return None


def _ytdlp_download_opts(settings: Settings, output_dir: Path) -> dict:
    js_runtimes = _resolve_js_runtimes(settings)
    ffmpeg_location = _resolve_ffmpeg_location(settings)
    if not js_runtimes:
        log.warning(
            "No JS runtime found (Deno/Node). YouTube downloads will likely fail. "
            "Install Deno, then reopen the terminal."
        )
    if not ffmpeg_location:
        log.warning(
            "ffmpeg not found. Install ffmpeg so video+audio can be merged into one mp4."
        )

    max_h = max(settings.ytdlp_max_height, 360)
    # Prefer target height (default 1080), then next-best, then any.
    # Do NOT use web/android alone — they often only expose 360p without PO tokens.
    format_selector = settings.ytdlp_format
    if format_selector == "bv*+ba/b":
        prefer = min(max_h, 1080)
        format_selector = (
            f"bv*[height>={prefer}][height<=?{max_h}]+ba[ext=m4a]/"
            f"bv*[height>={prefer}][height<=?{max_h}]+ba/"
            f"bv*[height>=720][height<=?{max_h}]+ba[ext=m4a]/"
            f"bv*[height>=720][height<=?{max_h}]+ba/"
            f"bv*[height<=?{max_h}]+ba/"
            "bv*+ba/b"
        )

    opts: dict = {
        "format": format_selector,
        # Higher resolution wins (do not use res:N — that prefers closest-to-N).
        "format_sort": ["res", "vbr", "abr", "size"],
        "format_sort_force": True,
        "merge_output_format": "mp4",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "ignoreerrors": False,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "retry_sleep_functions": {
            "http": lambda n: min(2 ** n, 30),
            "fragment": lambda n: min(2 ** n, 30),
            "file_access": lambda n: min(2 ** n, 10),
        },
        # High concurrency against googlevideo often triggers intermittent 403s.
        "concurrent_fragment_downloads": 1,
        "overwrites": True,
        "noprogress": False,
        "quiet": False,
        "no_warnings": False,
        "restrictfilenames": True,
        "remote_components": ["ejs:github"],
        "extractor_args": {
            "youtube": {
                # These clients still expose 720p/1080p DASH. web/android often
                # only return progressive 360p without a PO token.
                "player_client": [
                    "android_sdkless",
                    "tv_embedded",
                    "web_embedded",
                    "mediaconnect",
                ],
            }
        },
    }
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location
    if settings.cookies_file:
        opts["cookiefile"] = str(settings.cookies_file)
    elif settings.cookies_from_browser:
        opts["cookiesfrombrowser"] = (settings.cookies_from_browser,)
    return opts


def download_video(settings: Settings, video: Video, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    opts = _ytdlp_download_opts(settings, output_dir)
    attempts = max(1, settings.download_retries)
    last_error: Exception | None = None

    log.info("Downloading %s (%s)", video.youtube_video_id, video.title or video.url)
    for attempt in range(1, attempts + 1):
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(video.url, download=True)
                if not info:
                    raise RuntimeError("yt-dlp returned no info after download")

                height = info.get("height")
                if not height and info.get("requested_formats"):
                    height = max(
                        (fmt.get("height") or 0 for fmt in info["requested_formats"]),
                        default=0,
                    ) or None
                log.info(
                    "Selected format %s (%s) for %s",
                    info.get("format_id") or "unknown",
                    f"{height}p" if height else (info.get("resolution") or "unknown"),
                    video.youtube_video_id,
                )
                if height and height < 720:
                    log.warning(
                        "Low resolution %sp for %s — source may not offer HD, "
                        "or YouTube blocked higher formats",
                        height,
                        video.youtube_video_id,
                    )

                requested = info.get("requested_downloads") or []
                filepath = None
                if requested and requested[0].get("filepath"):
                    filepath = Path(requested[0]["filepath"])
                if filepath is None:
                    filename = ydl.prepare_filename(info)
                    filepath = Path(filename)
                    if not filepath.exists():
                        alt = filepath.with_suffix(".mp4")
                        if alt.exists():
                            filepath = alt
            if not filepath.exists():
                matches = list(output_dir.glob(f"{video.youtube_video_id}.*"))
                matches = [
                    path
                    for path in matches
                    if path.suffix.lower() not in {".part", ".ytdl", ".json"}
                ]
                if not matches:
                    raise RuntimeError(
                        f"Downloaded file not found for {video.youtube_video_id}"
                    )
                filepath = matches[0]
            return filepath
        except Exception as exc:
            last_error = exc
            message = str(exc).lower()
            retryable = any(
                token in message
                for token in ("403", "forbidden", "http error 429", "timed out", "timeout")
            )
            if attempt >= attempts or not retryable:
                raise
            sleep_for = min(2 ** attempt, 45)
            log.warning(
                "Download attempt %s/%s failed for %s (%s). Retrying in %ss...",
                attempt,
                attempts,
                video.youtube_video_id,
                exc,
                sleep_for,
            )
            time.sleep(sleep_for)

    raise RuntimeError(str(last_error) if last_error else "Download failed")


def _eligible_download_videos(
    session: Session,
    *,
    retry_failed: bool,
    channel_id: int | None,
    max_per_channel: int,
) -> list[Video]:
    statuses = ["pending", "failed"] if retry_failed else ["pending"]
    query = (
        session.query(Video)
        .options(joinedload(Video.creator).joinedload(Creator.category))
        .filter(Video.is_short.is_(False), Video.transfer_status.in_(statuses))
        .order_by(Video.creator_row_id.asc(), Video.popular_rank.asc())
    )
    if channel_id is not None:
        query = query.filter(Video.creator_row_id == channel_id)

    already_kept = dict(
        session.query(Video.creator_row_id, func.count(Video.id))
        .filter(
            Video.is_short.is_(False),
            Video.transfer_status.in_(["downloaded", "uploaded", "uploading"]),
        )
        .group_by(Video.creator_row_id)
        .all()
    )

    selected: list[Video] = []
    queued: dict[int, int] = {}
    for video in query.all():
        # Failed downloads are eligible for retry, but failed uploads should not
        # be re-downloaded unless the local file is gone.
        if video.transfer_status == "failed" and video.local_path:
            local = Path(video.local_path)
            if local.exists():
                continue
        cid = video.creator_row_id
        used = already_kept.get(cid, 0) + queued.get(cid, 0)
        if used >= max_per_channel:
            continue
        selected.append(video)
        queued[cid] = queued.get(cid, 0) + 1
    return selected


def _eligible_upload_videos(
    session: Session,
    *,
    retry_failed: bool,
    channel_id: int | None,
) -> list[Video]:
    statuses = ["downloaded", "failed"] if retry_failed else ["downloaded"]
    query = (
        session.query(Video)
        .options(joinedload(Video.creator).joinedload(Creator.category))
        .filter(Video.is_short.is_(False), Video.transfer_status.in_(statuses))
        .order_by(Video.creator_row_id.asc(), Video.popular_rank.asc())
    )
    if channel_id is not None:
        query = query.filter(Video.creator_row_id == channel_id)

    selected: list[Video] = []
    for video in query.all():
        if video.transfer_status == "failed" and not video.local_path:
            continue
        if video.local_path and not Path(video.local_path).exists():
            if video.transfer_status == "downloaded":
                video.transfer_status = "pending"
                video.transfer_error = "Local file missing; marked pending for re-download"
            continue
        if video.transfer_status == "failed" and video.local_path and Path(video.local_path).exists():
            selected.append(video)
        elif video.transfer_status == "downloaded":
            selected.append(video)
    return selected


def download_videos(
    session: Session,
    settings: Settings,
    *,
    retry_failed: bool = False,
    channel_id: int | None = None,
) -> tuple[int, int]:
    videos = _eligible_download_videos(
        session,
        retry_failed=retry_failed,
        channel_id=channel_id,
        max_per_channel=settings.max_videos_per_channel,
    )
    if not videos:
        log.info("No videos waiting to download")
        return 0, 0

    by_creator: dict[int, list[Video]] = defaultdict(list)
    for video in videos:
        by_creator[video.creator_row_id].append(video)

    downloaded = 0
    failed = 0
    settings.download_dir.mkdir(parents=True, exist_ok=True)

    for creator_index, (_cid, creator_videos) in enumerate(by_creator.items(), start=1):
        creator = creator_videos[0].creator
        folder = creator_dir(settings, creator)
        folder.mkdir(parents=True, exist_ok=True)
        log.info(
            "[creator %s/%s] Downloading %s videos for %s -> %s",
            creator_index,
            len(by_creator),
            len(creator_videos),
            creator.name,
            folder,
        )

        for index, video in enumerate(creator_videos, start=1):
            video.transfer_status = "downloading"
            video.transfer_error = None
            session.commit()
            try:
                local_path = download_video(settings, video, folder)
                video.local_path = str(local_path.resolve())
                video.file_size = local_path.stat().st_size
                video.transfer_status = "downloaded"
                session.commit()
                downloaded += 1
                log.info(
                    "  [%s/%s] Downloaded %s",
                    index,
                    len(creator_videos),
                    local_path.name,
                )
            except Exception as exc:
                video.transfer_status = "failed"
                video.transfer_error = str(exc)[:2000]
                video.local_path = None
                session.commit()
                failed += 1
                log.exception("Failed download for %s", video.youtube_video_id)

            if index < len(creator_videos) and settings.download_delay_seconds > 0:
                time.sleep(settings.download_delay_seconds)

    return downloaded, failed


def upload_videos(
    session: Session,
    settings: Settings,
    *,
    retry_failed: bool = False,
    channel_id: int | None = None,
) -> tuple[int, int]:
    bunny = BunnyStream(settings)
    videos = _eligible_upload_videos(
        session,
        retry_failed=retry_failed,
        channel_id=channel_id,
    )
    session.commit()  # persist any pending->pending fixes from missing files
    if not videos:
        log.info("No downloaded videos waiting to upload")
        return 0, 0

    by_creator: dict[int, list[Video]] = defaultdict(list)
    for video in videos:
        by_creator[video.creator_row_id].append(video)

    uploaded = 0
    failed = 0

    for creator_index, (_cid, creator_videos) in enumerate(by_creator.items(), start=1):
        creator = creator_videos[0].creator
        folder = creator_dir(settings, creator)
        creator_name = (creator.name or creator.handle or f"creator-{creator.id}").strip()
        collection_title = creator_name.lstrip("@").strip() or f"creator-{creator.id}"
        log.info(
            "[creator %s/%s] Uploading %s videos for creator %r -> Stream collection",
            creator_index,
            len(by_creator),
            len(creator_videos),
            collection_title,
        )

        try:
            collection_id = creator.bunny_collection_id or bunny.ensure_creator_collection(
                collection_title
            )
            if creator.bunny_collection_id != collection_id:
                creator.bunny_collection_id = collection_id
                session.commit()
        except Exception as exc:
            log.exception("Failed to ensure Stream collection for %s", collection_title)
            for video in creator_videos:
                video.transfer_status = "failed"
                video.transfer_error = f"Collection error: {exc}"[:2000]
            session.commit()
            failed += len(creator_videos)
            continue

        # Prepare jobs; skip missing files first.
        jobs: list[tuple[Video, Path]] = []
        for video in creator_videos:
            local_path = Path(video.local_path) if video.local_path else None
            if local_path is None or not local_path.exists():
                video.transfer_status = "pending"
                video.local_path = None
                video.transfer_error = "Local file missing before upload"
                failed += 1
                continue
            video.transfer_status = "uploading"
            video.transfer_error = None
            jobs.append((video, local_path))
        session.commit()

        if not jobs:
            continue

        workers = min(settings.upload_concurrency, len(jobs))
        log.info(
            "Uploading %s files for %r with concurrency=%s",
            len(jobs),
            collection_title,
            workers,
        )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(
                    _upload_one_file,
                    settings,
                    local_path=local_path,
                    title=video.video_id or video.title or video.youtube_video_id,
                    collection_id=collection_id,
                ): (video, local_path, index)
                for index, (video, local_path) in enumerate(jobs, start=1)
            }

            for future in as_completed(future_map):
                video, local_path, index = future_map[future]
                try:
                    result = future.result()
                    video.file_size = local_path.stat().st_size if local_path.exists() else video.file_size
                    video.bunny_path = result["video_id"]
                    video.bunny_url = result.get("hls_url") or result["play_url"]
                    video.transfer_status = "uploaded"
                    video.uploaded_at = utcnow()
                    video.local_path = None
                    video.transfer_error = None
                    session.commit()
                    local_path.unlink(missing_ok=True)
                    uploaded += 1
                    log.info(
                        "  [%s/%s] Uploaded %s -> collection=%s video=%s",
                        index,
                        len(jobs),
                        video.youtube_video_id,
                        collection_id,
                        result["video_id"],
                    )
                except Exception as exc:
                    video.transfer_status = "failed"
                    video.transfer_error = str(exc)[:2000]
                    session.commit()
                    failed += 1
                    log.exception("Failed upload for %s", video.youtube_video_id)

        # Remove empty channel folder after uploads.
        if folder.exists() and not any(folder.iterdir()):
            shutil.rmtree(folder, ignore_errors=True)
        elif folder.exists():
            leftovers = [p.name for p in folder.iterdir()]
            log.warning(
                "Creator folder still has files after upload (%s): %s",
                folder,
                leftovers[:10],
            )

    return uploaded, failed


@dataclass
class _TransferJob:
    db_id: int
    youtube_video_id: str
    url: str
    title: str | None
    app_video_id: str | None
    collection_id: str
    output_dir: Path
    existing_local: Path | None


class _DownloadTarget:
    """Minimal object accepted by download_video()."""

    def __init__(self, youtube_video_id: str, url: str, title: str | None) -> None:
        self.youtube_video_id = youtube_video_id
        self.url = url
        self.title = title


def _eligible_transfer_videos(
    session: Session,
    *,
    retry_failed: bool,
    channel_id: int | None,
    max_per_channel: int,
) -> list[Video]:
    """Videos that need download and/or upload."""
    statuses = ["pending", "failed", "downloaded"]
    if not retry_failed:
        # Still include downloaded leftovers so one command finishes the pipeline.
        statuses = ["pending", "downloaded"]

    query = (
        session.query(Video)
        .options(joinedload(Video.creator).joinedload(Creator.category))
        .filter(Video.is_short.is_(False), Video.transfer_status.in_(statuses))
        .order_by(Video.creator_row_id.asc(), Video.popular_rank.asc())
    )
    if channel_id is not None:
        query = query.filter(Video.creator_row_id == channel_id)

    already_done = dict(
        session.query(Video.creator_row_id, func.count(Video.id))
        .filter(
            Video.is_short.is_(False),
            Video.transfer_status.in_(
                ["uploaded", "uploading", "downloading", "downloaded"]
            ),
        )
        .group_by(Video.creator_row_id)
        .all()
    )

    selected: list[Video] = []
    queued: dict[int, int] = {}
    for video in query.all():
        local = Path(video.local_path) if video.local_path else None
        local_ok = bool(local and local.exists())

        if video.transfer_status == "downloaded" and not local_ok:
            video.transfer_status = "pending"
            video.local_path = None
            video.transfer_error = "Local file missing; will re-download"
            local_ok = False

        if video.transfer_status == "failed":
            if not retry_failed:
                continue
            # Retry upload-only when file is still on disk; else re-download.
            if local_ok:
                pass
            else:
                video.local_path = None

        if video.transfer_status == "downloaded" and local_ok:
            selected.append(video)
            continue

        if video.transfer_status in {"pending", "failed"}:
            cid = video.creator_row_id
            used = already_done.get(cid, 0) + queued.get(cid, 0)
            # Count already-selected download jobs for this creator too.
            if used >= max_per_channel:
                continue
            selected.append(video)
            if not local_ok:
                queued[cid] = queued.get(cid, 0) + 1

    return selected


def _run_transfer_job(settings: Settings, job: _TransferJob) -> dict:
    """
    Worker: download (if needed) -> upload to Stream -> delete local file.
    """
    local_path = job.existing_local
    try:
        if local_path is None or not local_path.exists():
            job.output_dir.mkdir(parents=True, exist_ok=True)
            target = _DownloadTarget(job.youtube_video_id, job.url, job.title)
            local_path = download_video(settings, target, job.output_dir)

        result = BunnyStream(settings).upload_video(
            local_path,
            title=job.app_video_id or job.title or job.youtube_video_id,
            collection_id=job.collection_id,
        )
        file_size = local_path.stat().st_size if local_path.exists() else None
        local_path.unlink(missing_ok=True)
        return {
            "ok": True,
            "db_id": job.db_id,
            "youtube_video_id": job.youtube_video_id,
            "bunny": result,
            "file_size": file_size,
        }
    except Exception as exc:
        kept = None
        if local_path is not None and local_path.exists():
            kept = str(local_path.resolve())
        return {
            "ok": False,
            "db_id": job.db_id,
            "youtube_video_id": job.youtube_video_id,
            "error": str(exc)[:2000],
            "local_path": kept,
        }


def transfer_videos(
    session: Session,
    settings: Settings,
    *,
    retry_failed: bool = False,
    channel_id: int | None = None,
) -> tuple[int, int, int]:
    """
    Combined pipeline: download -> upload -> delete, with limited parallelism.

    Only ``TRANSFER_CONCURRENCY`` videos are in-flight at once so disk usage
    stays bounded. Returns (uploaded, download_failed_or_upload_failed, skipped).
    """
    videos = _eligible_transfer_videos(
        session,
        retry_failed=retry_failed,
        channel_id=channel_id,
        max_per_channel=settings.max_videos_per_channel,
    )
    session.commit()
    if not videos:
        log.info("No videos waiting to transfer")
        return 0, 0, 0

    bunny = BunnyStream(settings)
    settings.download_dir.mkdir(parents=True, exist_ok=True)

    # Ensure one Stream collection per creator up front.
    collection_by_creator: dict[int, str] = {}
    by_creator: dict[int, list[Video]] = defaultdict(list)
    for video in videos:
        by_creator[video.creator_row_id].append(video)

    skipped_creators: set[int] = set()
    for creator_row_id, creator_videos in by_creator.items():
        creator = creator_videos[0].creator
        title = (creator.name or creator.handle or f"creator-{creator.id}").strip()
        title = title.lstrip("@").strip() or f"creator-{creator.id}"
        try:
            collection_id = creator.bunny_collection_id or bunny.ensure_creator_collection(
                title
            )
            if creator.bunny_collection_id != collection_id:
                creator.bunny_collection_id = collection_id
            collection_by_creator[creator_row_id] = collection_id
        except Exception as exc:
            log.exception("Failed Stream collection for %s", title)
            skipped_creators.add(creator_row_id)
            for video in creator_videos:
                video.transfer_status = "failed"
                video.transfer_error = f"Collection error: {exc}"[:2000]

    session.commit()
    videos = [v for v in videos if v.creator_row_id not in skipped_creators]
    if not videos:
        return 0, len(skipped_creators), 0

    jobs: list[_TransferJob] = []
    for video in videos:
        collection_id = collection_by_creator.get(video.creator_row_id)
        if not collection_id:
            continue
        creator = video.creator
        folder = creator_dir(settings, creator)
        existing = Path(video.local_path) if video.local_path else None
        if existing and not existing.exists():
            existing = None
        video.transfer_status = "downloading" if existing is None else "uploading"
        video.transfer_error = None
        jobs.append(
            _TransferJob(
                db_id=video.id,
                youtube_video_id=video.youtube_video_id,
                url=video.url,
                title=video.title,
                app_video_id=video.video_id,
                collection_id=collection_id,
                output_dir=folder,
                existing_local=existing,
            )
        )
    session.commit()

    if not jobs:
        log.info("No transfer jobs after collection setup")
        return 0, 0, 0

    workers = min(settings.transfer_concurrency, len(jobs))
    log.info(
        "Transferring %s videos with concurrency=%s (download → upload → delete)",
        len(jobs),
        workers,
    )

    uploaded = 0
    failed = 0
    video_by_id = {v.id: v for v in videos}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_transfer_job, settings, job): job for job in jobs
        }
        for future in as_completed(futures):
            job = futures[future]
            result = future.result()
            video = session.get(Video, job.db_id) or video_by_id.get(job.db_id)
            if video is None:
                continue
            if result["ok"]:
                bunny_info = result["bunny"]
                video.file_size = result.get("file_size")
                video.bunny_path = bunny_info["video_id"]
                video.bunny_url = bunny_info.get("hls_url") or bunny_info["play_url"]
                video.transfer_status = "uploaded"
                video.uploaded_at = utcnow()
                video.local_path = None
                video.transfer_error = None
                uploaded += 1
                log.info(
                    "Transferred %s -> Stream %s (collection %s)",
                    result["youtube_video_id"],
                    bunny_info["video_id"],
                    job.collection_id,
                )
            else:
                video.transfer_status = "failed"
                video.transfer_error = result.get("error")
                video.local_path = result.get("local_path")
                failed += 1
                log.error(
                    "Transfer failed for %s: %s",
                    result["youtube_video_id"],
                    result.get("error"),
                )
            session.commit()

    # Clean empty creator folders.
    for creator_videos in by_creator.values():
        folder = creator_dir(settings, creator_videos[0].creator)
        if folder.exists() and not any(folder.iterdir()):
            shutil.rmtree(folder, ignore_errors=True)

    return uploaded, failed, 0

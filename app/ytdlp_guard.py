"""YouTube download throttling, rate-limit cooldown, and cookie rotation."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_RATE_LIMIT_MARKERS = (
    "rate-limited",
    "rate limited",
    "try again later",
    "this content isn't available",
    "http error 429",
    "too many requests",
)


_BOT_CHECK_MARKERS = (
    "sign in to confirm you’re not a bot",
    "sign in to confirm you're not a bot",
    "confirm you're not a bot",
    "confirm you’re not a bot",
)


def is_youtube_rate_limited(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return any(token in text for token in _RATE_LIMIT_MARKERS)


def is_youtube_bot_check(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return any(token in text for token in _BOT_CHECK_MARKERS)


class RateLimitAbort(RuntimeError):
    """Raised when cooldowns/cookie rotations are exhausted — stop the transfer run."""


class CookiePool:
    """
    Ordered list of Netscape cookie files.

    Put multiple accounts in one folder, e.g.:
      data/cookies/account1.txt
      data/cookies/account2.txt
      data/cookies/account3.txt
    """

    def __init__(self, paths: list[Path]):
        self._paths = [p for p in paths if p is not None]
        self._index = 0
        self._lock = threading.Lock()

    @classmethod
    def from_settings(
        cls,
        *,
        cookies_file: Path | None,
        cookies_dir: Path | None,
    ) -> CookiePool:
        paths: list[Path] = []
        seen: set[str] = set()

        def add(path: Path | None) -> None:
            if path is None:
                return
            resolved = path.resolve()
            key = str(resolved).lower()
            if key in seen:
                return
            if not resolved.is_file():
                log.warning("Cookie file missing, skipping: %s", resolved)
                return
            seen.add(key)
            paths.append(resolved)

        add(cookies_file)
        if cookies_dir is not None:
            directory = cookies_dir.resolve()
            if directory.is_dir():
                for path in sorted(directory.glob("*.txt")):
                    name = path.name.lower()
                    if name in {"readme.txt", "license.txt"} or name.startswith("."):
                        continue
                    add(path)
            else:
                log.warning("YTDLP_COOKIES_DIR is not a directory: %s", directory)
        return cls(paths)

    @property
    def size(self) -> int:
        return len(self._paths)

    def current(self) -> Path | None:
        with self._lock:
            if not self._paths:
                return None
            return self._paths[self._index % len(self._paths)]

    def rotate(self) -> Path | None:
        """Move to the next cookie file. Returns the new current path (or None)."""
        with self._lock:
            if len(self._paths) <= 1:
                return self._paths[0] if self._paths else None
            self._index = (self._index + 1) % len(self._paths)
            nxt = self._paths[self._index]
            log.warning("Rotated YouTube cookies → %s", nxt.name)
            return nxt

    def describe(self) -> str:
        with self._lock:
            if not self._paths:
                return "none"
            names = [p.name for p in self._paths]
            cur = names[self._index % len(names)]
            return f"{cur} ({self._index + 1}/{len(names)}: {', '.join(names)})"


class YoutubeDownloadGuard:
    """
    Process-wide guard shared by transfer workers:

    - Only one yt-dlp extract/download at a time (uploads may still overlap)
    - Enforced delay between downloads
    - On rate-limit: cool down (default ~1h) and rotate cookies, then retry
    - After max cooldown rounds: abort so remaining rows stay pending
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slot = threading.Lock()  # exclusive download slot
        self._cooldown_until = 0.0
        self._last_download_end = 0.0
        self._cooldown_rounds = 0
        self._abort = False
        self._abort_reason = ""
        self._cookies: CookiePool | None = None
        self._configured = False

    def configure(self, settings, *, force_reload: bool = False) -> None:
        with self._lock:
            if self._configured and not force_reload:
                return
            self._cookies = CookiePool.from_settings(
                cookies_file=settings.cookies_file,
                cookies_dir=settings.cookies_dir,
            )
            self._configured = True
            if self._cookies.size:
                log.info("YouTube cookie pool: %s", self._cookies.describe())
            elif settings.cookies_from_browser:
                log.info(
                    "Using browser cookies (%s) — consider YTDLP_COOKIES_DIR with "
                    "multiple accounts for rotation",
                    settings.cookies_from_browser,
                )
            elif settings.cookies_dir or settings.cookies_file:
                log.error(
                    "Cookie path configured but no .txt files found "
                    "(expected under %s). Anonymous downloads will hit bot-check. "
                    "Export fresh Netscape cookies into that folder.",
                    settings.cookies_dir or settings.cookies_file,
                )
            else:
                log.error(
                    "No YTDLP_COOKIES / YTDLP_COOKIES_DIR configured — "
                    "YouTube will bot-check / rate-limit anonymous downloads"
                )

    def reset_run(self) -> None:
        """Call at the start of each transfer/download command."""
        with self._lock:
            self._abort = False
            self._abort_reason = ""
            self._cooldown_rounds = 0
            # Reload cookie files so newly dropped cookies.txt are picked up.
            self._configured = False

    def has_cookies(self) -> bool:
        with self._lock:
            return bool(self._cookies and self._cookies.size)

    def trip_bot_check(self, settings, *, video_id: str) -> None:
        """
        Bot-check usually means dead/missing cookies. Rotate if possible, else abort.
        """
        self.configure(settings)
        with self._lock:
            self._cooldown_rounds += 1
            cooldown = max(60.0, min(600.0, float(settings.rate_limit_cooldown_seconds) / 6))
            self._cooldown_until = time.time() + cooldown
            rotated = None
            if self._cookies and self._cookies.size > 1:
                rotated = self._cookies.rotate()
            log.error(
                "YouTube bot-check on %s (round %s/%s). "
                "Cookies are missing, expired, or invalid%s. Cooling %.0fs.",
                video_id,
                self._cooldown_rounds,
                settings.rate_limit_max_cooldowns,
                f"; switched → {rotated.name}" if rotated else "",
                cooldown,
            )
            if self._cooldown_rounds >= max(1, settings.rate_limit_max_cooldowns) or (
                not self._cookies or self._cookies.size <= 1
            ):
                self._abort = True
                self._abort_reason = (
                    "YouTube bot-check (Sign in to confirm you’re not a bot). "
                    "Export fresh cookies to YTDLP_COOKIES_DIR (Netscape .txt) "
                    "and re-run. Anonymous / android_vr downloads will not work."
                )
                raise RateLimitAbort(self._abort_reason)

    def cookiefile(self) -> Path | None:
        assert self._cookies is not None
        return self._cookies.current()

    def is_aborted(self) -> bool:
        with self._lock:
            return self._abort

    def raise_if_aborted(self) -> None:
        with self._lock:
            if self._abort:
                raise RateLimitAbort(self._abort_reason or "YouTube rate-limit abort")

    def acquire(self, settings) -> Path | None:
        """
        Block until it is safe to run yt-dlp. Returns the cookie file to use.
        Holds the exclusive download slot until release().
        """
        self.configure(settings)
        self.raise_if_aborted()

        while True:
            self.raise_if_aborted()
            with self._lock:
                now = time.time()
                wait_cd = max(0.0, self._cooldown_until - now)
                wait_gap = max(
                    0.0,
                    self._last_download_end + float(settings.download_delay_seconds) - now,
                )
                wait = max(wait_cd, wait_gap)
                if wait <= 0:
                    break
                reason = "rate-limit cooldown" if wait_cd >= wait_gap else "download delay"
                log.info(
                    "YouTube %s: waiting %.0fs before next download "
                    "(cookie=%s, rounds=%s/%s)",
                    reason,
                    wait,
                    self._cookies.describe() if self._cookies else "none",
                    self._cooldown_rounds,
                    settings.rate_limit_max_cooldowns,
                )
            # Sleep outside the lock so other threads can observe the same wait.
            time.sleep(min(wait, 30.0))

        self._slot.acquire()
        try:
            self.raise_if_aborted()
            return self.cookiefile()
        except Exception:
            self._slot.release()
            raise

    def release(self, *, ok: bool) -> None:
        with self._lock:
            self._last_download_end = time.time()
        self._slot.release()

    def trip_rate_limit(self, settings, *, video_id: str) -> None:
        """
        Enter cooldown and rotate cookies. Raises RateLimitAbort when exhausted.
        Caller should retry the same video after this returns.
        """
        self.configure(settings)
        with self._lock:
            self._cooldown_rounds += 1
            cooldown = max(60.0, float(settings.rate_limit_cooldown_seconds))
            self._cooldown_until = time.time() + cooldown
            rotated = None
            if self._cookies and self._cookies.size > 1:
                rotated = self._cookies.rotate()
            log.error(
                "YouTube rate-limited on %s (round %s/%s). "
                "Cooling down %.0fs%s. Remaining queue will wait — not fail-storm.",
                video_id,
                self._cooldown_rounds,
                settings.rate_limit_max_cooldowns,
                cooldown,
                f", switched cookies → {rotated.name}" if rotated else "",
            )
            if self._cooldown_rounds >= max(1, settings.rate_limit_max_cooldowns):
                self._abort = True
                self._abort_reason = (
                    f"YouTube rate-limit persisted after {self._cooldown_rounds} "
                    f"cooldown(s) / cookie rotation(s). "
                    f"Re-export fresh cookies into YTDLP_COOKIES_DIR and re-run "
                    f"with --retry-failed later."
                )
                raise RateLimitAbort(self._abort_reason)


# Shared by all transfer worker threads in this process.
DOWNLOAD_GUARD = YoutubeDownloadGuard()

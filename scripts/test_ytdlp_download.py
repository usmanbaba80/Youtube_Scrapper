"""
Quick YouTube download smoke test (run on WebHost).

Usage:
  .\.venv\Scripts\activate
  python scripts/test_ytdlp_download.py nmB5-hzt57w
  python scripts/test_ytdlp_download.py nmB5-hzt57w --cookies data/cookies/account1.txt
  python scripts/test_ytdlp_download.py nmB5-hzt57w --clients ios,tv --no-cookies
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yt_dlp import YoutubeDL


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video_id")
    parser.add_argument("--cookies", type=Path, default=None)
    parser.add_argument("--no-cookies", action="store_true")
    parser.add_argument(
        "--clients",
        default="ios,tv,tv_simply",
        help="Comma-separated player clients",
    )
    args = parser.parse_args()
    url = f"https://www.youtube.com/watch?v={args.video_id}"
    out = ROOT / "temp_downloads" / "_ytdlp_test"
    out.mkdir(parents=True, exist_ok=True)

    clients = [c.strip() for c in args.clients.split(",") if c.strip()]
    opts: dict = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 2,
        "skip_unavailable_fragments": False,
        "remote_components": ["ejs:github"],
        "extractor_args": {"youtube": {"player_client": clients}},
        "quiet": False,
    }
    if not args.no_cookies:
        if args.cookies:
            opts["cookiefile"] = str(args.cookies.resolve())
        else:
            pool = ROOT / "data" / "cookies"
            found = sorted(pool.glob("*.txt")) if pool.is_dir() else []
            found = [p for p in found if p.name.lower() != "readme.txt"]
            if found:
                opts["cookiefile"] = str(found[0].resolve())
                print("Using cookies:", found[0])
            else:
                print("WARNING: no cookies found; continuing without")

    print("URL:", url)
    print("Clients:", clients)
    print("Cookies:", opts.get("cookiefile") or "(none)")
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        print("OK format=", info.get("format_id"), "height=", info.get("height"))
        return 0
    except Exception as exc:
        print("FAIL:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

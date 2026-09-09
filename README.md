# YouTube Popular Videos → Bunny Stream

Reads your Excel workbook (one sheet per category), stores creators in SQLite, scrapes
**Popular** long-form videos, **Shorts**, and **playlists** per creator, pulls metadata
from the YouTube Data API, then downloads long-form videos and uploads them to **Bunny Stream**.

## What it does

1. **Excel** — every sheet name is a category. Columns used:
   - `YouTube Channel`
   - `Monetization Model`
   - `Channel links`
   - `Id` (creator id)
2. **Scrape**
   - Popular long-form videos (Videos tab / Innertube), default 100
   - Shorts tab (Innertube), default 50 → table `shorts`
   - Channel playlists (Data API), default 10 → tables `playlists` + `playlist_items`
3. **Playlist reuse** — if a playlist video already exists in that creator’s `videos`
   (or `shorts`), the item is **linked** (`reuse_source=video|short`). No extra
   metadata/download/upload for that asset; Bunny file from the linked row is reused.
   Unlinked playlist-only items get metadata only (`transfer_status=skipped`).
4. **Metadata** — Data API for videos, shorts, playlists, and unlinked playlist items.
5. **Transfer** — download → Bunny Stream → delete local, into folder collections:
   - `{CreatorName}/videos`
   - `{CreatorName}/shorts`
   - `{CreatorName}/playlists` (only playlist items **not** already in videos/shorts)

Bunny Stream has no nested folders API, so names use a slash (`Creator/videos`).
In the Stream library UI they appear as separate collections under that naming.

### IDs

- Videos: `{category}{creator}{serial:03d}` → `1100001`
- Shorts: `S{category}{creator}{serial:03d}` → `S1100001`
- Playlists: `P{category}{creator}{serial:02d}` → `P110001`

## Setup

```powershell
cd "c:\Users\UsmanShabbir\OneDrive - Veroke\Desktop\YouTube_Scrapper"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Install [ffmpeg](https://ffmpeg.org/download.html) and [Deno](https://deno.land/) (required for YouTube downloads with current yt-dlp), and make sure both are on `PATH`. Then reopen the terminal.

Fill in `.env`:

- `YOUTUBE_API_KEY` — Data API key (metadata + playlists)
- `BUNNY_STREAM_LIBRARY_ID` / `BUNNY_STREAM_API_KEY`
- `MAX_VIDEOS_PER_CHANNEL` / `MAX_SHORTS_PER_CHANNEL` / `MAX_PLAYLISTS_PER_CHANNEL`

## Commands

```powershell
python main.py load-excel
python main.py scrape --force
python main.py metadata
python main.py transfer
python main.py status
```

## Database

Default: `data/scraper.db`

- `categories`, `creators`, `videos`
- `shorts`, `playlists`, `playlist_items`

New tables are created automatically on next `python main.py …`.

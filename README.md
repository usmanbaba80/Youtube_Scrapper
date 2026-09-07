# YouTube Popular Videos → Bunny Stream

Reads your Excel workbook (one sheet per category), stores channels in SQLite, scrapes the first 100 **Popular** long-form videos from each channel, pulls metadata in bulk from the YouTube Data API, then downloads **one channel at a time** into a local folder and uploads that channel’s files to **Bunny Stream** in a separate step.

## What it does

1. **Excel** — every sheet name is a category. Columns used:
   - `YouTube Channel`
   - `Monetization Model`
   - `Channel links`
2. **Scrape** — opens the channel Videos tab and uses YouTube’s **Popular** chip
   (Innertube continuation). This is required because `?sort=p` / yt-dlp currently
   fall back to Latest. Shorts and Live tabs are not used.
3. **Metadata** — YouTube Data API `videos.list` in batches of 50 IDs. No API usage for listing or downloading.
4. **Transfer** — for each video: **download → upload to Bunny Stream → delete local**.
   Runs a few videos in parallel (`TRANSFER_CONCURRENCY`) so disk stays limited.
   Each creator gets a Stream **collection**; videos go into that collection.

Bunny Stream layout:

```text
Stream Library
  └── Collection: {creator name}
        ├── video 1
        ├── video 2
        └── ...
```

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

- `YOUTUBE_API_KEY` — Data API key for metadata only
- `BUNNY_STREAM_LIBRARY_ID` — Stream library numeric ID
- `BUNNY_STREAM_API_KEY` — Stream library API key (Stream → Library → API)
- `BUNNY_STREAM_CDN_HOSTNAME` — optional, e.g. `vz-xxxx.b-cdn.net` for HLS URLs
- `EXCEL_PATH` — path to the workbook (default `data/channels.xlsx`)

Copy the Excel file to `data/channels.xlsx`.

If YouTube blocks anonymous downloads, export `cookies.txt` (Netscape format) or set:

```env
YTDLP_COOKIES_FROM_BROWSER=chrome
```

Downloads prefer **1080p** (configurable via `YTDLP_MAX_HEIGHT`) using YouTube clients that still expose HD streams. Logs show `Selected format … (1080p)` per video. Already-downloaded low-quality files stay until you re-download with `--force`.

### Excel IDs

- Sheet name must end with `(category_id)`, e.g. `Kids - MiniMinds (1)` → category_id `1`
- Column `Id` is the creator_id (e.g. `100`)
- Each video gets `video_id` by joining `category_id` + `creator_id` + `serial` (3 digits):  
  Example: `1` + `100` + `001` → **`1100001`**, `1100011`, and creator 101 → **`1101001`**
  Videos also store `category_id` and `creator_id`.

## Commands

```powershell
python main.py load-excel
python main.py scrape
python main.py metadata
python main.py transfer
python main.py status
```

`transfer` = download → Bunny Stream upload → delete local (parallel, disk-safe).

Optional separate steps still work: `download`, `upload`.

Full pipeline:

```powershell
python main.py run
```

Useful flags:

```powershell
python main.py scrape --force
python main.py transfer --channel-id 18
python main.py transfer --retry-failed
python main.py run --excel "C:\path\to\channels.xlsx"
```

`--force` re-scrapes creators already marked done.

The job is resumable:
- `transfer` continues from `pending` / leftover `downloaded`
- use `--retry-failed` for errors

Tune parallelism / disk use in `.env`:

```env
TRANSFER_CONCURRENCY=2
```

## Database

Default: `data/scraper.db`

- `categories` ← sheet name + category id from `(N)`
- `creators` ← `creator_id` from Excel `Id`, name, URL, scrape status, Stream collection id
- `videos` ← `video_id` (`{category}{creator}{serial}`), YouTube id, metadata, transfer status

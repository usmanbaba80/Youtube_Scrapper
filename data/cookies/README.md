Put Netscape cookie files here (one Google/YouTube account per file), e.g.:
  account1.txt
  account2.txt
  account3.txt

Export with a "Get cookies.txt LOCALLY" browser extension while logged into YouTube.

Set in .env:
  YTDLP_COOKIES_DIR=data/cookies

On rate-limit the transfer pauses ~1 hour, rotates to the next file, and retries.
Do NOT commit real cookie files to git.

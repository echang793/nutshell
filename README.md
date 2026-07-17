# Nutshell

AI-powered YouTube video summarizer. Paste a video URL and get structured notes in seconds. Two modes: **General** (overview, key points, notable quotes, takeaways — for any video) and **Financial** (thesis, tickers, price levels, catalysts, risks — for trading/investing videos). Free and unlimited, no account required.

Runs locally at: http://localhost:8090 — see [Local hosting](#local-hosting) below.

---

## What it does

1. Fetches the video transcript (via Supadata or youtube-transcript-api)
2. Sends it to Groq's Llama 3.3 70B model for analysis, using the prompt for whichever tab you're on (General or Financial)
3. Returns structured markdown notes
4. Saves your history so you can revisit past summaries

No login, no plans, no limits — every request is unlimited.

---

## Tech stack

- **Backend**: FastAPI + Python 3.11
- **AI**: Groq API (llama-3.3-70b-versatile)
- **Transcripts**: Supadata API (handles YouTube's cloud IP restrictions)
- **Database**: SQLite at `data/summaries.db`
- **Deployment**: local only (macOS LaunchAgent) — no longer hosted on Railway

---

## Data

All data lives in a single SQLite file: **`data/summaries.db`**, one `summaries` table (video URL, generated notes, word count, timestamp). No user accounts, no PII stored. Running locally, this file persists on disk indefinitely (no ephemeral-filesystem resets like a PaaS deploy).

---

## Local hosting

### Requirements

- Python 3.9+
- [Groq API key](https://console.groq.com) (free) — kept in `~/.yt-notes.json` as `{"groq_api_key": "..."}`, or set `GROQ_API_KEY` in the environment
- Supadata API key (optional — not needed locally; only helps on cloud hosts where YouTube blocks the transcript API by IP)

### Install

```bash
cd /Users/erichang/Desktop/Nutshell
pip3 install -r requirements.txt
```

### Run as an always-on background service (recommended)

A LaunchAgent keeps Nutshell running at **http://localhost:8090**, starts it on login, and restarts it automatically if it crashes. Template lives at `deploy/com.erichang.nutshell.plist`:

```bash
cp deploy/com.erichang.nutshell.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.erichang.nutshell.plist
```

Logs: `data/nutshell.log` (stdout) and `data/nutshell.err.log` (stderr).

Stop it:
```bash
launchctl bootout gui/$(id -u)/com.erichang.nutshell
```

Restart after a code change (LaunchAgent doesn't hot-reload):
```bash
launchctl kickstart -k gui/$(id -u)/com.erichang.nutshell
```

### Run manually instead

```bash
uvicorn main:app --host 127.0.0.1 --port 8090 --reload
```

---

## Files

```
main.py          FastAPI app — routes for summarize, history
summarizer.py    Transcript fetching and Groq summarization logic (general + financial prompts)
db.py            SQLite — summaries
static/          Frontend SPA (index.html) — General / Financial / History tabs
Procfile         Unused — leftover from the Railway deployment, harmless to keep or delete
data/            SQLite database + LaunchAgent logs (gitignored)
```

The LaunchAgent template is versioned at `deploy/com.erichang.nutshell.plist`; the active copy launchd runs from is `~/Library/LaunchAgents/com.erichang.nutshell.plist`.

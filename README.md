# Nutshell

AI-powered YouTube video summarizer. Paste a video URL and get structured notes in seconds. Two modes: **General** (overview, key points, notable quotes, takeaways — for any video) and **Financial** (thesis, tickers, price levels, catalysts, risks — for trading/investing videos). Free and unlimited, no account required.

Live at: https://web-production-c271f.up.railway.app

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
- **Deployment**: Railway (auto-deploy from GitHub)

---

## Data

All data lives in a single SQLite file: **`data/summaries.db`**, one `summaries` table (video URL, generated notes, word count, timestamp). No user accounts, no PII stored.

**Railway note**: Railway's filesystem is ephemeral — `data/summaries.db` resets on every redeploy. To persist history across deploys, add a Railway Volume mounted at `/app/data`, or migrate to Postgres.

---

## Self-hosting

### Requirements

- Python 3.9+
- [Groq API key](https://console.groq.com) (free)
- Supadata API key (optional, for transcripts on cloud deployments)

### Install

```bash
git clone https://github.com/echang793/nutshell
cd nutshell
pip3 install -r requirements.txt
```

### Configure

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

Required:
```
GROQ_API_KEY=your_groq_key
```

Optional:
```
SUPADATA_API_KEY=your_supadata_key
```

### Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open http://localhost:8000

---

## Files

```
main.py          FastAPI app — routes for summarize, history
summarizer.py    Transcript fetching and Groq summarization logic (general + financial prompts)
db.py            SQLite — summaries
static/          Frontend SPA (index.html) — General / Financial / History tabs
Procfile         Railway / Heroku process definition
data/            SQLite database (gitignored)
```

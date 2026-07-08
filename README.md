# Nutshell

AI-powered YouTube video summarizer. Paste a video URL and get structured notes in seconds — overview, key points, notable quotes, topics, and takeaways — for any kind of video: tutorials, lectures, podcasts, interviews, and more.

Live at: https://web-production-c271f.up.railway.app

---

## What it does

1. Fetches the video transcript (via Supadata or youtube-transcript-api)
2. Sends it to Groq's Llama 3.3 70B model for analysis
3. Returns structured markdown notes with an overview, key points, notable quotes, and takeaways
4. Saves your history so you can revisit past summaries

---

## Plans

| Plan | Price | Summaries |
|------|-------|-----------|
| Free | $0 | 3 / month |
| Basic | $19 / month | 30 / month |
| Pro | $49 / month | Unlimited + priority processing |

Payments via Stripe. Manage or cancel anytime from the Account tab.

Anonymous visitors get 2 free summaries (tracked by cookie) before being prompted to create a free account.

---

## Tech stack

- **Backend**: FastAPI + Python 3.11
- **AI**: Groq API (llama-3.3-70b-versatile)
- **Transcripts**: Supadata API (handles YouTube's cloud IP restrictions)
- **Auth**: bcrypt password hashing, httponly session cookies (30-day TTL)
- **Payments**: Stripe Checkout + Customer Portal + webhooks
- **Database**: SQLite at `data/summaries.db`
- **Deployment**: Railway (auto-deploy from GitHub)

---

## Data & credentials

All user data lives in a single SQLite file: **`data/summaries.db`**

| Table | What's stored |
|-------|--------------|
| `users` | email, bcrypt-hashed password, plan, usage counts, Stripe customer ID |
| `sessions` | random session tokens (httponly cookie) mapped to user IDs |
| `summaries` | video URL, generated notes, word count |

Passwords are hashed with bcrypt before being written — the plaintext password is never stored or logged anywhere.

**Railway note**: Railway's filesystem is ephemeral — `data/summaries.db` resets on every redeploy. To persist user accounts across deploys, add a Railway Volume mounted at `/app/data`, or migrate to Postgres.

---

## Self-hosting

### Requirements

- Python 3.9+
- [Groq API key](https://console.groq.com) (free)
- Stripe account (for payments)
- Supadata API key (for transcripts on cloud deployments)

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

Optional (for payments and alerts):
```
STRIPE_SECRET_KEY=sk_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_BASIC_PRICE_ID=price_...
STRIPE_PRO_PRICE_ID=price_...
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
main.py          FastAPI app — routes for auth, summarize, payments
summarizer.py    Transcript fetching and Groq summarization logic
db.py            SQLite — users, sessions, summaries
auth.py          Password hashing and session management
payments.py      Stripe checkout, portal, and webhook handling
static/          Frontend SPA (index.html)
Procfile         Railway / Heroku process definition
data/            SQLite database (gitignored)
```

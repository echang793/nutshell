"""Core transcript fetching and summarization logic."""

from __future__ import annotations

import re
import time
from pathlib import Path

CACHE_DIR  = Path(__file__).parent / "data" / "transcripts"
GROQ_MODEL = "llama-3.3-70b-versatile"

# Groq on-demand tier caps this model at 12000 tokens/minute (TPM), shared across
# prompt + completion. ~4 chars/token, so keep the transcript small enough that
# prompt + completion tokens stay comfortably under the limit for a single call.
MAX_TRANSCRIPT_CHARS = 18_000

_SYSTEM_PROMPT = (
    "You are an expert note-taker and summarizer for YouTube video transcripts of any kind — "
    "tutorials, lectures, podcasts, interviews, reviews, vlogs, and more. "
    "Extract and organize the key information into clear, accurate notes. "
    "Never invent information that is not in the transcript. "
    "Format your response in clean markdown."
)

_STOCK_SYSTEM_PROMPT = (
    "You are an expert financial analyst and note-taker specializing in stock trading content. "
    "Extract and organize key information from YouTube transcripts into clear, actionable notes. "
    "Be precise with numbers, tickers, and price levels. "
    "Never invent information that is not in the transcript. "
    "Format your response in clean markdown."
)

_FULL_PROMPT = """\
Analyze this YouTube video transcript and create structured notes.

TRANSCRIPT:
{transcript}

Create notes with these exact sections:

## Overview
2-3 sentences summarizing what this video is about and who it's for.

## Key Points
5-10 bullets covering the main ideas, arguments, or steps, in the order presented.

## Notable Quotes
1-3 direct quotes worth remembering. If none stand out, write "None."

## Topics & Terms
Important names, tools, concepts, or terminology mentioned.

## Takeaways
3-5 bullet points of the most useful or actionable things a viewer should remember or do next.\
"""

_BRIEF_PROMPT = """\
Analyze this YouTube video transcript and write a very short briefing.

TRANSCRIPT:
{transcript}

Respond with ONLY these two sections (keep each tight):

## Summary
1-2 sentences — what this video covers.

## Top Takeaways
- Bullet 1
- Bullet 2
- Bullet 3\
"""

_STOCK_FULL_PROMPT = """\
Analyze this YouTube video transcript and create structured trading notes.

TRANSCRIPT:
{transcript}

Create notes with these exact sections:

## Thesis
One or two sentences summarizing the core investment or trading idea.

## Stocks & Tickers Mentioned
List each stock with:
- **$TICKER — Company Name**: Bullish / Bearish / Neutral — what was said

## Key Price Levels
Specific prices, targets, support/resistance levels, or moving averages mentioned:
- $TICKER: [level] — context (e.g. "support at $150", "target $200", "stop below $145")
If none were mentioned, write "None specified."

## Catalysts & Time Horizon
- What events or factors are expected to drive the move
- Timeframe: short-term (days/weeks) / medium-term (months) / long-term

## Risks
Key risks or concerns mentioned by the presenter.

## Actionable Takeaways
3–5 bullet points of the most important things to act on or monitor.

## Plain-English Summary
2–3 sentences summarizing the whole video for someone who hasn't watched it.\
"""

_STOCK_BRIEF_PROMPT = """\
Analyze this YouTube video transcript and write a very short trading briefing.

TRANSCRIPT:
{transcript}

Respond with ONLY these three sections (keep each tight):

## Thesis
One sentence — the core idea.

## Top Takeaways
- Bullet 1
- Bullet 2
- Bullet 3

## Tickers
Comma-separated list of every stock ticker mentioned, each labeled (bullish/bearish/neutral).
If none mentioned, write "None."\
"""

_TRANSLATE_PROMPT = """\
Translate the following transcript to English. Output only the translated text, nothing else.

TRANSCRIPT:
{transcript}\
"""


def extract_video_id(url: str) -> str | None:
    match = re.search(r'(?:v=|youtu\.be/|shorts/|embed/)([a-zA-Z0-9_-]{11})', url)
    return match.group(1) if match else None


def fetch_video_title(video_id: str) -> str:
    """Best-effort title lookup via YouTube's public oEmbed endpoint. Never raises."""
    import requests
    try:
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            timeout=10,
        )
        if not resp.ok:
            return ""
        return resp.json().get("title", "") or ""
    except Exception:
        return ""


def thumbnail_url(video_id: str) -> str:
    return f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg"


def extract_playlist_id(url: str) -> str | None:
    """Return playlist ID only for pure playlist URLs (not single-video-in-playlist)."""
    if "watch" in url:
        return None
    match = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else None


def get_playlist_video_ids(playlist_id: str) -> list[str]:
    import subprocess
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--print", "id",
             f"https://www.youtube.com/playlist?list={playlist_id}"],
            capture_output=True, text=True, check=True, timeout=60,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except FileNotFoundError:
        raise RuntimeError("Playlist support requires yt-dlp. Install: pip install yt-dlp")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"yt-dlp failed: {e.stderr.strip()}")


def fetch_transcript(video_id: str, api_key: str, model: str = GROQ_MODEL,
                      usage_cb=None, usage_sync_cb=None) -> tuple[str, bool]:
    """Return (transcript_text, from_cache). usage_cb(tokens:int) fires for any
    Groq call made along the way (only happens if translation is needed)."""
    import os
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{video_id}.txt"
    if cache.exists():
        return cache.read_text(), True

    supadata_key = os.environ.get("SUPADATA_API_KEY", "")
    if supadata_key:
        text = _fetch_via_supadata(video_id, supadata_key)
    else:
        text = _fetch_via_yt_api(video_id, api_key, model, usage_cb, usage_sync_cb)

    cache.write_text(text)
    return text, False


def _fetch_via_supadata(video_id: str, supadata_key: str) -> str:
    import requests
    resp = requests.get(
        "https://api.supadata.ai/v1/youtube/transcript",
        params={"videoId": video_id, "text": "true"},
        headers={"x-api-key": supadata_key},
        timeout=30,
    )
    if not resp.ok:
        raise ValueError(f"Supadata error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    text = data.get("content") or data.get("text") or ""
    if not text:
        raise ValueError("Supadata returned an empty transcript.")
    text = re.sub(r"\[.*?\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_via_yt_api(video_id: str, api_key: str, model: str,
                       usage_cb=None, usage_sync_cb=None) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi
    api        = YouTubeTranscriptApi()
    translated = False
    try:
        tlist      = api.list(video_id)
        transcript = tlist.find_transcript(["en", "en-US", "en-GB"])
        segments   = transcript.fetch()
    except Exception:
        try:
            tlist      = api.list(video_id)
            transcript = tlist.find_generated_transcript(["en"])
            segments   = transcript.fetch()
        except Exception:
            try:
                tlist      = api.list(video_id)
                transcript = next(iter(tlist))
                segments   = transcript.fetch()
                translated = True
            except Exception as e:
                raise ValueError(f"Could not fetch transcript: {e}")

    text = " ".join(s.text for s in segments)
    text = re.sub(r"\[.*?\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    if translated:
        text = _call_groq(_TRANSLATE_PROMPT.format(transcript=text[:MAX_TRANSCRIPT_CHARS]),
                          api_key, model, max_tokens=1200,
                          usage_cb=usage_cb, usage_sync_cb=usage_sync_cb)
    return text


def _call_groq(prompt: str, api_key: str, model: str, max_tokens: int = 2048,
               retries: int = 2, system_prompt: str = _SYSTEM_PROMPT,
               usage_cb=None, usage_sync_cb=None) -> str:
    from groq import Groq, APIConnectionError, APIStatusError

    client = Groq(api_key=api_key)
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.1,
                max_tokens=max_tokens,
            )
            if usage_cb and resp.usage:
                try:
                    usage_cb(resp.usage.total_tokens)
                except Exception:
                    pass  # usage tracking must never break the summarize flow
            return resp.choices[0].message.content
        except (APIConnectionError, APIStatusError) as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
            else:
                # Our own token counter only sees calls made through this app —
                # it can't see other traffic on the same Groq account. On a rate
                # limit, the error body reports the account's true daily usage;
                # resync our tracker to that ground truth so /api/quota stays honest.
                if usage_sync_cb:
                    match = re.search(r"Used (\d+)", str(e))
                    if match:
                        try:
                            usage_sync_cb(int(match.group(1)))
                        except Exception:
                            pass
                raise RuntimeError(f"Groq API failed after {retries + 1} attempts: {e}")


def summarize(transcript: str, api_key: str, model: str = GROQ_MODEL,
              brief: bool = False, mode: str = "general",
              usage_cb=None, usage_sync_cb=None) -> str:
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS] + "\n[transcript truncated]"
    if mode == "stock":
        template      = _STOCK_BRIEF_PROMPT if brief else _STOCK_FULL_PROMPT
        system_prompt = _STOCK_SYSTEM_PROMPT
        max_tokens    = 600 if brief else 1400
    else:
        template      = _BRIEF_PROMPT if brief else _FULL_PROMPT
        system_prompt = _SYSTEM_PROMPT
        max_tokens    = 500 if brief else 1200
    prompt = template.format(transcript=transcript)
    return _call_groq(prompt, api_key, model, max_tokens=max_tokens,
                       system_prompt=system_prompt, usage_cb=usage_cb, usage_sync_cb=usage_sync_cb)



"""Core transcript fetching and summarization logic."""

from __future__ import annotations

import json
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
    "You are an expert summarizer for YouTube video transcripts of any kind — "
    "tutorials, lectures, podcasts, interviews, reviews, vlogs, and more. "
    "Write clear, accurate, detailed prose summaries. "
    "Never invent information that is not in the transcript. "
    "Never use headers, bullet points, or numbered lists — write in flowing paragraphs only."
)

_STOCK_SYSTEM_PROMPT = (
    "You are an expert financial analyst summarizing stock trading YouTube content. "
    "Write clear, accurate, detailed prose summaries. Be precise with numbers, tickers, and price levels. "
    "Never invent information that is not in the transcript. "
    "Never use headers, bullet points, or numbered lists — write in flowing paragraphs only."
)

_FULL_PROMPT = """\
Analyze this YouTube video transcript and write a single, detailed summary of it.

TRANSCRIPT:
{transcript}

Write one well-organized summary, as flowing prose (no headers, no bullet points, no numbered \
lists) that covers: what the video is about, the main ideas and arguments in the order they're \
presented, and the key takeaway. Aim for a thorough paragraph or two — detailed enough that \
someone who hasn't watched the video understands the substance of it, not just the topic.\
"""

_BRIEF_PROMPT = """\
Analyze this YouTube video transcript and write a short summary of it.

TRANSCRIPT:
{transcript}

Write 2-3 sentences of flowing prose (no headers, no bullet points, no lists) covering what the \
video is about and its main point.\
"""

_STOCK_FULL_PROMPT = """\
Analyze this YouTube video transcript and write a single, detailed trading-focused summary of it.

TRANSCRIPT:
{transcript}

Write one well-organized summary, as flowing prose (no headers, no bullet points, no numbered \
lists) that covers: the core investment or trading thesis, any stocks/tickers mentioned and the \
sentiment on each, specific price levels or targets if any were given, catalysts and time \
horizon, and key risks. If tickers or price levels weren't mentioned, just say so in passing \
rather than listing an empty section. Aim for a thorough paragraph or two.\
"""

_STOCK_BRIEF_PROMPT = """\
Analyze this YouTube video transcript and write a short trading-focused summary of it.

TRANSCRIPT:
{transcript}

Write 2-3 sentences of flowing prose (no headers, no bullet points, no lists) covering the core \
trading thesis and any tickers mentioned.\
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


def _clean(text: str) -> str:
    text = re.sub(r"\[.*?\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _group_into_lines(raw_segments: list[dict], max_chars: int = 130) -> list[dict]:
    """Merge small transcript fragments into readable sentence-ish lines with a
    start timestamp each, for a per-line timestamped transcript display."""
    lines: list[dict] = []
    buf, buf_start = [], None
    for seg in raw_segments:
        text = _clean(seg["text"])
        if not text:
            continue
        if buf_start is None:
            buf_start = seg["start"]
        buf.append(text)
        joined = " ".join(buf)
        if joined.rstrip().endswith((".", "?", "!")) or len(joined) >= max_chars:
            lines.append({"start": int(buf_start), "text": joined})
            buf, buf_start = [], None
    if buf:
        lines.append({"start": int(buf_start), "text": " ".join(buf)})
    return lines


def _synthetic_lines(text: str, words_per_line: int = 16, wpm: int = 155) -> list[dict]:
    """Fallback for transcript sources that only return flat text (no per-segment
    timing): split into evenly-paced lines using an average speaking rate."""
    words = text.split()
    lines = []
    for i in range(0, len(words), words_per_line):
        chunk = " ".join(words[i:i + words_per_line])
        start = int((i / wpm) * 60)
        lines.append({"start": start, "text": chunk})
    return lines


def fetch_transcript(video_id: str, api_key: str, model: str = GROQ_MODEL,
                      usage_cb=None, usage_sync_cb=None) -> tuple[str, bool, list[dict]]:
    """Return (transcript_text, from_cache, lines). `lines` is a list of
    {start:int seconds, text:str} for a timestamped transcript display —
    real per-segment timing when the source provides it, evenly-paced
    synthetic timing otherwise. usage_cb(tokens:int) fires for any Groq call
    made along the way (only happens if translation is needed)."""
    import os
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{video_id}.json"
    if cache.exists():
        data = json.loads(cache.read_text())
        return data["text"], True, data["lines"]

    supadata_key = os.environ.get("SUPADATA_API_KEY", "")
    if supadata_key:
        text, lines = _fetch_via_supadata(video_id, supadata_key)
    else:
        text, lines = _fetch_via_yt_api(video_id, api_key, model, usage_cb, usage_sync_cb)

    cache.write_text(json.dumps({"text": text, "lines": lines}))
    return text, False, lines


def _fetch_via_supadata(video_id: str, supadata_key: str) -> tuple[str, list[dict]]:
    import requests
    resp = requests.get(
        "https://api.supadata.ai/v1/youtube/transcript",
        params={"videoId": video_id},
        headers={"x-api-key": supadata_key},
        timeout=30,
    )
    if not resp.ok:
        raise ValueError(f"Supadata error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()

    content = data.get("content")
    if isinstance(content, list) and content:
        # Chunked response: [{text, offset, duration, lang}], offset in ms.
        raw = [{"start": c.get("offset", 0) / 1000, "text": c.get("text", "")} for c in content]
        text = _clean(" ".join(c.get("text", "") for c in content))
        lines = _group_into_lines(raw)
        if not text:
            raise ValueError("Supadata returned an empty transcript.")
        return text, lines

    flat = content if isinstance(content, str) else data.get("text", "")
    flat = _clean(flat)
    if not flat:
        raise ValueError("Supadata returned an empty transcript.")
    return flat, _synthetic_lines(flat)


def _fetch_via_yt_api(video_id: str, api_key: str, model: str,
                       usage_cb=None, usage_sync_cb=None) -> tuple[str, list[dict]]:
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

    raw  = [{"start": s.start, "text": s.text} for s in segments]
    text = _clean(" ".join(s.text for s in segments))

    if translated:
        text = _call_groq(_TRANSLATE_PROMPT.format(transcript=text[:MAX_TRANSCRIPT_CHARS]),
                          api_key, model, max_tokens=1200,
                          usage_cb=usage_cb, usage_sync_cb=usage_sync_cb)
        # Translated text no longer lines up with the original-language segment
        # timings word-for-word, so fall back to evenly-paced lines for display.
        lines = _synthetic_lines(text)
    else:
        lines = _group_into_lines(raw)

    return text, lines


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

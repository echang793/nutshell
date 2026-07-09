"""FastAPI web app for YouTube video note-taker."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import json as _json

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import summarizer

app = FastAPI(title="Nutshell")

STATIC_DIR  = Path(__file__).parent / "static"
CONFIG_PATH = Path.home() / ".yt-notes.json"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _get_api_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "")
    if key:
        return key
    try:
        import json
        key = json.loads(CONFIG_PATH.read_text()).get("groq_api_key", "")
    except (OSError, Exception):
        pass
    if not key:
        raise HTTPException(500, "GROQ_API_KEY not set. Run: python3 ~/Desktop/yt-notes/notes.py setup")
    return key


# ── Health ────────────────────────────────────────────────────────────

@app.get("/api/health")
def api_health():
    """Liveness + DB connectivity check. Returns real error message as JSON."""
    try:
        db._conn().execute("SELECT 1")
        db_status = "ok"
        db_error  = None
    except Exception as exc:
        db_status = "error"
        db_error  = f"{type(exc).__name__}: {exc}"
    return {
        "status":    "ok" if db_error is None else "degraded",
        "db":        db_status,
        "db_error":  db_error,
        "turso_url": bool(os.environ.get("TURSO_URL")),
        "turso_tok": bool(os.environ.get("TURSO_TOKEN")),
    }


@app.get("/api/quota")
def api_quota():
    SUPADATA_LIMIT = 100
    used      = db.get_monthly_summary_count()
    remaining = max(0, SUPADATA_LIMIT - used)
    return {"used": used, "limit": SUPADATA_LIMIT, "remaining": remaining}


# ── Static ────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ── Summarize ─────────────────────────────────────────────────────────

class SummarizeRequest(BaseModel):
    url:   str
    brief: bool = False
    model: str  = summarizer.GROQ_MODEL
    mode:  str  = "general"  # "general" or "stock"


@app.post("/api/summarize")
async def api_summarize(req: SummarizeRequest):
    api_key  = _get_api_key()
    video_id = summarizer.extract_video_id(req.url)
    if not video_id:
        raise HTTPException(400, f"Could not parse a video ID from: {req.url}")

    try:
        transcript, cached = await run_in_threadpool(
            summarizer.fetch_transcript, video_id, api_key, req.model
        )
    except Exception as e:
        raise HTTPException(400, str(e))

    word_count = len(transcript.split())

    mode = req.mode if req.mode in ("general", "stock") else "general"
    try:
        notes = await run_in_threadpool(
            summarizer.summarize, transcript, api_key, req.model, req.brief, mode
        )
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    title     = await run_in_threadpool(summarizer.fetch_video_title, video_id)
    thumb_url = summarizer.thumbnail_url(video_id)
    sid       = str(uuid.uuid4())[:8]

    await run_in_threadpool(
        db.save_summary, sid, video_id, req.url, notes, req.brief, word_count,
        title, thumb_url
    )

    return {
        "id":            sid,
        "video_id":      video_id,
        "url":           req.url,
        "title":         title,
        "thumbnail_url": thumb_url,
        "notes":         notes,
        "brief":         req.brief,
        "word_count":    word_count,
        "cached":        cached,
    }


@app.get("/api/summaries/{sid}")
def api_get_summary(sid: str):
    row = db.get_summary(sid)
    if not row:
        raise HTTPException(404, "Summary not found.")
    return row


@app.delete("/api/summaries/{sid}")
def api_delete_summary(sid: str):
    if not db.get_summary(sid):
        raise HTTPException(404, "Summary not found.")
    db.delete_summary(sid)
    return {"ok": True}


@app.get("/api/history")
def api_history(limit: int = 50):
    return db.get_history(limit)


@app.delete("/api/history")
def api_clear_history():
    db.clear_history()
    return {"ok": True}


# ── Playlist ──────────────────────────────────────────────────────────

class PlaylistRequest(BaseModel):
    url:   str
    brief: bool = False
    model: str  = summarizer.GROQ_MODEL
    mode:  str  = "general"  # "general" or "stock"


@app.post("/api/summarize/playlist")
async def api_summarize_playlist(req: PlaylistRequest):
    api_key = _get_api_key()
    mode    = req.mode if req.mode in ("general", "stock") else "general"

    playlist_id = summarizer.extract_playlist_id(req.url)
    if not playlist_id:
        raise HTTPException(400, "Not a playlist URL — use a youtube.com/playlist?list=... URL.")

    try:
        video_ids = await run_in_threadpool(summarizer.get_playlist_video_ids, playlist_id)
    except RuntimeError as e:
        raise HTTPException(400, str(e))

    if not video_ids:
        raise HTTPException(400, "No videos found in playlist.")

    async def generate():
        yield f"data: {_json.dumps({'type': 'start', 'total': len(video_ids)})}\n\n"
        for i, vid in enumerate(video_ids):
            vid_url = f"https://www.youtube.com/watch?v={vid}"
            try:
                transcript, cached = await run_in_threadpool(
                    summarizer.fetch_transcript, vid, api_key, req.model
                )
                notes = await run_in_threadpool(
                    summarizer.summarize, transcript, api_key, req.model, req.brief, mode
                )
                title     = await run_in_threadpool(summarizer.fetch_video_title, vid)
                thumb_url = summarizer.thumbnail_url(vid)
                sid       = str(uuid.uuid4())[:8]
                await run_in_threadpool(
                    db.save_summary, sid, vid, vid_url, notes, req.brief,
                    len(transcript.split()), title, thumb_url
                )
                payload = {
                    "type": "video", "index": i, "total": len(video_ids),
                    "id": sid, "video_id": vid, "url": vid_url,
                    "title": title, "thumbnail_url": thumb_url,
                    "notes": notes,
                    "word_count": len(transcript.split()),
                }
                yield f"data: {_json.dumps(payload)}\n\n"
            except Exception as e:
                yield f"data: {_json.dumps({'type': 'error', 'index': i, 'video_id': vid, 'url': vid_url, 'error': str(e)})}\n\n"
        yield f"data: {_json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

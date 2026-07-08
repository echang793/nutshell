"""FastAPI web app for YouTube video note-taker."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import json as _json

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import db
import payments
import summarizer

app = FastAPI(title="Nutshell")

STATIC_DIR  = Path(__file__).parent / "static"
CONFIG_PATH = Path.home() / ".yt-notes.json"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

SESSION_COOKIE   = "yt_session"
ANON_COOKIE      = "yt_anon"
ANON_LIMIT       = 2


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


def _current_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    return auth.get_user_from_token(token)


def _require_user(request: Request) -> dict:
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Not logged in.")
    return user


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


# ── Auth ──────────────────────────────────────────────────────────────

class AuthRequest(BaseModel):
    email:    str
    password: str


@app.post("/api/auth/register")
def api_register(req: AuthRequest, response: Response):
    email = req.email.strip().lower()
    if not email or not req.password:
        raise HTTPException(400, "Email and password are required.")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    if db.get_user_by_email(email):
        raise HTTPException(409, "An account with that email already exists.")

    uid           = str(uuid.uuid4())
    password_hash = auth.hash_password(req.password)
    user          = db.create_user(uid, email, password_hash)
    token         = auth.create_session(uid)

    response.set_cookie(SESSION_COOKIE, token, max_age=86400 * 30,
                        httponly=True, samesite="lax")
    return _user_payload(user)


@app.post("/api/auth/login")
def api_login(req: AuthRequest, response: Response):
    email = req.email.strip().lower()
    user  = db.get_user_by_email(email)
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password.")

    token = auth.create_session(user["id"])
    response.set_cookie(SESSION_COOKIE, token, max_age=86400 * 30,
                        httponly=True, samesite="lax")
    return _user_payload(user)


@app.post("/api/auth/logout")
def api_logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(request: Request):
    user = _current_user(request)
    if not user:
        return {"user": None}
    return {"user": _user_payload(user)}


def _user_payload(user: dict) -> dict:
    return {
        "id":            user["id"],
        "email":         user["email"],
        "plan":          user["plan"],
        "summary_count": user["summary_count"],
        "monthly_count": user["monthly_count"],
    }


# ── Summarize ─────────────────────────────────────────────────────────

class SummarizeRequest(BaseModel):
    url:   str
    brief: bool = False
    model: str  = summarizer.GROQ_MODEL
    mode:  str  = "general"  # "general" or "stock"


@app.post("/api/summarize")
async def api_summarize(req: SummarizeRequest, request: Request, response: Response):
    user     = _current_user(request)
    api_key  = _get_api_key()
    video_id = summarizer.extract_video_id(req.url)
    if not video_id:
        raise HTTPException(400, f"Could not parse a video ID from: {req.url}")

    if user:
        # Enforce plan limits for logged-in users
        allowed, reason = payments.can_summarize(user)
        if not allowed:
            raise HTTPException(402, reason)
    else:
        # Anonymous users get ANON_LIMIT free summaries tracked by cookie
        try:
            anon_count = int(request.cookies.get(ANON_COOKIE, "0"))
        except ValueError:
            anon_count = 0
        if anon_count >= ANON_LIMIT:
            raise HTTPException(401, "Create a free account to keep summarizing — it only takes 10 seconds.")
        response.set_cookie(ANON_COOKIE, str(anon_count + 1),
                            max_age=86400 * 30, httponly=True, samesite="lax")

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

    sid = str(uuid.uuid4())[:8]
    uid = user["id"] if user else None

    await run_in_threadpool(
        db.save_summary, sid, video_id, req.url, notes, req.brief, word_count, uid
    )

    if uid:
        await run_in_threadpool(db.increment_summary_count, uid)

    return {
        "id":         sid,
        "video_id":   video_id,
        "url":        req.url,
        "notes":      notes,
        "brief":      req.brief,
        "word_count": word_count,
        "cached":     cached,
    }


@app.get("/api/summaries/{sid}")
def api_get_summary(sid: str):
    row = db.get_summary(sid)
    if not row:
        raise HTTPException(404, "Summary not found.")
    return row


@app.get("/api/history")
def api_history(request: Request, limit: int = 50):
    user = _current_user(request)
    uid  = user["id"] if user else None
    return db.get_history(limit, user_id=uid)


# ── Payments ──────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    plan: str  # "basic" or "pro"


@app.post("/api/payments/checkout")
def api_checkout(req: CheckoutRequest, request: Request):
    user        = _require_user(request)
    base_url    = str(request.base_url).rstrip("/")
    success_url = f"{base_url}/"
    cancel_url  = f"{base_url}/"
    try:
        url = payments.create_checkout_session(user, req.plan, success_url, cancel_url)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"url": url}


@app.post("/api/payments/portal")
def api_portal(request: Request):
    user       = _require_user(request)
    return_url = str(request.base_url).rstrip("/") + "/"
    try:
        url = payments.create_portal_session(user, return_url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"url": url}


@app.post("/api/payments/webhook")
async def api_webhook(request: Request):
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        await run_in_threadpool(payments.handle_webhook, payload, sig_header)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ── Playlist ──────────────────────────────────────────────────────────

class PlaylistRequest(BaseModel):
    url:   str
    brief: bool = False
    model: str  = summarizer.GROQ_MODEL
    mode:  str  = "general"  # "general" or "stock"


@app.post("/api/summarize/playlist")
async def api_summarize_playlist(req: PlaylistRequest, request: Request):
    user    = _current_user(request)
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
                notes   = await run_in_threadpool(
                    summarizer.summarize, transcript, api_key, req.model, req.brief, mode
                )
                sid     = str(uuid.uuid4())[:8]
                uid     = user["id"] if user else None
                await run_in_threadpool(
                    db.save_summary, sid, vid, vid_url, notes, req.brief,
                    len(transcript.split()), uid
                )
                if uid:
                    await run_in_threadpool(db.increment_summary_count, uid)
                payload = {
                    "type": "video", "index": i, "total": len(video_ids),
                    "id": sid, "video_id": vid, "url": vid_url,
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

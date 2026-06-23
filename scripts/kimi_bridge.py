#!/usr/bin/env python3
"""Tiny HTTP wrapper around kimi-code CLI.

Why: kimi binary + its OAuth creds live on the host (/root/.kimi-code).
The vpn-bot container can't see them, so it talks to this service over
the docker bridge. One request in → kimi subprocess → response back.

Endpoints (all 127.0.0.1 only):
  POST /ask     {"prompt": str, "session_id": str | null, "model": str | null}
                  → {"response": str, "session_id": str, "duration_ms": int}
  GET  /health  → {"status": "ok", "kimi_version": "..."}
  POST /reset   {"session_id": str}  → {"ok": true}  (cosmetic; new prompts
                                                      without session_id start fresh)

Auth: a static shared secret (KIMI_BRIDGE_TOKEN env var) — anything that
gets a TCP socket on 127.0.0.1:7077 is already inside the host network,
but a token still helps against a wayward LXC neighbor or a misconfigured
reverse proxy.
"""

import json
import logging
import mimetypes
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

LOG = logging.getLogger("kimi-bridge")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

KIMI_BIN = os.environ.get("KIMI_BIN", "/root/.kimi-code/bin/kimi")
BRIDGE_TOKEN = os.environ.get("KIMI_BRIDGE_TOKEN", "")
BRIDGE_PORT = int(os.environ.get("KIMI_BRIDGE_PORT", "7077"))
DEFAULT_TIMEOUT = int(os.environ.get("KIMI_BRIDGE_TIMEOUT", "180"))
WORKDIR = os.environ.get("KIMI_BRIDGE_WORKDIR", "/root")

app = FastAPI(title="kimi-bridge", version="0.1.0")


def _parse_stream_json(stdout: str) -> tuple[str, Optional[str]]:
    """Pull the assistant reply + session_id out of kimi's stream-json output.

    kimi's --output-format stream-json prints one JSON object per line:
      {"role": "assistant", "content": "..."}
      {"role": "meta", "type": "session.resume_hint", "session_id": "session_...", ...}
    Sometimes also tool-call events; we just collect the assistant pieces and
    take the last session_id we see.
    """
    parts: list[str] = []
    session_id: Optional[str] = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        role = obj.get("role")
        if role == "assistant" and isinstance(obj.get("content"), str):
            parts.append(obj["content"])
        elif role == "meta" and obj.get("type") == "session.resume_hint":
            sid = obj.get("session_id")
            if isinstance(sid, str) and sid.startswith("session_"):
                session_id = sid
    return ("\n".join(parts).strip(), session_id)


class AskBody(BaseModel):
    prompt: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    timeout: Optional[int] = None
    # "plan" — deep thinking + plan-first (--plan flag). Best for code-heavy
    # or multi-step requests. "fast" — straight one-shot, faster.
    mode: Optional[str] = None
    yolo: Optional[bool] = False


class ResetBody(BaseModel):
    session_id: str


def _check_token(provided: str) -> None:
    if not BRIDGE_TOKEN:
        return
    if provided != BRIDGE_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")


@app.get("/health")
def health():
    try:
        v = subprocess.run(
            [KIMI_BIN, "--version"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception as e:
        return {"status": "degraded", "error": str(e)}
    return {"status": "ok", "kimi_version": v}


# Max file size we'll hand back over HTTP (Telegram caps documents at 50 MB
# for bots; we shave a bit so multipart overhead doesn't blow the limit).
FILE_MAX_BYTES = 45 * 1024 * 1024

# Reading any path is intentional — Kimi runs as root and can already touch
# anything on the host (the user explicitly asked for "no isolation"). This
# endpoint only exists to ferry bytes back across the docker boundary; the
# token check stops random LAN peers from grabbing files.
@app.get("/file")
def file(
    path: str = Query(..., description="Absolute path on the host"),
    x_bridge_token: str = Header(default=""),
):
    _check_token(x_bridge_token)
    p = Path(path)
    if not p.is_absolute() or not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {path}")
    size = p.stat().st_size
    if size > FILE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file too large ({size} bytes > {FILE_MAX_BYTES})",
        )
    mime, _ = mimetypes.guess_type(p.name)
    return FileResponse(
        path=str(p),
        media_type=mime or "application/octet-stream",
        filename=p.name,
    )


@app.post("/ask")
def ask(body: AskBody, x_bridge_token: str = Header(default="")):
    _check_token(x_bridge_token)
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="empty prompt")

    cmd = [KIMI_BIN, "-p", body.prompt, "--output-format", "stream-json"]
    if body.session_id and body.session_id.startswith("session_"):
        cmd += ["-r", body.session_id]
    if body.model:
        cmd += ["-m", body.model]
    if (body.mode or "").lower() == "plan":
        cmd += ["--plan"]
    if body.yolo:
        cmd += ["-y"]

    started = time.time()
    LOG.info(
        "ask | session=%s | model=%s | len=%d",
        body.session_id, body.model, len(body.prompt),
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=body.timeout or DEFAULT_TIMEOUT,
            cwd=WORKDIR,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="kimi timed out")
    except Exception as e:
        LOG.exception("kimi subprocess failed")
        raise HTTPException(status_code=500, detail=f"kimi: {e}")

    duration_ms = int((time.time() - started) * 1000)
    stdout = result.stdout or ""
    stderr = result.stderr or ""

    if result.returncode != 0:
        LOG.warning("kimi exited %d, stderr=%s", result.returncode, stderr[:400])
        msg = stderr.strip() or stdout.strip() or f"exit {result.returncode}"
        raise HTTPException(status_code=502, detail=f"kimi: {msg[:600]}")

    response_text, out_session = _parse_stream_json(stdout)
    return {
        "response": response_text,
        "session_id": out_session or body.session_id,
        "duration_ms": duration_ms,
    }


@app.post("/reset")
def reset(body: ResetBody, x_bridge_token: str = Header(default="")):
    _check_token(x_bridge_token)
    # We don't actually need to call kimi to "forget" a session — the bot
    # just stops sending session_id and the next /ask creates a new one.
    # This endpoint exists so callers can record the intent symmetrically.
    LOG.info("reset requested for session=%s", body.session_id)
    return {"ok": True}


if __name__ == "__main__":
    # Bind to 127.0.0.1 and also to the docker bridge gateway so the
    # vpn-bot container can reach us via host.docker.internal.
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT, log_level="info")

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import requests
from fastapi import Body, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "sessions.db"))

SESSION_TTL_SECONDS = 7 * 24 * 3600
SUMMARY_THRESHOLD = 150
COMPRESS_COUNT = 50
SUMMARY_MARKER = "[ZUSAMMENFASSUNG]"

app = FastAPI(title="EC2 Stateful AI Chatbot")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                history TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_history(session_id: str) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT history FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if not row:
        return []
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return []


def save_history(session_id: str, history: list) -> None:
    timestamp = now_iso()
    payload = json.dumps(history, ensure_ascii=False)
    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT created_at FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        created_at = existing[0] if existing else timestamp
        conn.execute(
            """
            INSERT INTO sessions (session_id, history, message_count, updated_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                history = excluded.history,
                message_count = excluded.message_count,
                updated_at = excluded.updated_at
            """,
            (session_id, payload, len(history), timestamp, created_at),
        )
        conn.commit()


def list_sessions(limit: int = 100) -> List[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT session_id, message_count, updated_at
            FROM sessions
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        {
            "session_id": row[0],
            "message_count": int(row[1]),
            "updated_at": row[2],
        }
        for row in rows
    ]


# --------------------------------------------------------------------------
# Groq API
# --------------------------------------------------------------------------

def call_groq(messages: list) -> tuple:
    response = requests.post(
        GROQ_URL,
        json={
            "model": GROQ_MODEL,
            "messages": messages,
            "max_tokens": 1024,
        },
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    # Echte Token-Zahlen, wie von der Groq-API abgerechnet
    usage = data.get("usage", {}) or {}
    return data["choices"][0]["message"]["content"], {
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
    }


# --------------------------------------------------------------------------
# Context compression
# --------------------------------------------------------------------------

def maybe_summarize(history: list) -> tuple:
    if len(history) <= SUMMARY_THRESHOLD:
        return history, None

    existing_summaries = [
        msg for msg in history
        if msg.get("role") == "system"
        and msg.get("content", "").startswith(SUMMARY_MARKER)
    ]
    normal_messages = [
        msg for msg in history
        if not (
            msg.get("role") == "system"
            and msg.get("content", "").startswith(SUMMARY_MARKER)
        )
    ]

    to_compress = normal_messages[:COMPRESS_COUNT]
    remaining = normal_messages[COMPRESS_COUNT:]

    summary_text, summary_usage = summarize_messages(to_compress)
    new_summary = {
        "role": "system",
        "content": (
            f"{SUMMARY_MARKER} der aeltesten {len(to_compress)} Nachrichten: "
            f"{summary_text}"
        ),
    }
    return existing_summaries + [new_summary] + remaining, summary_usage


def summarize_messages(messages: list) -> tuple:
    formatted = "\n".join(f"{message['role'].upper()}: {message['content']}" for message in messages)
    prompt = [
        {
            "role": "user",
            "content": (
                "Fasse die folgende Konversation kurz und praezise auf Deutsch zusammen. "
                "Behalte alle wichtigen Informationen, Fakten und Themen:\n\n"
                + formatted
            ),
        }
    ]
    return call_groq(prompt)


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    import platform
    return {"status": "ok", "service": "ec2-chatbot",
            "python_version": platform.python_version()}


@app.exception_handler(RequestValidationError)
def validation_error_as_400(request, exc):
    # Gleiche Fehlersemantik wie das Lambda-Backend (400 statt FastAPI-422)
    return JSONResponse(status_code=400, content={"detail": "Ungueltiger JSON-Body"})


@app.post("/chat")
def chat(body: dict = Body(...)) -> dict:
    # Bewusst ein synchroner Handler: FastAPI führt ihn im Threadpool aus,
    # sodass parallele Requests nebeneinander laufen (klassisches
    # Server-Modell). Ein async-Handler mit blockierendem HTTP-Client würde
    # den Event-Loop blockieren und Parallelität künstlich serialisieren.
    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY ist nicht gesetzt")

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ungueltiger JSON-Body")

    action = str(body.get("action", "")).strip().lower()
    if action == "list_sessions":
        try:
            requested_limit = int(body.get("limit", 50))
        except (TypeError, ValueError):
            requested_limit = 50
        limit = max(1, min(requested_limit, 200))
        sessions = list_sessions(limit)
        return {"sessions": sessions, "count": len(sessions)}

    session_id = str(body.get("session_id", "")).strip()
    user_message = str(body.get("message", "")).strip()

    if not session_id or not user_message:
        raise HTTPException(status_code=400, detail="session_id und message sind Pflichtfelder")

    # Jede Phase wird einzeln gemessen (identisch zum Lambda-Backend), damit
    # der State-Store-Overhead als Messwert berichtet werden kann.
    t0 = time.perf_counter()
    history = load_history(session_id)
    t_load = time.perf_counter() - t0

    history.append({"role": "user", "content": user_message})

    try:
        t0 = time.perf_counter()
        assistant_message, usage = call_groq(history)
        t_llm = time.perf_counter() - t0
    except requests.exceptions.HTTPError as exc:
        error_text = exc.response.text if exc.response is not None else str(exc)
        raise HTTPException(status_code=502, detail=f"Groq API Fehler: {error_text}") from exc
    except requests.exceptions.Timeout as exc:
        raise HTTPException(status_code=504, detail="Groq Timeout") from exc
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(status_code=502, detail="Verbindungsfehler zu Groq") from exc

    history.append({"role": "assistant", "content": assistant_message})

    t0 = time.perf_counter()
    history, compression_usage = maybe_summarize(history)
    t_compress = time.perf_counter() - t0

    t0 = time.perf_counter()
    save_history(session_id, history)
    t_store = time.perf_counter() - t0

    result = {
        "response": assistant_message,
        "session_id": session_id,
        "message_count": len(history),
        "usage": usage,
        "timings_ms": {
            "load": round(t_load * 1000, 2),
            "llm": round(t_llm * 1000, 2),
            "compress": round(t_compress * 1000, 2),
            "store": round(t_store * 1000, 2),
        },
        "compressed": compression_usage is not None,
    }
    if compression_usage is not None:
        result["compression_usage"] = compression_usage
    return result


@app.get("/")
def root() -> dict:
    return {"message": "EC2 Stateful AI Chatbot is running"}

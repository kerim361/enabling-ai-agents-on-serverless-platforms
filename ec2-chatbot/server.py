import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

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

def call_groq(messages: list) -> str:
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
    return response.json()["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------
# Context compression
# --------------------------------------------------------------------------

def maybe_summarize(history: list) -> list:
    if len(history) <= SUMMARY_THRESHOLD:
        return history

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

    summary_text = summarize_messages(to_compress)
    new_summary = {
        "role": "system",
        "content": (
            f"{SUMMARY_MARKER} der aeltesten {len(to_compress)} Nachrichten: "
            f"{summary_text}"
        ),
    }
    return existing_summaries + [new_summary] + remaining


def summarize_messages(messages: list) -> str:
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
    return {"status": "ok", "service": "ec2-chatbot"}


@app.post("/chat")
async def chat(request: Request) -> dict:
    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY ist nicht gesetzt")

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Ungueltiger JSON-Body") from exc

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

    history = load_history(session_id)
    history.append({"role": "user", "content": user_message})

    try:
        started_at = time.perf_counter()
        assistant_message = call_groq(history)
        elapsed_seconds = time.perf_counter() - started_at
    except requests.exceptions.HTTPError as exc:
        error_text = exc.response.text if exc.response is not None else str(exc)
        raise HTTPException(status_code=502, detail=f"Groq API Fehler: {error_text}") from exc
    except requests.exceptions.Timeout as exc:
        raise HTTPException(status_code=504, detail="Groq Timeout") from exc
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(status_code=502, detail="Verbindungsfehler zu Groq") from exc

    history.append({"role": "assistant", "content": assistant_message})
    history = maybe_summarize(history)
    save_history(session_id, history)

    return {
        "response": assistant_message,
        "session_id": session_id,
        "message_count": len(history),
        "elapsed_seconds": round(elapsed_seconds, 2),
    }


@app.get("/")
def root() -> dict:
    return {"message": "EC2 Stateful AI Chatbot is running"}

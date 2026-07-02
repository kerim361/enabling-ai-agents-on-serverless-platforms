import requests
import uuid
import os
import sys
import json
import time
import typing
from pathlib import Path
from datetime import datetime, timezone

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

SESSIONS_DIR      = Path("sessions")
SESSION_FILE      = ".session_id"
SUMMARY_THRESHOLD = 150
COMPRESS_COUNT    = 50
SUMMARY_MARKER    = "[ZUSAMMENFASSUNG]"


# --------------------------------------------------------------------------
# Lokale Session-Persistenz (JSON-Dateien statt DynamoDB)
# --------------------------------------------------------------------------

def load_session() -> str | None:
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE, "r") as f:
            return f.read().strip()
    return None


def save_session(session_id: str) -> None:
    with open(SESSION_FILE, "w") as f:
        f.write(session_id)


def load_history(session_id: str) -> list:
    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("history", [])
    return []


def save_history(session_id: str, history: list) -> None:
    SESSIONS_DIR.mkdir(exist_ok=True)
    path = SESSIONS_DIR / f"{session_id}.json"
    data = {
        "session_id": session_id,
        "history": history,
        "message_count": len(history),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_sessions(limit: int = 100) -> typing.List[dict]:
    SESSIONS_DIR.mkdir(exist_ok=True)
    sessions = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            sessions.append({
                "session_id": data.get("session_id", path.stem),
                "message_count": data.get("message_count", 0),
                "updated_at": data.get("updated_at", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue
    sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return sessions[:limit]


# --------------------------------------------------------------------------
# Groq API (direkt, ohne Lambda)
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
# Verlaufskomprimierung (identisch mit Lambda-Logik)
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
    remaining   = normal_messages[COMPRESS_COUNT:]

    summary_text = _summarize_messages(to_compress)
    new_summary = {
        "role": "system",
        "content": (
            f"{SUMMARY_MARKER} der ältesten {len(to_compress)} Nachrichten: "
            f"{summary_text}"
        ),
    }
    return existing_summaries + [new_summary] + remaining


def _summarize_messages(messages: list) -> str:
    formatted = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in messages
    )
    prompt = [{
        "role": "user",
        "content": (
            "Fasse die folgende Konversation kurz und präzise auf Deutsch zusammen. "
            "Behalte alle wichtigen Informationen, Fakten und Themen:\n\n"
            + formatted
        ),
    }]
    return call_groq(prompt)


# --------------------------------------------------------------------------
# UI (identisch mit client.py)
# --------------------------------------------------------------------------

def print_sessions(sessions: typing.List[dict], current_session: str | None = None) -> None:
    print("\nVerfügbare Sessions:")
    for index, session in enumerate(sessions, start=1):
        session_id    = session.get("session_id", "")
        message_count = session.get("message_count", 0)
        updated_at    = session.get("updated_at", "-")
        marker        = " (aktiv)" if current_session and session_id == current_session else ""
        print(f"  [{index}] {session_id} | msgs: {message_count} | updated: {updated_at}{marker}")
    print()


def choose_from_session_list(current_session: str | None) -> str | None:
    sessions = list_sessions()
    if not sessions:
        print("Keine Sessions gefunden.\n")
        return None

    print_sessions(sessions, current_session)
    valid_ids = {str(s.get("session_id", "")) for s in sessions}

    while True:
        user_value = input("Index oder Session-ID wählen (Enter = abbrechen): ").strip()
        if not user_value:
            return None

        if user_value.isdigit():
            idx = int(user_value)
            if 1 <= idx <= len(sessions):
                session_id = str(sessions[idx - 1].get("session_id", "")).strip()
                if session_id:
                    save_session(session_id)
                    print(f"Session gewechselt zu: {session_id}\n")
                    return session_id
            print("Ungültiger Index. Bitte erneut versuchen.")
            continue

        if user_value in valid_ids:
            save_session(user_value)
            print(f"Session gewechselt zu: {user_value}\n")
            return user_value

        print("Session nicht gefunden. Bitte Index oder exakte Session-ID eingeben.")


def start_new_session() -> str:
    session_id = str(uuid.uuid4())
    save_session(session_id)
    print(f"Neue Session gestartet: {session_id}\n")
    return session_id


def prompt_for_session_id(default_session: str | None) -> str:
    while True:
        raw_value = input("Session-ID eingeben: ").strip()
        if not raw_value:
            if default_session:
                print(f"Keine Eingabe. Letzte Session bleibt aktiv: {default_session}\n")
                return default_session
            print("Bitte eine gültige Session-ID eingeben.")
            continue
        save_session(raw_value)
        print(f"Session gewechselt zu: {raw_value}\n")
        return raw_value


def choose_session(existing_session: str | None) -> str:
    print("Session-Auswahl:")
    if existing_session:
        print(f"  [1] Letzte Session fortsetzen ({existing_session})")
        print("  [2] Session aus Liste wählen")
        print("  [3] Andere Session-ID eingeben")
        print("  [4] Neue Session starten")
        choice = input("Auswahl [1/2/3/4]: ").strip()
        if choice in ("", "1"):
            return existing_session
        if choice == "2":
            selected = choose_from_session_list(existing_session)
            return selected if selected else existing_session
        if choice == "3":
            return prompt_for_session_id(existing_session)
        return start_new_session()

    print("  [1] Session aus Liste wählen")
    print("  [2] Session-ID eingeben")
    print("  [3] Neue Session starten")
    choice = input("Auswahl [1/2/3]: ").strip()
    if choice in ("", "1"):
        selected = choose_from_session_list(None)
        return selected if selected else start_new_session()
    if choice == "2":
        return prompt_for_session_id(None)
    return start_new_session()


def switch_session(current_session: str) -> str:
    print("Session wechseln:")
    print("  [1] Session aus Liste wählen")
    print("  [2] Session-ID eingeben")
    print("  [3] Neue Session starten")
    choice = input("Auswahl [1/2/3]: ").strip()
    if choice in ("", "1"):
        selected = choose_from_session_list(current_session)
        return selected if selected else current_session
    if choice == "2":
        return prompt_for_session_id(current_session)
    return start_new_session()


# --------------------------------------------------------------------------
# Hauptprogramm
# --------------------------------------------------------------------------

def main():
    print("=== Stateful AI Chatbot (Lokal + Groq) ===\n")

    if not GROQ_API_KEY:
        print("FEHLER: GROQ_API_KEY ist nicht gesetzt!")
        print("  export GROQ_API_KEY='gsk_...'")
        sys.exit(1)

    existing_session = load_session()
    if existing_session:
        print(f"Letzte Session gefunden: {existing_session}\n")
    else:
        print("Noch keine lokale Session gespeichert.\n")

    session_id = choose_session(existing_session)

    print("Befehle: 'neu' = neue Session starten, 'session' = Session wechseln, 'ende' = Programm beenden\n")

    while True:
        try:
            user_input = input("Du: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAuf Wiedersehen!")
            break

        if not user_input:
            continue

        if user_input.lower() in ("ende", "quit", "exit"):
            print("Auf Wiedersehen!")
            break

        if user_input.lower() in ("neu", "new"):
            session_id = start_new_session()
            continue

        if user_input.lower() in ("session", "wechsel", "switch"):
            session_id = switch_session(session_id)
            continue

        history = load_history(session_id)
        history.append({"role": "user", "content": user_input})

        try:
            started_at        = time.perf_counter()
            assistant_message = call_groq(history)
            elapsed_seconds   = time.perf_counter() - started_at
        except requests.exceptions.HTTPError as e:
            print(f"\nHTTP-Fehler: {e.response.status_code} - {e.response.text}\n")
            continue
        except requests.exceptions.ConnectionError:
            print("\nVerbindungsfehler. Internet prüfen.\n")
            continue
        except requests.exceptions.Timeout:
            print("\nTimeout. Groq antwortet nicht.\n")
            continue
        except Exception as e:
            print(f"\nUnbekannter Fehler: {e}\n")
            continue

        history.append({"role": "assistant", "content": assistant_message})
        history = maybe_summarize(history)
        save_history(session_id, history)

        print(f"\nAssistent: {assistant_message}")
        print(f"(Nachrichten in dieser Session: {len(history)})\n")
        print(f"(Antwortzeit: {elapsed_seconds:.2f} Sekunden)\n")


if __name__ == "__main__":
    main()

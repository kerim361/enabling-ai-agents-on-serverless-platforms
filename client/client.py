import requests
import uuid
import os
import sys
import typing
import time

# Set your API Gateway URL here or via environment variable
API_URL = os.environ.get("API_URL", "https://YOUR_API_GATEWAY_ID.execute-api.eu-central-1.amazonaws.com/chat")
SESSION_FILE = ".session_id"


def load_session() -> str | None:
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE, "r") as f:
            return f.read().strip()
    return None


def save_session(session_id: str) -> None:
    with open(SESSION_FILE, "w") as f:
        f.write(session_id)

def fetch_sessions(limit: int = 50) -> typing.List[dict]:
    payload = {"action": "list_sessions", "limit": limit}
    response = requests.post(API_URL, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    sessions = data.get("sessions", [])
    if isinstance(sessions, list):
        return sessions
    return []


def print_sessions(sessions: typing.List[dict], current_session: str | None = None) -> None:
    print("\nVerfügbare Sessions:")
    for index, session in enumerate(sessions, start=1):
        session_id = session.get("session_id", "")
        message_count = session.get("message_count", 0)
        updated_at = session.get("updated_at", "-")
        marker = " (aktiv)" if current_session and session_id == current_session else ""
        print(f"  [{index}] {session_id} | msgs: {message_count} | updated: {updated_at}{marker}")
    print()


def choose_from_session_list(current_session: str | None) -> str | None:
    try:
        sessions = fetch_sessions(limit=100)
    except requests.exceptions.RequestException as e:
        print(f"Session-Liste konnte nicht geladen werden: {e}")
        print("Fallback: Bitte Session-ID manuell eingeben.\n")
        return prompt_for_session_id(current_session)

    if not sessions:
        print("Keine Sessions gefunden.\n")
        return None

    print_sessions(sessions, current_session)
    valid_ids = {str(session.get("session_id", "")) for session in sessions}

    while True:
        user_value = input("Index oder Session-ID wählen (Enter = abbrechen): ").strip()
        if not user_value:
            return None

        if user_value.isdigit():
            chosen_index = int(user_value)
            if 1 <= chosen_index <= len(sessions):
                session_id = str(sessions[chosen_index - 1].get("session_id", "")).strip()
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
            if selected:
                return selected
            return existing_session
        if choice == "3":
            return prompt_for_session_id(existing_session)
        return start_new_session()

    print("  [1] Session aus Liste wählen")
    print("  [2] Session-ID eingeben")
    print("  [3] Neue Session starten")
    choice = input("Auswahl [1/2/3]: ").strip()
    if choice in ("", "1"):
        selected = choose_from_session_list(None)
        if selected:
            return selected
        return start_new_session()
    if choice == "2":
        return prompt_for_session_id(None)
    return start_new_session()


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

        session_id = raw_value
        save_session(session_id)
        print(f"Session gewechselt zu: {session_id}\n")
        return session_id


def switch_session(current_session: str) -> str:
    print("Session wechseln:")
    print("  [1] Session aus Liste wählen")
    print("  [2] Session-ID eingeben")
    print("  [3] Neue Session starten")
    choice = input("Auswahl [1/2/3]: ").strip()

    if choice in ("", "1"):
        selected = choose_from_session_list(current_session)
        if selected:
            return selected
        return current_session
    if choice == "2":
        return prompt_for_session_id(current_session)
    return start_new_session()


def send_message(session_id: str, message: str) -> dict:
    payload = {"session_id": session_id, "message": message}
    response = requests.post(API_URL, json=payload, timeout=60)
    response.raise_for_status()
    return response.json()


def main():
    print("=== Stateful AI Chatbot (AWS Lambda + DynamoDB) ===\n")

    if API_URL.startswith("https://YOUR_API_GATEWAY"):
        print("FEHLER: Bitte setze die API_URL Variable.")
        print("  Entweder in dieser Datei oder als Umgebungsvariable:")
        print("  export API_URL=https://DEINE_ID.execute-api.eu-central-1.amazonaws.com/chat\n")
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

        try:
            started_at = time.perf_counter()
            data = send_message(session_id, user_input)
            elapsed_seconds = time.perf_counter() - started_at
            print(f"\nAgent: {data['response']}")
            print(f"(Nachrichten in dieser Session: {data.get('message_count', '?')})\n")
            print(f"(Antwortzeit: {elapsed_seconds:.2f} Sekunden)\n")
        except requests.exceptions.HTTPError as e:
            print(f"\nHTTP-Fehler: {e.response.status_code} - {e.response.text}\n")
        except requests.exceptions.ConnectionError:
            print("\nVerbindungsfehler. Ist die API_URL korrekt?\n")
        except requests.exceptions.Timeout:
            print("\nTimeout. Lambda-Funktion antwortet nicht.\n")
        except Exception as e:
            print(f"\nUnbekannter Fehler: {e}\n")


if __name__ == "__main__":
    main()

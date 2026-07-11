import json
import boto3
import time
import urllib.request
import urllib.error
import os
from datetime import datetime, timezone

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ.get("DYNAMODB_TABLE", "chat_sessions"))

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

SESSION_TTL_SECONDS = 7 * 24 * 3600
SUMMARY_THRESHOLD   = 150   # Ab dieser Nachrichtenanzahl wird komprimiert
COMPRESS_COUNT      = 50    # Älteste N Nachrichten werden zusammengefasst
SUMMARY_MARKER      = "[ZUSAMMENFASSUNG]"


def lambda_handler(event, context):
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _response(200, {})

    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Ungültiger JSON-Body"})

    action = body.get("action", "").strip().lower()
    if action == "list_sessions":
        try:
            requested_limit = int(body.get("limit", 50))
        except (TypeError, ValueError):
            requested_limit = 50
        limit = max(1, min(requested_limit, 200))
        try:
            sessions = _list_sessions(limit)
        except Exception as e:
            return _response(500, {"error": f"Session-Liste konnte nicht geladen werden: {str(e)}"})
        return _response(200, {"sessions": sessions, "count": len(sessions)})

    session_id   = body.get("session_id", "").strip()
    user_message = body.get("message", "").strip()

    if not session_id or not user_message:
        return _response(400, {"error": "session_id und message sind Pflichtfelder"})

    # Jede Phase wird einzeln gemessen, damit der Overhead des externalisierten
    # State (DynamoDB) als Messwert und nicht nur als Annahme berichtet werden kann.
    t0 = time.perf_counter()
    history = _load_history(session_id)
    t_load = time.perf_counter() - t0

    history.append({"role": "user", "content": user_message})

    try:
        t0 = time.perf_counter()
        assistant_message, usage = _call_groq(history)
        t_llm = time.perf_counter() - t0
    except Exception as e:
        return _response(502, {"error": f"Groq API Fehler: {str(e)}"})

    history.append({"role": "assistant", "content": assistant_message})

    # Nach jeder Antwort prüfen ob Komprimierung nötig ist
    t0 = time.perf_counter()
    history, compression_usage = _maybe_summarize(history)
    t_compress = time.perf_counter() - t0

    t0 = time.perf_counter()
    _save_history(session_id, history)
    t_store = time.perf_counter() - t0

    result = {
        "response": assistant_message,
        "session_id": session_id,
        "message_count": len(history),
        # Echte Token-Zahlen aus der Groq-API (statt chars/4-Schätzung)
        "usage": usage,
        # Serverseitige Phasen-Timings in Millisekunden
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
    return _response(200, result)


def _list_sessions(limit: int) -> list:
    expression_names = {
        "#sid": "session_id",
        "#mc": "message_count",
        "#ua": "updated_at",
        "#ttl": "ttl",
    }

    items = []
    scan_kwargs = {
        "ProjectionExpression": "#sid, #mc, #ua, #ttl",
        "ExpressionAttributeNames": expression_names,
    }

    while len(items) < limit:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    normalized = []
    for item in items:
        normalized.append({
            "session_id": item.get("session_id", ""),
            "message_count": int(item.get("message_count", 0) or 0),
            "updated_at": item.get("updated_at", ""),
            "ttl": int(item.get("ttl", 0) or 0),
        })

    normalized.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return normalized[:limit]


def _maybe_summarize(history: list) -> list:
    """
    Wenn der Verlauf über SUMMARY_THRESHOLD Nachrichten hat:
    Die ältesten COMPRESS_COUNT normalen Nachrichten (keine bestehenden
    Zusammenfassungen) werden per KI zusammengefasst und durch eine
    einzelne System-Nachricht ersetzt.
    """
    if len(history) <= SUMMARY_THRESHOLD:
        return history, None

    # Bestehende Zusammenfassungen und normale Nachrichten trennen
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

    # Älteste COMPRESS_COUNT normale Nachrichten komprimieren
    to_compress = normal_messages[:COMPRESS_COUNT]
    remaining   = normal_messages[COMPRESS_COUNT:]

    summary_text, summary_usage = _summarize_messages(to_compress)
    new_summary = {
        "role": "system",
        "content": (
            f"{SUMMARY_MARKER} der ältesten {len(to_compress)} Nachrichten: "
            f"{summary_text}"
        ),
    }

    # Aufbau: alle alten Zusammenfassungen + neue Zusammenfassung + verbleibende Nachrichten
    return existing_summaries + [new_summary] + remaining, summary_usage


def _summarize_messages(messages: list) -> tuple:
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
    return _call_groq(prompt)


def _call_groq(messages: list) -> tuple:
    payload = json.dumps({
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": 1024,
    }).encode("utf-8")

    req = urllib.request.Request(
        GROQ_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "User-Agent": "python-urllib/3.12",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise Exception(f"HTTP {e.code} von Groq: {error_body}")

    # Echte Token-Zahlen, wie von der Groq-API abgerechnet
    usage = data.get("usage", {}) or {}
    return data["choices"][0]["message"]["content"], {
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
    }


def _load_history(session_id: str) -> list:
    db_response = table.get_item(Key={"session_id": session_id})
    if "Item" in db_response:
        return db_response["Item"].get("history", [])
    return []


def _save_history(session_id: str, history: list) -> None:
    now = datetime.now(timezone.utc)
    table.put_item(Item={
        "session_id": session_id,
        "history": history,
        "message_count": len(history),
        "updated_at": now.isoformat(),
        "ttl": int(now.timestamp()) + SESSION_TTL_SECONDS,
    })


def _parse_body(event: dict) -> dict:
    body = event.get("body", "{}")
    if isinstance(body, str):
        return json.loads(body)
    if isinstance(body, dict):
        return body
    return {}


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }

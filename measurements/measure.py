#!/usr/bin/env python3
"""
Measurement harness for the bachelor thesis
"Enabling AI Agents on Stateless Serverless Platforms".

Compares the serverless system (AWS Lambda + DynamoDB) against the
server-based baseline (EC2 + FastAPI + SQLite). Both expose an identical
HTTP /chat interface and use the same Groq LLM, so the measurements isolate
the effect of the serverless, stateless execution model.

All raw per-request measurements are written to CSV so the evaluation chapter
can be reproduced from the data. No values are fabricated.

Usage examples
--------------
  # Experiment 1 (latency) against the Lambda system, 10 requests
  python measure.py --system lambda exp1 --n 10

  # Experiment 2 (cold vs warm). Cold starts are forced by updating the
  # Lambda env (invalidates warm containers); requires AWS CLI access.
  python measure.py --system lambda exp2 --n 10

  # Experiment 3 (conversation length scaling)
  python measure.py --system lambda exp3 --lengths 10 50 100 150

Environment / config
--------------------
  Endpoints are read from endpoints.json (see ENDPOINTS_DEFAULT below) or
  overridden via --url. The Groq key (for local token accounting only) is not
  needed here; the LLM is called server-side.
"""

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Default endpoints. Override per run with --url or via endpoints.json.
ENDPOINTS_DEFAULT = {
    "lambda": "https://1b9qjrvgy7.execute-api.eu-central-1.amazonaws.com/chat",
    "ec2": "",  # filled in once the EC2 baseline is deployed
}

LAMBDA_FUNCTION = "chatbot-handler"
AWS_REGION = "eu-central-1"

# Fixed query used across latency experiments to keep input size comparable.
SIMPLE_QUERY = "Hello, how are you?"


def load_endpoints():
    path = os.path.join(HERE, "endpoints.json")
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        return {**ENDPOINTS_DEFAULT, **data}
    return dict(ENDPOINTS_DEFAULT)


def post_chat(url, payload, timeout=90):
    """Send one /chat request and return (response_dict, wall_clock_seconds)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    elapsed = time.perf_counter() - start
    return json.loads(raw), elapsed


def new_session():
    return f"measure-{uuid.uuid4()}"


def force_lambda_cold_start():
    """
    Force the next invocation to be a cold start by updating an environment
    variable on the Lambda function. AWS tears down existing execution
    environments when the configuration changes, so the following request
    initializes a fresh container.
    """
    marker = str(int(time.time()))
    subprocess.run(
        ["aws", "lambda", "update-function-configuration",
         "--function-name", LAMBDA_FUNCTION,
         "--region", AWS_REGION,
         "--environment", f"Variables={{COLD_MARKER={marker}}}"],
        check=True, capture_output=True,
    )
    # The above replaces the env map, which would drop GROQ_API_KEY etc.
    # We therefore never call this without restore_lambda_env(); see exp2.


def get_lambda_env():
    out = subprocess.run(
        ["aws", "lambda", "get-function-configuration",
         "--function-name", LAMBDA_FUNCTION, "--region", AWS_REGION,
         "--query", "Environment.Variables", "--output", "json"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def set_lambda_env(env: dict):
    var_str = "Variables={" + ",".join(f"{k}={v}" for k, v in env.items()) + "}"
    subprocess.run(
        ["aws", "lambda", "update-function-configuration",
         "--function-name", LAMBDA_FUNCTION, "--region", AWS_REGION,
         "--environment", var_str],
        check=True, capture_output=True,
    )
    # Wait until the update is applied.
    subprocess.run(
        ["aws", "lambda", "wait", "function-updated",
         "--function-name", LAMBDA_FUNCTION, "--region", AWS_REGION],
        check=False, capture_output=True,
    )


def write_csv(name, rows, fieldnames):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(DATA_DIR, f"{name}_{stamp}.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    return path


def summarize(latencies):
    if not latencies:
        return {}
    return {
        "n": len(latencies),
        "mean_s": round(statistics.mean(latencies), 3),
        "min_s": round(min(latencies), 3),
        "max_s": round(max(latencies), 3),
        "median_s": round(statistics.median(latencies), 3),
        "stdev_s": round(statistics.stdev(latencies), 3) if len(latencies) > 1 else 0.0,
    }


# --------------------------------------------------------------------------
# Experiments
# --------------------------------------------------------------------------

def exp1_latency(url, system, n):
    """End-to-end latency over n consecutive requests on a fresh session."""
    session = new_session()
    rows = []
    print(f"[exp1] {system}: {n} requests, session={session}")
    for i in range(n):
        try:
            resp, elapsed = post_chat(url, {"session_id": session, "message": SIMPLE_QUERY})
            answer = resp.get("response", "")
            rows.append({
                "system": system, "request_index": i, "latency_s": round(elapsed, 4),
                "message_count": resp.get("message_count", ""),
                "response_chars": len(answer), "error": "",
            })
            print(f"  [{i+1}/{n}] {elapsed:.3f}s")
        except Exception as e:
            rows.append({"system": system, "request_index": i, "latency_s": "",
                         "message_count": "", "response_chars": "", "error": str(e)})
            print(f"  [{i+1}/{n}] ERROR: {e}")
    fields = ["system", "request_index", "latency_s", "message_count", "response_chars", "error"]
    path = write_csv(f"exp1_latency_{system}", rows, fields)
    lat = [r["latency_s"] for r in rows if isinstance(r["latency_s"], float)]
    print(f"[exp1] summary: {summarize(lat)}")
    print(f"[exp1] raw data -> {path}")
    return path


def exp2_cold_warm(url, system, n):
    """
    Cold vs warm latency. Warm = n consecutive requests. Cold = n requests,
    each preceded by forcing a fresh execution environment (Lambda only).
    """
    rows = []
    # --- Warm ---
    print(f"[exp2] {system}: WARM, {n} requests")
    session = new_session()
    # one priming request to ensure the container is warm
    post_chat(url, {"session_id": session, "message": SIMPLE_QUERY})
    for i in range(n):
        resp, elapsed = post_chat(url, {"session_id": session, "message": SIMPLE_QUERY})
        rows.append({"system": system, "mode": "warm", "request_index": i,
                     "latency_s": round(elapsed, 4), "error": ""})
        print(f"  warm [{i+1}/{n}] {elapsed:.3f}s")

    # --- Cold (Lambda only) ---
    if system == "lambda":
        print(f"[exp2] {system}: COLD, {n} requests (forcing fresh container each time)")
        base_env = get_lambda_env()
        try:
            for i in range(n):
                env = dict(base_env)
                env["COLD_MARKER"] = str(int(time.time() * 1000) + i)
                set_lambda_env(env)
                session = new_session()
                resp, elapsed = post_chat(url, {"session_id": session, "message": SIMPLE_QUERY})
                rows.append({"system": system, "mode": "cold", "request_index": i,
                             "latency_s": round(elapsed, 4), "error": ""})
                print(f"  cold [{i+1}/{n}] {elapsed:.3f}s")
        finally:
            # Always restore the original env (without COLD_MARKER).
            set_lambda_env(base_env)
            print("[exp2] restored original Lambda environment")

    fields = ["system", "mode", "request_index", "latency_s", "error"]
    path = write_csv(f"exp2_coldwarm_{system}", rows, fields)
    for mode in ("warm", "cold"):
        lat = [r["latency_s"] for r in rows if r["mode"] == mode and isinstance(r["latency_s"], float)]
        if lat:
            print(f"[exp2] {mode} summary: {summarize(lat)}")
    print(f"[exp2] raw data -> {path}")
    return path


def exp3_length_scaling(url, system, lengths, reps=5):
    """
    Latency as a function of conversation length. For each target length we
    build up a session to that many messages, then measure `reps` queries.
    """
    rows = []
    for target in lengths:
        session = new_session()
        # Build history up to `target` messages (each turn adds user+assistant).
        print(f"[exp3] {system}: building session to ~{target} messages")
        while True:
            resp, _ = post_chat(url, {"session_id": session, "message": "Continue the conversation briefly."})
            mc = resp.get("message_count", 0)
            if mc >= target:
                break
        print(f"[exp3] {system}: measuring at length={mc}")
        for i in range(reps):
            resp, elapsed = post_chat(url, {"session_id": session, "message": SIMPLE_QUERY})
            rows.append({"system": system, "target_length": target,
                         "actual_message_count": resp.get("message_count", ""),
                         "rep": i, "latency_s": round(elapsed, 4)})
            print(f"  len~{target} [{i+1}/{reps}] {elapsed:.3f}s")
    fields = ["system", "target_length", "actual_message_count", "rep", "latency_s"]
    path = write_csv(f"exp3_scaling_{system}", rows, fields)
    print(f"[exp3] raw data -> {path}")
    return path


def exp4_compression(url, system, max_messages=170):
    """
    Compression impact. Drive a session past the summarization threshold
    (>150 messages) while logging the message_count and latency of every turn.
    When the server compresses the oldest 50 messages into one summary, the
    message_count drops sharply; the magnitude of that drop quantifies the
    state-size reduction, and the latency of the compressing turn captures the
    cost of the extra summarization LLM call.
    """
    session = new_session()
    rows = []
    prev_count = 0
    print(f"[exp4] {system}: driving session past compression threshold")
    turn = 0
    while True:
        turn += 1
        resp, elapsed = post_chat(url, {"session_id": session,
                                        "message": "Continue the conversation briefly."})
        mc = resp.get("message_count", 0)
        compressed = mc < prev_count
        rows.append({"system": system, "turn": turn, "message_count": mc,
                     "prev_count": prev_count, "latency_s": round(elapsed, 4),
                     "compressed_here": int(compressed),
                     "count_drop": (prev_count - mc) if compressed else 0,
                     "response_chars": len(resp.get("response", ""))})
        flag = "  <== COMPRESSION" if compressed else ""
        print(f"  turn {turn}: count={mc} latency={elapsed:.3f}s{flag}")
        prev_count = mc
        # Stop a few turns after we have seen at least one compression event.
        if any(r["compressed_here"] for r in rows) and mc >= max_messages - 40:
            break
        if mc >= max_messages + 30:  # safety stop
            break
    fields = ["system", "turn", "message_count", "prev_count", "latency_s",
              "compressed_here", "count_drop", "response_chars"]
    path = write_csv(f"exp4_compression_{system}", rows, fields)
    drops = [r["count_drop"] for r in rows if r["compressed_here"]]
    if drops:
        print(f"[exp4] state-size reduction per compression: {drops} messages")
    print(f"[exp4] raw data -> {path}")
    return path


def exp5_cost_tokens(url, system, checkpoints=(2, 20, 60, 120)):
    """
    Token and cost accounting. We drive a session and, because the client knows
    every message it sends and receives, we mirror the conversation locally to
    estimate the input size (the full history re-sent on every stateless turn)
    and the output size. Tokens are approximated as chars/4 (as stated in the
    methodology); exact monetary cost is computed in the evaluation chapter from
    published Groq/AWS price tables using these token counts.
    """
    session = new_session()
    mirror = []  # local copy of the conversation history
    rows = []
    checkpoints = sorted(set(checkpoints))
    print(f"[exp5] {system}: token accounting at message counts {checkpoints}")
    turn = 0
    while checkpoints:
        turn += 1
        msg = SIMPLE_QUERY
        mirror.append({"role": "user", "content": msg})
        input_chars = sum(len(m["content"]) for m in mirror)
        resp, elapsed = post_chat(url, {"session_id": session, "message": msg})
        answer = resp.get("response", "")
        mirror.append({"role": "assistant", "content": answer})
        mc = resp.get("message_count", len(mirror))
        if mc >= checkpoints[0]:
            target = checkpoints.pop(0)
            in_tok = input_chars / 4
            out_tok = len(answer) / 4
            rows.append({"system": system, "checkpoint_msgs": target,
                         "actual_message_count": mc,
                         "input_chars": input_chars, "output_chars": len(answer),
                         "est_input_tokens": round(in_tok),
                         "est_output_tokens": round(out_tok),
                         "est_total_tokens": round(in_tok + out_tok),
                         "latency_s": round(elapsed, 4)})
            print(f"  checkpoint {target}: ~{round(in_tok)} in / {round(out_tok)} out tokens")
    fields = ["system", "checkpoint_msgs", "actual_message_count", "input_chars",
              "output_chars", "est_input_tokens", "est_output_tokens",
              "est_total_tokens", "latency_s"]
    path = write_csv(f"exp5_cost_{system}", rows, fields)
    print(f"[exp5] raw data -> {path}")
    return path


def main():
    ap = argparse.ArgumentParser(description="Thesis measurement harness")
    ap.add_argument("--system", choices=["lambda", "ec2"], default="lambda")
    ap.add_argument("--url", default=None, help="override endpoint URL")
    sub = ap.add_subparsers(dest="experiment", required=True)

    p1 = sub.add_parser("exp1", help="latency")
    p1.add_argument("--n", type=int, default=10)

    p2 = sub.add_parser("exp2", help="cold vs warm")
    p2.add_argument("--n", type=int, default=10)

    p3 = sub.add_parser("exp3", help="conversation length scaling")
    p3.add_argument("--lengths", type=int, nargs="+", default=[10, 50, 100, 150])
    p3.add_argument("--reps", type=int, default=5)

    sub.add_parser("exp4", help="compression impact")

    p5 = sub.add_parser("exp5", help="cost / token accounting")
    p5.add_argument("--checkpoints", type=int, nargs="+", default=[2, 20, 60, 120])

    args = ap.parse_args()
    endpoints = load_endpoints()
    url = args.url or endpoints.get(args.system, "")
    if not url:
        print(f"ERROR: no endpoint URL for system '{args.system}'. "
              f"Set it in endpoints.json or pass --url.", file=sys.stderr)
        sys.exit(1)

    if args.experiment == "exp1":
        exp1_latency(url, args.system, args.n)
    elif args.experiment == "exp2":
        exp2_cold_warm(url, args.system, args.n)
    elif args.experiment == "exp3":
        exp3_length_scaling(url, args.system, args.lengths, args.reps)
    elif args.experiment == "exp4":
        exp4_compression(url, args.system)
    elif args.experiment == "exp5":
        exp5_cost_tokens(url, args.system, tuple(args.checkpoints))


if __name__ == "__main__":
    main()

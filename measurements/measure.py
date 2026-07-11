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


def post_chat_retry(url, payload, timeout=90, retries=3, backoff=2.0):
    """post_chat with retries on transient upstream errors (5xx).

    Used only in the session-driving loops (exp3 build-up, exp4, exp5), where
    a transient LLM-provider error would otherwise abort a long campaign.
    The latency-measuring experiments (exp1/exp2/exp6) deliberately do NOT
    retry, because there an error is itself a data point.
    """
    for attempt in range(retries + 1):
        try:
            return post_chat(url, payload, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < retries:
                print(f"    (transient HTTP {e.code}, retry {attempt+1}/{retries})")
                time.sleep(backoff * (attempt + 1))
                continue
            raise


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

def exp1_latency(url, system, n, tag=""):
    """End-to-end latency over n consecutive requests on a fresh session.

    Besides the client-side wall clock, the server-side phase timings
    (state load, LLM call, state store) and the provider-reported token
    counts are recorded from the instrumented backend response.
    """
    session = new_session()
    rows = []
    print(f"[exp1] {system}{tag}: {n} requests, session={session}")
    # Priming request (not recorded): ensures the measured requests are
    # genuinely warm-state and no hidden cold start skews the statistics.
    post_chat(url, {"session_id": session, "message": SIMPLE_QUERY})
    for i in range(n):
        try:
            resp, elapsed = post_chat(url, {"session_id": session, "message": SIMPLE_QUERY})
            answer = resp.get("response", "")
            tm = resp.get("timings_ms", {}) or {}
            us = resp.get("usage", {}) or {}
            rows.append({
                "system": system, "request_index": i, "latency_s": round(elapsed, 4),
                "message_count": resp.get("message_count", ""),
                "response_chars": len(answer),
                "load_ms": tm.get("load", ""), "llm_ms": tm.get("llm", ""),
                "compress_ms": tm.get("compress", ""), "store_ms": tm.get("store", ""),
                "prompt_tokens": us.get("prompt_tokens", ""),
                "completion_tokens": us.get("completion_tokens", ""),
                "error": "",
            })
            print(f"  [{i+1}/{n}] {elapsed:.3f}s (load {tm.get('load','?')}ms, "
                  f"llm {tm.get('llm','?')}ms, store {tm.get('store','?')}ms)")
        except Exception as e:
            rows.append({"system": system, "request_index": i, "latency_s": "",
                         "message_count": "", "response_chars": "",
                         "load_ms": "", "llm_ms": "", "compress_ms": "", "store_ms": "",
                         "prompt_tokens": "", "completion_tokens": "", "error": str(e)})
            print(f"  [{i+1}/{n}] ERROR: {e}")
    fields = ["system", "request_index", "latency_s", "message_count", "response_chars",
              "load_ms", "llm_ms", "compress_ms", "store_ms",
              "prompt_tokens", "completion_tokens", "error"]
    path = write_csv(f"exp1_latency_{system}{tag}", rows, fields)
    lat = [r["latency_s"] for r in rows if isinstance(r["latency_s"], float)]
    print(f"[exp1] summary: {summarize(lat)}")
    print(f"[exp1] raw data -> {path}")
    return path


def exp2_cold_warm(url, system, n, tag=""):
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
    path = write_csv(f"exp2_coldwarm_{system}{tag}", rows, fields)
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
            resp, _ = post_chat_retry(url, {"session_id": session, "message": "Continue the conversation briefly."})
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
        resp, elapsed = post_chat_retry(url, {"session_id": session,
                                              "message": "Continue the conversation briefly."})
        mc = resp.get("message_count", 0)
        compressed = mc < prev_count
        comp_us = resp.get("compression_usage", {}) or {}
        rows.append({"system": system, "turn": turn, "message_count": mc,
                     "prev_count": prev_count, "latency_s": round(elapsed, 4),
                     "compressed_here": int(compressed),
                     "count_drop": (prev_count - mc) if compressed else 0,
                     "response_chars": len(resp.get("response", "")),
                     "compression_prompt_tokens": comp_us.get("prompt_tokens", ""),
                     "compression_completion_tokens": comp_us.get("completion_tokens", "")})
        flag = "  <== COMPRESSION" if compressed else ""
        print(f"  turn {turn}: count={mc} latency={elapsed:.3f}s{flag}")
        prev_count = mc
        # Stop a few turns after we have seen at least one compression event.
        if any(r["compressed_here"] for r in rows) and mc >= max_messages - 40:
            break
        if mc >= max_messages + 30:  # safety stop
            break
    fields = ["system", "turn", "message_count", "prev_count", "latency_s",
              "compressed_here", "count_drop", "response_chars",
              "compression_prompt_tokens", "compression_completion_tokens"]
    path = write_csv(f"exp4_compression_{system}", rows, fields)
    drops = [r["count_drop"] for r in rows if r["compressed_here"]]
    if drops:
        print(f"[exp4] state-size reduction per compression: {drops} messages")
    print(f"[exp4] raw data -> {path}")
    return path


def exp5_cost_tokens(url, system, checkpoints=(2, 20, 60, 120), reps=1):
    """
    Token and cost accounting. Two sources are recorded per checkpoint:

    (a) the *exact* token counts reported by the LLM provider in the API
        response (usage.prompt_tokens / usage.completion_tokens), and
    (b) the chars/4 estimate obtained by mirroring the conversation locally
        (kept for comparison with the estimation heuristic).

    The run is repeated `reps` times on independent fresh sessions so that
    per-checkpoint values can be averaged; conversation content is
    model-generated and varies between runs.
    """
    rows = []
    for rep in range(reps):
        session = new_session()
        mirror = []  # local copy of the conversation history
        remaining = sorted(set(checkpoints))
        print(f"[exp5] {system}: rep {rep+1}/{reps}, checkpoints {remaining}")
        turn = 0
        while remaining:
            turn += 1
            msg = SIMPLE_QUERY
            mirror.append({"role": "user", "content": msg})
            input_chars = sum(len(m["content"]) for m in mirror)
            resp, elapsed = post_chat_retry(url, {"session_id": session, "message": msg})
            answer = resp.get("response", "")
            mirror.append({"role": "assistant", "content": answer})
            mc = resp.get("message_count", len(mirror))
            us = resp.get("usage", {}) or {}
            if mc >= remaining[0]:
                target = remaining.pop(0)
                in_tok = input_chars / 4
                out_tok = len(answer) / 4
                rows.append({"system": system, "rep": rep,
                             "checkpoint_msgs": target,
                             "actual_message_count": mc,
                             "input_chars": input_chars, "output_chars": len(answer),
                             "est_input_tokens": round(in_tok),
                             "est_output_tokens": round(out_tok),
                             "est_total_tokens": round(in_tok + out_tok),
                             "real_prompt_tokens": us.get("prompt_tokens", ""),
                             "real_completion_tokens": us.get("completion_tokens", ""),
                             "latency_s": round(elapsed, 4)})
                print(f"  rep {rep+1} checkpoint {target}: "
                      f"real {us.get('prompt_tokens','?')} in / {us.get('completion_tokens','?')} out "
                      f"(est ~{round(in_tok)}/{round(out_tok)})")
    fields = ["system", "rep", "checkpoint_msgs", "actual_message_count", "input_chars",
              "output_chars", "est_input_tokens", "est_output_tokens",
              "est_total_tokens", "real_prompt_tokens", "real_completion_tokens",
              "latency_s"]
    path = write_csv(f"exp5_cost_{system}", rows, fields)
    print(f"[exp5] raw data -> {path}")
    return path


def exp6_concurrency(url, system, levels=(1, 2, 5, 10, 20), turns=4):
    """
    Horizontal scaling under concurrent load. For each concurrency level c,
    c independent clients (threads) each drive their own fresh session with
    `turns` sequential requests, mirroring the across-session scaling model
    of the system. Every request's latency and outcome is recorded, plus a
    per-level throughput/percentile summary.
    """
    import concurrent.futures

    rows = []
    summary_lines = []
    for level in levels:
        print(f"[exp6] {system}: concurrency={level}, {turns} turns per client")

        def worker(worker_id, _level=level):
            session = new_session()
            results = []
            for t in range(turns):
                try:
                    resp, elapsed = post_chat(
                        url, {"session_id": session, "message": SIMPLE_QUERY},
                        timeout=120)
                    results.append({
                        "system": system, "concurrency": _level, "worker": worker_id,
                        "turn": t, "latency_s": round(elapsed, 4),
                        "http_status": 200, "error": "",
                    })
                except urllib.error.HTTPError as e:
                    results.append({
                        "system": system, "concurrency": _level, "worker": worker_id,
                        "turn": t, "latency_s": "", "http_status": e.code,
                        "error": str(e)[:120],
                    })
                except Exception as e:
                    results.append({
                        "system": system, "concurrency": _level, "worker": worker_id,
                        "turn": t, "latency_s": "", "http_status": "",
                        "error": str(e)[:120],
                    })
            return results

        t_level = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=level) as ex:
            futures = [ex.submit(worker, w) for w in range(level)]
            level_rows = []
            for fut in concurrent.futures.as_completed(futures):
                level_rows.extend(fut.result())
        wall = time.perf_counter() - t_level
        rows.extend(level_rows)

        lat = sorted(r["latency_s"] for r in level_rows
                     if isinstance(r["latency_s"], float))
        errors = sum(1 for r in level_rows if r["error"])
        total = len(level_rows)
        if lat:
            p50 = statistics.median(lat)
            p95 = lat[min(len(lat) - 1, max(0, round(0.95 * len(lat)) - 1))]
            line = (f"c={level}: n={total}, errors={errors}, "
                    f"mean={statistics.mean(lat):.3f}s, p50={p50:.3f}s, "
                    f"p95={p95:.3f}s, max={max(lat):.3f}s, "
                    f"throughput={len(lat)/wall:.2f} req/s (wall {wall:.1f}s)")
        else:
            line = f"c={level}: n={total}, ALL FAILED ({errors} errors)"
        summary_lines.append(line)
        print(f"  {line}")
        time.sleep(2)  # settle between levels

    fields = ["system", "concurrency", "worker", "turn", "latency_s",
              "http_status", "error"]
    path = write_csv(f"exp6_concurrency_{system}", rows, fields)
    print(f"[exp6] summary:")
    for line in summary_lines:
        print(f"  {line}")
    print(f"[exp6] raw data -> {path}")
    return path


def main():
    ap = argparse.ArgumentParser(description="Thesis measurement harness")
    ap.add_argument("--system", choices=["lambda", "ec2"], default="lambda")
    ap.add_argument("--url", default=None, help="override endpoint URL")
    ap.add_argument("--tag", default="",
                    help="suffix for the output file name, e.g. '1gb' for a "
                         "memory-variant run (file becomes exp1_latency_lambda1gb_*)")
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
    p5.add_argument("--reps", type=int, default=1,
                    help="independent repetitions on fresh sessions")

    p6 = sub.add_parser("exp6", help="concurrent load / horizontal scaling")
    p6.add_argument("--levels", type=int, nargs="+", default=[1, 2, 5, 10, 20])
    p6.add_argument("--turns", type=int, default=4,
                    help="sequential requests per concurrent client")

    args = ap.parse_args()
    endpoints = load_endpoints()
    url = args.url or endpoints.get(args.system, "")
    if not url:
        print(f"ERROR: no endpoint URL for system '{args.system}'. "
              f"Set it in endpoints.json or pass --url.", file=sys.stderr)
        sys.exit(1)

    if args.experiment == "exp1":
        exp1_latency(url, args.system, args.n, tag=args.tag)
    elif args.experiment == "exp2":
        exp2_cold_warm(url, args.system, args.n, tag=args.tag)
    elif args.experiment == "exp3":
        exp3_length_scaling(url, args.system, args.lengths, args.reps)
    elif args.experiment == "exp4":
        exp4_compression(url, args.system)
    elif args.experiment == "exp5":
        exp5_cost_tokens(url, args.system, tuple(args.checkpoints), reps=args.reps)
    elif args.experiment == "exp6":
        exp6_concurrency(url, args.system, tuple(args.levels), args.turns)


if __name__ == "__main__":
    main()

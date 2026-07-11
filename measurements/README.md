# Measurements — Reproducibility Guide

This directory contains the complete measurement and analysis pipeline for the
bachelor thesis *"Enabling AI Agents on Stateless Serverless Platforms:
Feasibility and Limitations"*. Everything required to reproduce the figures and
numbers in the Evaluation chapter (Chapter 6) is here: the measurement harness,
the analysis/plotting script, the raw data, and the generated figures.

The evaluation compares two functionally identical systems that expose the same
HTTP `/chat` interface and use the same Groq LLM:

| System | Compute | State store |
|--------|---------|-------------|
| **Lambda** (serverless) | AWS Lambda, `python3.12`, 256 MB | DynamoDB (`chat_sessions`) |
| **EC2** (server baseline) | EC2 `t3.micro`, FastAPI + Uvicorn | SQLite (local file) |

Both run in `eu-central-1` (Frankfurt) and use `llama-3.3-70b-versatile` via Groq.

---

## Directory layout

```
measurements/
  measure.py        # measurement harness (experiments 1–6)
  analyze.py        # aggregates the raw CSVs and renders the PDF figures
  run_campaign.sh   # runs the complete campaign (all experiments, both systems)
  endpoints.json    # the /chat endpoint URLs for each system
  data/             # raw per-request CSV output + Lambda REPORT log (cost basis)
  figures/          # generated PDF figures (also copied into the thesis figures/)
  venv/             # Python venv with matplotlib + numpy (for analyze.py)
  .groq_key         # Groq API key (gitignored, not committed)
```

## Prerequisites

- Python 3.12+ (`measure.py` uses only the standard library).
- For `analyze.py`: `matplotlib` and `numpy` (installed in `venv/`).
- AWS CLI configured (`aws sts get-caller-identity` must succeed) — required only
  for Experiment 2, which forces Lambda cold starts via the AWS API.
- A deployed Lambda system. The EC2 baseline is deployed on demand for a
  measurement campaign and terminated afterwards (see the thesis Implementation
  chapter and `../setup/deploy.sh`).

The endpoints are read from `endpoints.json`:

```json
{
  "lambda": "https://<api-id>.execute-api.eu-central-1.amazonaws.com/chat",
  "ec2":    "http://<ec2-ip>:8000/chat"
}
```

---

## The experiments

All experiments are driven by `measure.py`. Each run writes a timestamped CSV to
`data/` containing the **raw, per-request** measurements (no pre-aggregation), so
the analysis is fully auditable.

| # | Subcommand | What it measures | Key output columns |
|---|-----------|------------------|--------------------|
| 1 | `exp1` | Warm end-to-end latency + server-side phase timings | `latency_s`, `load_ms`, `llm_ms`, `store_ms`, `prompt_tokens` |
| 2 | `exp2` | Cold vs. warm latency (Lambda only for cold) | `mode`, `latency_s` |
| 3 | `exp3` | Latency as a function of conversation length | `actual_message_count`, `latency_s` |
| 4 | `exp4` | Compression event: state-size drop, compress-turn latency + tokens | `message_count`, `count_drop`, `compression_prompt_tokens` |
| 5 | `exp5` | Token usage vs. conversation length (provider-reported + chars/4 estimate), repeated on independent sessions | `real_prompt_tokens`, `real_completion_tokens`, `est_input_tokens`, `rep` |
| 6 | `exp6` | Concurrent load across independent sessions (horizontal scaling) | `concurrency`, `latency_s`, `http_status`, `error` |

### Methodological notes

- **Latency** is end-to-end wall-clock time, measured client-side with
  `time.perf_counter()` around each HTTP request. In addition, the instrumented
  backends report per-phase timings (state load, LLM call, compression, state
  store) and the provider's exact token usage in every response.
- **Warm runs** are preceded by one unrecorded priming request, so no hidden
  cold start skews the warm statistics.
- **Cold starts** (Exp 2) are forced by updating a Lambda environment variable,
  which invalidates warm execution environments so the next invocation
  initializes a fresh container. The original environment is always restored
  afterwards (`get_lambda_env` / `set_lambda_env` in `measure.py`).
- **Tokens** (Exp 5) come primarily from the LLM provider's own accounting
  (`usage.prompt_tokens` / `usage.completion_tokens`, passed through by the
  backends). The local `characters / 4` estimate is still recorded for
  comparison with the heuristic (it reaches only ~0.7–0.8 of the true count).
- **Cost basis**: after a campaign, `run_campaign.sh` exports the Lambda
  `REPORT` log lines (billed duration, max memory used, init duration) from
  CloudWatch into `data/`; `analyze.py` aggregates them.
- Experiments 1–5 run **sequentially**; Experiment 6 issues concurrent
  requests by design (levels 1/2/5/10/20, each client on its own session).
  Transient upstream 5xx errors are retried only in session build-up loops,
  never in latency-measuring requests.

---

## Reproducing the full campaign

The complete campaign (all experiments, both systems, plus a 1-GB Lambda
memory variant and the CloudWatch REPORT export) is scripted:

```bash
./run_campaign.sh
```

Individual experiments:

```bash
# 0. activate the analysis venv (only needed for analyze.py)
#    measure.py itself needs no third-party packages.

# 1. Experiment 1 — warm latency + phase timings, both systems
python3 measure.py --system lambda exp1 --n 20
python3 measure.py --system ec2    exp1 --n 20

# 2. Experiment 2 — cold vs warm (Lambda does both; EC2 warm only)
python3 measure.py --system lambda exp2 --n 20

# 3. Experiment 3 — conversation length scaling (below compression threshold)
python3 measure.py --system lambda exp3 --lengths 10 50 100 140 --reps 5
python3 measure.py --system ec2    exp3 --lengths 10 50 100 140 --reps 5

# 4. Experiment 4 — compression impact
python3 measure.py --system lambda exp4
python3 measure.py --system ec2    exp4

# 5. Experiment 5 — token / cost accounting (5 independent sessions)
python3 measure.py --system lambda exp5 --reps 5
python3 measure.py --system ec2    exp5 --reps 5

# 6. Experiment 6 — concurrent load / horizontal scaling
python3 measure.py --system lambda exp6 --levels 1 2 5 10 20 --turns 4
python3 measure.py --system ec2    exp6 --levels 1 2 5 10 20 --turns 4

# 7. Aggregate + render all figures
./venv/bin/python analyze.py
```

`analyze.py` picks the most recent CSV per experiment and system, prints the
aggregate statistics (mean, median, min, max, standard deviation), and writes the
figures to `figures/`:

| Figure | File | Thesis |
|--------|------|--------|
| Warm latency distribution | `exp1_latency.pdf` | Fig. 4.1 |
| Cold vs warm | `exp2_coldwarm.pdf` | Fig. 4.2 |
| Length scaling | `exp3_scaling.pdf` | Fig. 4.3 |
| Compression sawtooth | `exp4_compression.pdf` | Fig. 4.4 |
| Token growth (provider-reported) | `exp5_tokens.pdf` | Fig. 4.5 |
| Concurrency: latency + throughput | `exp6_concurrency.pdf` | Fig. 4.6 |

After running, copy the figures into the thesis:

```bash
cp figures/*.pdf "../../Enabling_AI_Agents_on_Serverless_Platforms/figures/"
```

---

## Headline results (from the campaign reported in the thesis)

| Metric | Lambda (serverless) | EC2 (server) |
|--------|---------------------|--------------|
| Warm latency (mean, n=20) | 0.460 s | 0.422 s |
| State access per turn (load+store, measured) | 16.8 ms (DynamoDB) | 7.3 ms (SQLite) |
| Cold-start latency (mean, n=20) | 1.205 s | — (always warm) |
| Concurrency (independent sessions) | linear scaling, stable p50, up to account quota (10) | linear up to c=20 (I/O-bound) |
| Compression state reduction | 47 messages (152→103) | 47 messages (152→103) |

Under warm conditions the two systems are statistically indistinguishable;
the measured price of state externalization is ~10 ms per turn, and the
managed request path adds ~95 ms — both invisible under the LLM call's own
variance. Cold starts cost ~0.57 s of latency but no money (the init phase is
not billed). Token usage grows with conversation length **on both
architectures alike** (both re-send the full history to the LLM API) and is
bounded by compression. The LLM call dominates per-turn cost (>99 %); the
entire serverless infrastructure (API Gateway + Lambda + DynamoDB) contributes
~0.7 %. See the thesis evaluation chapter for the full discussion and the
cost breakdown.

## Notes & limitations

- Absolute token counts at large conversation lengths vary strongly between
  runs because model-generated conversation lengths are heavy-tailed; the
  growth *mechanism* and the tokenization density are architecture-independent
  (measured: 0.27 vs. 0.24 tokens per character of history).
- The serverless concurrency measurement is truncated at the account's Lambda
  concurrency quota (10 for this account); beyond it the platform rejects
  requests with HTTP 503 while keeping admitted requests fast.
- Cold starts are reproduced via configuration changes rather than natural
  container aging.
- The serverless path is measured through API Gateway over HTTPS, the baseline
  over plain HTTP directly against the instance.

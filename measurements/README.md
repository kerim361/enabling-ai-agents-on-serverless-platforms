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
  measure.py        # measurement harness (experiments 1–5)
  analyze.py        # aggregates the raw CSVs and renders the PDF figures
  endpoints.json    # the /chat endpoint URLs for each system
  data/             # raw per-request CSV output (one file per experiment run)
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
| 1 | `exp1` | Warm end-to-end latency over N consecutive requests | `latency_s` |
| 2 | `exp2` | Cold vs. warm latency (Lambda only for cold) | `mode`, `latency_s` |
| 3 | `exp3` | Latency as a function of conversation length | `actual_message_count`, `latency_s` |
| 4 | `exp4` | Compression event: state-size drop + compress-turn latency | `message_count`, `compressed_here`, `count_drop` |
| 5 | `exp5` | Token usage vs. conversation length (cost driver) | `est_input_tokens`, `est_output_tokens` |

### Methodological notes

- **Latency** is end-to-end wall-clock time, measured client-side with
  `time.perf_counter()` around each HTTP request. It therefore includes network,
  API Gateway / Uvicorn, the backend, the state store, and the LLM call.
- **Cold starts** (Exp 2) are forced by updating a Lambda environment variable,
  which invalidates warm execution environments so the next invocation
  initializes a fresh container. The original environment is always restored
  afterwards (`get_lambda_env` / `set_lambda_env` in `measure.py`).
- **Tokens** (Exp 5) are approximated as `characters / 4`, as stated in the
  methodology. The harness mirrors the conversation locally to estimate the
  full input size that is re-sent on every stateless turn.
- All runs are **sequential** (no parallel requests) to avoid self-interference.

---

## Reproducing the full campaign

```bash
# 0. activate the analysis venv (only needed for analyze.py)
#    measure.py itself needs no third-party packages.

# 1. Experiment 1 — warm latency, both systems
python3.12 measure.py --system lambda exp1 --n 20
python3.12 measure.py --system ec2    exp1 --n 20

# 2. Experiment 2 — cold vs warm (Lambda does both; EC2 warm only)
python3.12 measure.py --system lambda exp2 --n 8

# 3. Experiment 3 — conversation length scaling
python3.12 measure.py --system lambda exp3 --lengths 10 50 100 150 --reps 5
python3.12 measure.py --system ec2    exp3 --lengths 10 50 100 150 --reps 5

# 4. Experiment 4 — compression impact
python3.12 measure.py --system lambda exp4
python3.12 measure.py --system ec2    exp4

# 5. Experiment 5 — token / cost accounting
python3.12 measure.py --system lambda exp5
python3.12 measure.py --system ec2    exp5

# 6. Aggregate + render all figures
./venv/bin/python analyze.py
```

`analyze.py` picks the most recent CSV per experiment and system, prints the
aggregate statistics (mean, median, min, max, standard deviation), and writes the
figures to `figures/`:

| Figure | File | Thesis |
|--------|------|--------|
| Warm latency distribution | `exp1_latency.pdf` | Fig. 6.1 |
| Cold vs warm | `exp2_coldwarm.pdf` | Fig. 6.2 |
| Length scaling | `exp3_scaling.pdf` | Fig. 6.3 |
| Compression sawtooth | `exp4_compression.pdf` | Fig. 6.4 |
| Token growth | `exp5_tokens.pdf` | Fig. 6.5 |

After running, copy the figures into the thesis:

```bash
cp figures/*.pdf "../../Enabling_AI_Agents_on_Serverless_Platforms/figures/"
```

---

## Headline results (from the campaign reported in the thesis)

| Metric | Lambda (serverless) | EC2 (server) |
|--------|---------------------|--------------|
| Warm latency (mean) | 0.476 s | 0.505 s |
| Cold-start latency (mean) | 1.248 s | — (always warm) |
| Compression state reduction | 47 messages | 47 messages |

Under warm conditions the two systems are statistically comparable; the
serverless model's distinct costs are the cold-start penalty (~0.7 s) and
token growth with conversation length, the latter bounded by compression. The
LLM call dominates per-turn cost (~99 %); the serverless infrastructure
(Lambda + DynamoDB) contributes under one percent. See Chapter 6 for the full
discussion and the cost breakdown.

## Notes & limitations

- The absolute token counts in Exp 5 differ slightly between systems because the
  conversation content is model-generated and not byte-identical across runs; the
  relevant result is the growth *trend*, which is architecture-independent.
- Cold starts are reproduced via configuration changes rather than natural
  container aging.
- Experiments were run from a single client; concurrent multi-client load is left
  as future work.

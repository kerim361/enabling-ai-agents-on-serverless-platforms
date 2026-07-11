# Enabling AI Agents on Stateless Serverless Platforms

Code and measurement artifacts for the bachelor thesis
**"Enabling AI Agents on Stateless Serverless Platforms: Feasibility and Limitations"**
(TU Berlin, Service-centric Networking — SNET).

## What this is

LLM-based agents need conversational state, but serverless platforms are
stateless by design. This project studies whether — and at what cost — an AI
agent can run on stateless serverless infrastructure by **externalizing its
state**, and quantifies the trade-offs against a classical server deployment.

Two functionally identical agent backends expose the same HTTP `/chat`
interface and use the same LLM (`llama-3.3-70b-versatile` via the Groq API):

| | **Serverless (system under test)** | **Server baseline** |
|---|---|---|
| Compute | AWS Lambda (`python3.12`, 256 MB) | EC2 `t3.micro`, FastAPI + Uvicorn |
| State store | DynamoDB (`chat_sessions` table) | SQLite (local file) |
| State model | Stateless compute, state externalized per request | Stateful process, local persistence |
| Region | `eu-central-1` (Frankfurt) | `eu-central-1` (Frankfurt) |

Both are compared on **latency, cold starts, scaling with conversation
length, cost, and concurrent load** (Experiments 1–6, see
[`measurements/`](measurements/)). Both backends are instrumented: every
response carries the provider-reported token usage and per-phase server
timings (state load, LLM call, compression, state store).

### How the agent works

- ReAct-style loop with **one reasoning step per invocation**: each `/chat`
  request loads the session history, appends the user message, performs one
  LLM call, appends the answer, and re-persists the state.
- State is addressed by a client-chosen `session_id`; on Lambda every
  invocation rebuilds the full agent state from DynamoDB (load → mutate → store).
- **History compression** bounds state growth: once a conversation exceeds
  150 messages, the oldest 50 are replaced by an LLM-generated summary that is
  kept as a system message.

## Repository layout

```
lambda/          Serverless backend: AWS Lambda handler (stdlib + boto3 only)
ec2-chatbot/     Server baseline: FastAPI + SQLite (see its README)
local-chatbot/   Local variant: same agent loop, JSON-file state, no AWS needed
client/          Interactive CLI client (measures end-to-end latency per turn)
setup/           deploy.sh (serverless stack) + deploy_ec2.sh (baseline
                 instance via user-data bootstrap) + IAM policies
measurements/    Measurement harness (exp1–exp6), campaign script, raw data
                 (CSV + Lambda REPORT log), analysis & figures
```

## Quickstart

### Prerequisites

- Python 3.12+
- A [Groq API key](https://console.groq.com) (free tier is sufficient)
- For the serverless system: AWS CLI configured (`aws sts get-caller-identity`
  must succeed) and `zip` installed

### Option A — serverless system on AWS (the system under test)

`setup/deploy.sh` provisions everything in one idempotent run: the DynamoDB
table (with TTL), the IAM role and policies, the Lambda function, and an API
Gateway HTTP API with the `POST /chat` route.

```bash
cd setup
export GROQ_API_KEY="gsk_..."          # your Groq key
./deploy.sh                             # prints the API URL when done
```

Then chat with the agent:

```bash
cd ../client
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export API_URL="https://<api-id>.execute-api.eu-central-1.amazonaws.com/chat"
python client.py
```

Or test the API directly:

```bash
curl -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "demo-1", "message": "Hello!"}'
```

The response contains `response`, `session_id`, and `message_count`.
Sending the same `session_id` again continues the conversation — the
state round-trips through DynamoDB on every request.

### Option B — no AWS account needed

The local variant runs the identical agent loop with JSON-file state:

```bash
cd local-chatbot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export GROQ_API_KEY="gsk_..."
python chatbot.py
```

### Option C — server baseline (EC2 / any Linux host)

The baseline instance used in the evaluation is deployed fully automatically:

```bash
cd setup
export GROQ_API_KEY="gsk_..."
./deploy_ec2.sh        # t3.micro + Python 3.12 + systemd service, prints URL
```

Remember to terminate the instance afterwards (the script prints the
command). See [`ec2-chatbot/README.md`](ec2-chatbot/README.md) for running
the baseline locally instead.

## Reproducing the evaluation

The complete measurement pipeline — harness, methodology notes, per-request
raw CSVs of the reported campaign, and the plotting script — is documented in
[`measurements/README.md`](measurements/README.md). In short:

```bash
cd measurements
./run_campaign.sh                                   # full campaign, both systems
# or individually: python3 measure.py --system lambda exp1 --n 20   (exp1–exp6)
./venv/bin/python analyze.py                        # aggregates + renders figures
```

Headline results from the campaign reported in the thesis:

| Metric | Lambda (serverless) | EC2 (server) |
|--------|---------------------|--------------|
| Warm latency (mean, n=20) | 0.460 s | 0.422 s |
| State access per turn (measured) | 16.8 ms (DynamoDB) | 7.3 ms (SQLite) |
| Cold-start latency (mean, n=20) | 1.205 s | — (always warm) |
| Concurrent sessions | linear scaling, stable p50, up to account quota | linear up to c=20 |

Under warm conditions the two architectures are statistically
indistinguishable — the measured price of state externalization is ~10 ms per
turn, hidden beneath the LLM call's own variance. Cold starts cost ~0.57 s of
latency but no money (the init phase is not billed). Token usage grows with
conversation length on both architectures alike and is bounded by history
compression. The LLM call dominates per-turn cost (>99 %); the entire
serverless infrastructure (API Gateway + Lambda + DynamoDB) contributes
about 0.7 %.

## Cost note

All AWS resources are pay-per-request (Lambda, DynamoDB on-demand, API
Gateway); running the full sequential measurement campaign causes only
negligible AWS charges. The dominant cost driver is the LLM API. Remember to
delete the stack (Lambda, API Gateway, DynamoDB table, IAM role) when done.

## Relation to the thesis

The thesis documents the concept and design (Ch. 4), the implementation
(Ch. 5), and the evaluation with all five experiments (Ch. 6); the appendix
contains a step-by-step reproduction guide matching this repository. The
endpoints used in the reported campaign are recorded in
`measurements/endpoints.json`; the EC2 instance was terminated after the
campaign, so reproduction requires deploying your own endpoints as described
above.

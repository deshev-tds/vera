# VERA - Verification-Enabled Research Agent

A proof-of-concept “Verification-Enabled Research Agent” (VERA) that runs **locally**, with full root permissions in a Linux **Docker sandbox**, can browse the public internet, read/write mounted files, run Linux commands + Python, and uses a **test-time verification loop** to reduce hallucinations and enforce evidence-grounded outputs.

When an LLM is given real I/O (files, network, shell) and real tools, it can exhibit **emergent problem-solving behaviors**. In one experiment, a small 30B coding-optimized model independently converged on a strict numerical error tolerance while searching atomic mass data, installed required libraries at runtime, and produced chemical structure diagrams - without any explicit human guidance.

This repo is explicitly motivated by two complementary research threads:

- **LLM-in-Sandbox**: giving an LLM a real “virtual computer” (terminal + files + internet) can elicit *general* agentic capabilities without additional training, and can reduce long-context token costs by offloading context to files. See [1].
- **Inference-time scaling of verification (DeepVerifier)**: correctness can often be improved by iteratively verifying and repairing outputs using rubric-guided, decomposed checks rather than “one-shot” answers. See [2].

> Note: citations are included as a References section below; the two papers above are the main “science behind” this project.

## Why This Exists

Agentic systems fail in predictable ways:

- **Wrong sources** / low-quality sources, especially on legal/technical claims.
- **Mis-extraction**: wrong number, wrong section, wrong quote.
- **Tool misuse**: a command fails but the agent proceeds anyway.
- **Overconfident synthesis**: conclusions not supported by evidence.
- **Long-horizon drift**: continued synthesis without epistemic progress.

This project exists to make a local agent behave more like an auditable system:

- every tool action is logged,
- claims are forced to carry “evidence hooks” (URLs + snippets, or file paths + commands),
- a verifier loop checks risky claims and provides targeted corrective instructions.

## What You Get

- A Docker sandbox with `/input` (read-only) + `/work` (read-write).
- A minimal tool protocol: model outputs a single-line JSON tool call.
- A DeepVerifier-style verifier loop (decompose -> verify -> judge) with stop-early logic and a configurable retry budget.
- Live “digging” monitoring via a local dashboard:
  - SSE event stream from `trace.jsonl`
  - Prometheus-style `/metrics`
  - Session picker + “New session” + “Start run” UI
- A non-terminal epistemic state model: missing evidence does not cause failure; tasks remain UNRESOLVED until new evidence is produced or search is exhausted.

## Repository Layout

- `run.py` – CLI entrypoint (build, run, dashboard)
- `agent/`
  - `loop.py` – main agent loop + trace logging + verifier integration
  - `tools.py` – Docker sandbox + shell tool (shell-only interface)
  - `verifier.py` – DeepVerifier-style verifier modules in one file
  - `model_client.py` – OpenAI-compatible `/chat/completions` client (+ latency & usage capture)
- `assets/`
  - `docker/Dockerfile` – sandbox image
  - `system_prompt.en.txt` – system prompt for sandbox interaction
- `dashboard/`
  - `server.py` – local dashboard UI + `/events` + `/metrics`

## Architecture

```text
User Task
   |
   v
Agent Model  --(shell JSON)-->  Sandbox (Docker)
   ^                                 |
   |                                 v
Verifier (decompose/check/judge)  trace.jsonl + notes.md
   |                                 |
   +---------- feedback -------------+
                     |
                     v
        Dashboard (/events, /metrics)
```

## Design Decisions (and Why)

### 1) “Linux box” interface with file mounts

We follow the “LLM-in-Sandbox” idea: a computer is a universal substrate (files + shell + internet) and can generalize beyond coding when the model is encouraged to explore and use the environment. See [1].

Implementation:
- host mounts `--work-dir` -> `/work` (rw)
- optionally mount `--input-dir` -> `/input` (ro)

### 2) Runtime tool acquisition (venv-first, plus OS packages if needed)

We want “install at runtime” behavior. We bootstrap `/work/.venv` and put it on `PATH` so `pip install ...` works in an isolated, writable environment under the mounted work directory. In lab mode the container runs privileged, so OS-level installs (e.g., `apt-get`) are also possible.

### 2.1) Lab-mode privilege (maximum freedom)

The sandbox currently runs **privileged** to enable unrestricted experimentation (including OS package installs and low-level network changes). This is intentional for the “emergent behavior” lab setting, but it removes most isolation guardrails. Use only on trusted, local machines.

### 3) Persistent-ish shell ergonomics

LLM-in-Sandbox emphasizes a terminal session where state persists (cwd, environment). See [1].

We simulate the most important parts:
- `cd ...` persists across `shell` calls
- simple `export KEY=VALUE` persists across `shell` calls

(The underlying container exec is still per-call; this is a pragmatic approximation.)

### 4) Verification scaling (decompose -> verify -> judge)

Instead of a single “judge the entire answer” prompt, we decompose verification into a few yes/no checks and verify those with tools (asymmetry of verification). This follows the DeepVerifier direction: verification is more reliable when broken into targeted, source-bound questions. See [2].

The verifier:
- proposes ≤3 checks
- runs a tiny tool-using loop per check to gather evidence
- returns a score (1–4) and ≤3 concrete corrective instructions
- stops early when score ≥ 3; caps verification rounds to avoid diminishing returns

### 5) SCOUT gating (Scope -> Candidates -> Outcomes)

Some tasks are structurally easy to answer overconfidently (especially negative claims like “none / no one / never”). To reduce this failure mode, the verifier applies SCOUT-style gating:

- **Scope**: make the scope explicit (what entities count, time window, success criteria).
- **Candidates**: if the question implies a complete set, enumerate the candidate set from a cited source before concluding.
- **Outcomes**: verify the predicate for the candidates (or use a source that asserts it collectively).

The verifier caps scores (≤2) when any load-bearing check is unknown, when it cannot establish coverage for a negative claim, or when it cannot produce two independent citations (distinct domains).

### 6) Observability-first

Long-horizon agents are hard to debug without visibility. We log a structured event stream (`trace.jsonl`) and expose:
- `/events` (SSE) for live dashboards
- `/metrics` (Prometheus text format) for graphs and alerting

We also log internal verifier activity (decomposition/judge model calls, per-check tool calls, and verifier->agent feedback injection), so you can audit the end-to-end “digging” process.
Additional observability hooks:
- Raw model request/response snapshots are captured as `model_io` events and shown in the dashboard (Model I/O panel).
- Per-session container logs and Docker event stream are written to `/work/container.log` and `/work/container_events.log` and shown in the dashboard (Container Logs panel).

### 7) Web access is “just the shell”

There is no special web-browsing tool. If the agent needs the internet, it must use standard CLI tooling from the sandbox (typically `curl`/`wget`). This makes the environment feel like a real Linux box and keeps all network actions auditable as shell commands in `trace.jsonl`.

## Requirements

- Python 3.11+
- Docker (Docker Desktop / Docker Engine) running locally
- A model server exposing an OpenAI-compatible endpoint:
  - `POST {BASE_URL}/v1/chat/completions` (if you pass `http://127.0.0.1:1234`, this project auto-normalizes to `/v1`)

Python deps:
- see `requirements.txt`

## Quickstart

Install Python deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Build sandbox image:

```bash
python3 run.py build
```

Run the dashboard (in one terminal):

```bash
python3 run.py dashboard --base-dir . --host 127.0.0.1 --port 8844
```

Run a task (in another terminal):

```bash
python3 run.py run \
  --task "Find the official source for X and quote it." \
  --work-dir ./work/example \
  --input-dir ./input \
  --model-base-url http://127.0.0.1:1234
```

Open the dashboard:

```text
http://127.0.0.1:8844/?work_dir=./work/example
```

Artifacts produced in `--work-dir`:
- `notes.md` – human-readable notes
- `evidence.jsonl` – durable tool-output evidence ledger (one JSON per tool call)
- `move_ledger.jsonl` – structured search-move ledger (move typing + outcomes)
- `query_ledger.jsonl` – query mutation ledger (normalized query families + outcomes)
- `trace.jsonl` – structured event log (tools, model calls, verifier decisions)
- `container.log` – sandbox container stdout/stderr (per session)
- `container_events.log` – Docker events for the sandbox container (per session)
- `run.log` – stdout/stderr of the agent process when started from the dashboard
- `run.pid` – PID of the agent process when started from the dashboard
- `session.log` – per-session dashboard control log (start_run/new_session events)

### Dashboard sessions

Each `work_dir` is a “session”. In the dashboard you can:
- open an existing session (from `./work/*`)
- create a new session (auto-creates `./work/ui-run-...`)
- start a run once per session (prompt locks after start)

### First Test Run (Scripts)

Start the “backend/observability stack” (dashboard + Docker sandbox sanity check):

```bash
bash start_stack.sh
```

The script prompts for a step budget:
- `1` = unlimited (`MAX_STEPS=0`)
- `2` = set an integer limit

Stop the dashboard:

```bash
bash stop_stack.sh
```

## Metrics You’ll Likely Watch

- Decision score tracking:
  - `dra_verifier_scores_total{score="1|2|3|4"}`
  - `dra_verifier_last_score`
- Verification cost:
  - `dra_verifier_duration_seconds_sum` / `_count`
  - `dra_verifier_model_tokens_total`
  - `dra_model_tokens_total{scope="agent"}`
- Instruction drift / concreteness proxies:
  - `dra_verifier_instruction_chars_sum`
  - `dra_verifier_instruction_has_url_total`
  - `dra_verifier_instruction_has_path_total`
  - `dra_verifier_instruction_has_cmd_total`
- Diagnosis / paralysis indicators:
  - `dra_verifier_before_tools_total`
  - `dra_model_finish_reason_length_total`
  - `dra_policy_pre_tool_nudge_total`
  - `dra_policy_length_nudge_total`
  - `dra_policy_reminder_total`
  - `dra_policy_choice_total`
  - `dra_policy_choice_matched_total`
  - `dra_policy_stagnation_total`
  - `dra_policy_domain_shift_total`
  - `dra_policy_conclusion_ready_total`
  - `dra_verifier_gradient_total`

## Safety Notes (POC)

- `/input` is mounted read-only; `/work` is writable.
- The container currently runs as **root** (lab-mode / exploration). This enables `apt-get` and broader tool acquisition, but it can also create root-owned files under your mounted `--work-dir`.
- Shell commands are not allowlisted (to preserve exploration), but obvious destructive patterns are blocked (see `agent/config.py`).

This is a research prototype: review deny patterns and add policy/allowlists before using on untrusted inputs.

## Important behavior notes

- Tool execution still requires a parseable JSON tool call, but the parser now accepts either `{"tool":"shell","args":{"cmd":"..."}}` or `{"tool":"shell","command":"..."}`, and can recover JSON from fenced blocks.
- If a model talks about using tools but does not call them, you will see model/verifier events but few `tool` events in `trace.jsonl`.
- A lightweight policy layer nudges the agent before the first tool call and when the model hits `finish_reason="length"` repeatedly; these show up as `policy_*` metrics and events.
- Context is now **deterministic**: each step reassembles the prompt from System + PRIMARY TASK + pinned `notes.md` + a short action tail (no FIFO clipping of the middle).
  - Context assembly (fixed order):
    ```text
    [System Prompt]
         |
    [PRIMARY TASK]
         |
    [PINNED notes.md]
         |
    [Action Tail]
    ```
- Every `N` steps (default `3`), the agent is required to update `notes.md` before it can run further tools (see `NOTES_UPDATE_INTERVAL` in `loop.py`).
- Notes are **append-only**; overwrite/delete attempts are blocked.
  - Notes gate (append-only):
    ```text
    step % N == 0
        |
    must write notes.md
        |
    tool call allowed
    ```
- Output format is strict: **THOUGHT line + single JSON Action line + EVIDENCE_USED + STATUS_UPDATE**. Missing required lines results in a format error and no tool execution.
- Tool outputs are normalized into `evidence.jsonl`; evidence IDs (`ev_0001`…) are appended to `notes.md` and must be cited via `EVIDENCE_USED`.
- Search intuition scaffolding: tool calls are typed into `move_ledger.jsonl` and query families recorded in `query_ledger.jsonl`; repeated query families trigger a query-mutation requirement before retrying.
  - Search pressure (query + move typing):
    ```text
    same query family (xN)
        |
    require mutation
        |
    new query family
    ```
- The verifier runs in a **clean-room** “Auditor” persona and does not reuse the worker’s chat history (only task + notes + evidence).
- Stagnation detector: if `UNRESOLVED` repeats without new evidence for `STAGNATION_LIMIT` turns, the agent is forced to run a tool; repeated failure types prompt escalation (`FAILURE_ESCALATION_LIMIT`).
- Negative-claim protocol (e.g., “has X launched yet?”):
  - Minimum source coverage is enforced before any “no official announcement found” conclusion:
    - ≥2 **official** domains (vendor-owned)
    - ≥1 **independent** domain (non‑vendor)
  - Domain‑shift guard prevents hammering the same domain when minimums aren’t met (`policy_domain_shift`).
  - Once the negative‑claim budget is exhausted and source minimums are satisfied, the run transitions to `UNRESOLVED(reason)` with a concrete “no official announcement found in sources checked” summary (`policy_conclusion_ready`).
  - Negative‑claim constraints are injected as `OPEN CONSTRAINTS` so the model treats them as hard requirements (no explicit denial required).
- To prevent “final deliverables” tool-call loops, the runner stops after repeated finalization-style file writes (see `policy_finalization_stop` in `trace.jsonl`).
- Defaults were relaxed for exploration: `--max-steps` now defaults to `80` and tool timeouts are longer (`MAX_TOOL_SECONDS=900`). You can set unlimited steps with `--max-steps 0` or `MAX_STEPS=0`.
- Model request timeout defaults to `150s` and can be overridden with `MODEL_TIMEOUT` env var.
- Tooling caches are routed into `/work/.cache` (pip/npm/playwright) to make runtime installs more reliable across steps.

## Roadmap (Likely Next)

- Evidence enforcement: reject verifier answers that lack URL/path+command evidence.
- File-based output contract (`/work/output/...` + explicit submit) and stricter formatting.
- Better “persistent session” (true interactive shell or tmux-like session).
- Optional indexing (SQLite FTS5) for large GDPR exports and deterministic excerpt citations.

## References

[1] Daixuan Cheng et al. (2026). *LLM-in-Sandbox Elicits General Agentic Intelligence.* arXiv:2601.16206. Paper page: `https://arxiv.org/abs/2601.16206`

[2] Yuxuan Wan et al. (2026). *Inference-Time Scaling of Verification: Self-Evolving Deep Research Agents via Test-Time Rubric-Guided Verification.* arXiv:2601.15808. Repo: `https://github.com/yxwan123/DeepVerifier`

[3] Shunyu Yao et al. (2022). *ReAct: Synergizing Reasoning and Acting in Language Models.* arXiv:2210.03629. Paper page: `https://arxiv.org/abs/2210.03629`.

[4] Charlie Snell et al. (2024). *Scaling LLM Test-Time Compute Optimally can be More Effective than Scaling Model Parameters.* arXiv:2408.03314. Paper page: `https://arxiv.org/abs/2408.03314`

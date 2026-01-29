# -*- coding: utf-8 -*-

import json
import time
import base64
import threading
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import os
from .model_client import OpenAICompatClient
from .tools import SandboxManager, ToolBelt
from .parse import parse_with_thought, try_parse_tool_call
from .verifier import deep_verify, format_verifier_feedback

def load_system_prompt(profile: str | None = None) -> str:
    base = Path(__file__).resolve().parent.parent / "assets"
    if profile:
        candidate = base / f"system_prompt.{profile}.txt"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    p = base / "system_prompt.en.txt"
    return p.read_text(encoding="utf-8")

TOOL_SPEC = {
    "shell": {"args": {"cmd": "string"}},
}

MAX_MODEL_IO_MESSAGES = 12
MAX_MODEL_IO_CHARS = 4000
MAX_MODEL_IO_RESPONSE_CHARS = 12000
CONTEXT_MAX_CHARS = int(os.getenv("CONTEXT_MAX_CHARS", "20000"))
ACTION_TAIL_MESSAGES = int(os.getenv("ACTION_TAIL_MESSAGES", "10"))
NOTES_UPDATE_INTERVAL = int(os.getenv("NOTES_UPDATE_INTERVAL", "3"))
NEGATIVE_CLAIM_MIN_OFFICIAL = int(os.getenv("NEGATIVE_CLAIM_MIN_OFFICIAL", "2"))
NEGATIVE_CLAIM_MIN_INDEPENDENT = int(os.getenv("NEGATIVE_CLAIM_MIN_INDEPENDENT", "1"))
NEGATIVE_CLAIM_THRESHOLD_PCT = float(os.getenv("NEGATIVE_CLAIM_THRESHOLD_PCT", "0.6"))
NEGATIVE_CLAIM_MAX_STEPS = int(os.getenv("NEGATIVE_CLAIM_MAX_STEPS", "40"))
DOMAIN_SHIFT_LIMIT = int(os.getenv("DOMAIN_SHIFT_LIMIT", "2"))

def _clip_text(text: str, max_chars: int) -> str:
    if not isinstance(text, str):
        return str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...[truncated {len(text) - max_chars} chars]"

def _compact_messages(messages: list[dict], max_messages: int = MAX_MODEL_IO_MESSAGES, max_chars: int = MAX_MODEL_IO_CHARS) -> list[dict]:
    if not isinstance(messages, list):
        return []
    total = len(messages)
    trimmed: list[dict] = []
    if total > max_messages:
        trimmed.append({"role": "system", "content": f"[omitted {total - max_messages} earlier messages]"})
    for m in messages[-max_messages:]:
        if not isinstance(m, dict):
            trimmed.append({"role": "unknown", "content": _clip_text(str(m), max_chars)})
            continue
        cm: dict = {}
        for k, v in m.items():
            if k == "content" and isinstance(v, str):
                cm[k] = _clip_text(v, max_chars)
            else:
                try:
                    json.dumps(v)
                    cm[k] = v
                except Exception:
                    cm[k] = _clip_text(str(v), max_chars)
        trimmed.append(cm)
    return trimmed

def _extract_tool_calls(text: str) -> list[dict]:
    if not isinstance(text, str) or not text.strip():
        return []
    calls: list[dict] = []
    for m in re.finditer(r"\{.*?\}", text, flags=re.S):
        obj = try_parse_tool_call(m.group(0))
        if isinstance(obj, dict) and obj.get("tool"):
            calls.append(obj)
    if calls:
        return calls
    obj = try_parse_tool_call(text)
    if isinstance(obj, dict) and obj.get("tool"):
        return [obj]
    return []

def run_agent(
    task: str,
    input_dir: str | None,
    work_dir: str,
    model_base_url: str,
    model_name: str | None,
    brave_api_key: str | None,
    temperature: float,
    max_steps: int,
    prompt_profile: str | None,
    system_role: str,
) -> str:
    sm = SandboxManager()
    sandbox = sm.start(input_dir=input_dir, work_dir=work_dir, network_enabled=True)

    tb = ToolBelt(sm, sandbox, brave_api_key=brave_api_key)
    client = OpenAICompatClient(base_url=model_base_url, model=model_name)

    system_prompt = load_system_prompt(prompt_profile)
    system_role = (system_role or "system").strip().lower()

    history: list[dict] = []

    work_dir_host = Path(work_dir)
    work_dir_host.mkdir(parents=True, exist_ok=True)
    trace_path = work_dir_host / "trace.jsonl"
    container_log_path = work_dir_host / "container.log"
    container_events_path = work_dir_host / "container_events.log"
    notes_path_host = work_dir_host / "notes.md"

    def trace_event(event: dict) -> None:
        event = dict(event)
        event.setdefault("ts", time.time())
        with trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def runtime_shell(cmd: str) -> dict:
        """
        Runtime-initiated shell command (not model-initiated).
        Still logged for full observability.
        """
        obs = tb.shell(cmd)
        trace_event({"type": "tool", "scope": "runtime", "step": 0, "tool": "shell", "args": {"cmd": cmd}, "obs": obs})
        return obs

    def notes_reset(text: str) -> None:
        b64 = base64.b64encode(text.encode("utf-8", errors="replace")).decode("ascii")
        runtime_shell(
            "python3 - <<'PY'\n"
            "import base64\n"
            "from pathlib import Path\n"
            f"data = base64.b64decode('{b64}').decode('utf-8', errors='replace')\n"
            "p = Path('/work/notes.md')\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "p.write_text(data, encoding='utf-8', errors='replace')\n"
            "print('OK')\n"
            "PY"
        )

    def notes_append(text: str) -> None:
        b64 = base64.b64encode(text.encode("utf-8", errors="replace")).decode("ascii")
        runtime_shell(
            "python3 - <<'PY'\n"
            "import base64\n"
            "from pathlib import Path\n"
            f"data = base64.b64decode('{b64}').decode('utf-8', errors='replace')\n"
            "p = Path('/work/notes.md')\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "with p.open('a', encoding='utf-8', errors='replace') as f:\n"
            "    f.write(data)\n"
            "print('OK')\n"
            "PY"
        )

    def read_notes_content() -> str:
        try:
            return notes_path_host.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    MAX_MODEL_NOTE_CHARS = 6000

    def log_model_output(step: int, resp_text: str, tag: str) -> None:
        if not resp_text:
            return
        snippet = resp_text.strip()
        if len(snippet) > MAX_MODEL_NOTE_CHARS:
            snippet = snippet[:MAX_MODEL_NOTE_CHARS] + "\n... [truncated]"
        notes_append(
            f"\n\n## Step {step} (model_output:{tag})\n{snippet}\n"
        )

    def build_context(
        task: str,
        history: list[dict],
        notes_content: str,
        max_chars: int = CONTEXT_MAX_CHARS,
        constraints: list[str] | None = None,
        blocked: list[str] | None = None,
        unresolved: list[str] | None = None,
        status: str | None = None,
    ) -> list[dict]:
        if system_role == "user":
            msgs: list[dict] = [{"role": "user", "content": system_prompt}]
        else:
            msgs = [{"role": "system", "content": system_prompt}]
        if status:
            msgs.append({"role": "system", "content": f"EPISTEMIC STATE: {status}"})
        msgs.append({"role": "user", "content": f"PRIMARY TASK:\n{task}"})
        if notes_content.strip():
            msgs.append({"role": "user", "content": "CURRENT NOTES (PINNED):\n" + notes_content})
        else:
            msgs.append(
                {
                    "role": "system",
                    "content": "SYSTEM WARNING: notes.md is empty. Initialize /work/notes.md now before proceeding.",
                }
            )
            msgs.append({"role": "user", "content": "CURRENT NOTES (PINNED):\n<empty>"})

        if constraints:
            constraint_text = "\n".join(f"- {c}" for c in constraints if c)
            if constraint_text:
                msgs.append({"role": "user", "content": "OPEN CONSTRAINTS:\n" + constraint_text})
        if unresolved:
            unresolved_text = "\n".join(f"- {u}" for u in unresolved if u)
            if unresolved_text:
                msgs.append({"role": "user", "content": "UNRESOLVED REASONS:\n" + unresolved_text})
        if blocked:
            blocked_text = "\n".join(f"- {b}" for b in blocked if b)
            if blocked_text:
                msgs.append({"role": "user", "content": "BLOCKERS:\n" + blocked_text})

        action_layer = list(history)
        def _total_chars(items: list[dict]) -> int:
            return sum(len(m.get("content", "")) for m in items if isinstance(m, dict))

        assembled = msgs + action_layer
        while _total_chars(assembled) > max_chars and action_layer:
            action_layer.pop(0)
            assembled = msgs + action_layer
        return assembled

    def _notes_write_mode(cmd: str) -> str | None:
        if not cmd or "notes.md" not in cmd:
            return None
        c = cmd.lower()
        # append-only patterns
        if re.search(r">>\s*[^\\n]*notes\.md", c):
            return "append"
        if re.search(r"\btee\b[^\\n]*\s(-a|--append)\b[^\\n]*notes\.md", c):
            return "append"
        if "notes_append" in c:
            return "append"
        # generic overwrite redirection (exclude >>)
        if re.search(r"(?<!>)>\s*[^\\n]*notes\.md", c):
            return "overwrite"
        # overwrite/destructive patterns
        if re.search(r"\bcat\b\s+>.*notes\.md", c):
            return "overwrite"
        if re.search(r"\btee\b[^\\n]*notes\.md", c):
            return "overwrite"
        if re.search(r"\btruncate\b[^\\n]*notes\.md", c):
            return "overwrite"
        if re.search(r"\brm\b[^\\n]*notes\.md", c):
            return "overwrite"
        if re.search(r"\bmv\b[^\\n]*notes\.md", c):
            return "overwrite"
        if re.search(r"\bcp\b[^\\n]*notes\.md", c):
            return "overwrite"
        if "write_text" in c or "write(" in c or "notes_reset" in c:
            return "overwrite"
        # If no explicit write pattern matched, treat as read-only access.
        return None

    def _is_notes_append(cmd: str) -> bool:
        return _notes_write_mode(cmd) == "append"

    @dataclass
    class EpistemicState:
        status: str = "IN_PROGRESS"
        unresolved: list[str] = field(default_factory=list)
        constraints: list[str] = field(default_factory=list)
        blocked: list[str] = field(default_factory=list)

        def add_constraint(self, text: str) -> None:
            if not text:
                return
            if text not in self.constraints:
                self.constraints.append(text)

        def add_unresolved(self, text: str) -> None:
            if not text:
                return
            if text not in self.unresolved:
                self.unresolved.append(text)

        def add_blocked(self, text: str) -> None:
            if not text:
                return
            if text not in self.blocked:
                self.blocked.append(text)

        def set_verified(self) -> None:
            self.status = "VERIFIED"
            self.constraints.clear()
            self.unresolved.clear()
            self.blocked.clear()

    # init notes (runtime writes via shell to keep the 'shell-only' interaction surface honest)
    notes_reset("# Task\n" + task + "\n\n# Log\n")
    runtime_shell(
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "for name in ('evidence.jsonl','move_ledger.jsonl','query_ledger.jsonl'):\n"
        "    p = Path('/work') / name\n"
        "    if not p.exists():\n"
        "        p.write_text('', encoding='utf-8', errors='replace')\n"
        "print('OK')\n"
        "PY"
    )
    trace_event(
        {
            "type": "sandbox",
            "container_id": sandbox.container_id,
            "container_name": sandbox.name,
            "mem_limit": sandbox.mem_limit,
            "nano_cpus": sandbox.nano_cpus,
            "pids_limit": sandbox.pids_limit,
            "privileged": sandbox.privileged,
            "network_mode": sandbox.network_mode,
            "work_dir": str(work_dir_host),
            "container_log": str(container_log_path),
            "container_events_log": str(container_events_path),
        }
    )
    trace_event({"type": "task", "task": task})

    def _stream_container_logs() -> None:
        try:
            c = sm.client.containers.get(sandbox.container_id)
            with container_log_path.open("ab") as f:
                for chunk in c.logs(stream=True, follow=True, stdout=True, stderr=True):
                    if not chunk:
                        continue
                    f.write(chunk)
                    f.flush()
        except Exception as e:
            try:
                with container_events_path.open("a", encoding="utf-8", errors="replace") as f:
                    f.write(json.dumps({"ts": time.time(), "type": "log_stream_error", "error": str(e)}, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _stream_container_events() -> None:
        try:
            for ev in sm.client.events(filters={"container": sandbox.container_id}, decode=True):
                if not ev:
                    continue
                try:
                    with container_events_path.open("a", encoding="utf-8", errors="replace") as f:
                        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                except Exception:
                    continue
                try:
                    trace_event({"type": "container_event", "event": ev})
                except Exception:
                    pass
        except Exception as e:
            try:
                with container_events_path.open("a", encoding="utf-8", errors="replace") as f:
                    f.write(json.dumps({"ts": time.time(), "type": "event_stream_error", "error": str(e)}, ensure_ascii=False) + "\n")
            except Exception:
                pass

    threading.Thread(target=_stream_container_logs, daemon=True).start()
    threading.Thread(target=_stream_container_events, daemon=True).start()

    last = ""
    verifier_rounds = 0
    max_verifier_rounds = 8
    pending_gradient: dict | None = None
    gradient_reminders = 0
    tool_calls_made = 0
    pre_tool_nudges = 0
    length_nudges = 0
    finalization_hits = 0
    stagnation_streak = 0
    last_failure_type: str | None = None
    last_failure_streak = 0
    last_source_class: str | None = None
    source_class_failure_streak = 0
    force_tool_next = False
    force_query_mutation = False
    force_move_change = False
    force_source_shift = False
    force_domain_shift = False
    STAGNATION_LIMIT = int(os.getenv("STAGNATION_LIMIT", "3"))
    FAILURE_ESCALATION_LIMIT = int(os.getenv("FAILURE_ESCALATION_LIMIT", "3"))
    QUERY_MUTATION_BUDGET = int(os.getenv("QUERY_MUTATION_BUDGET", "2"))
    MOVE_REPEAT_LIMIT = int(os.getenv("MOVE_REPEAT_LIMIT", "3"))
    parse_error_hits = 0
    notes_required = False
    epistemic = EpistemicState()
    evidence_path = work_dir_host / "evidence.jsonl"
    move_path = work_dir_host / "move_ledger.jsonl"
    query_path = work_dir_host / "query_ledger.jsonl"
    evidence_counter = 0
    move_counter = 0
    query_counter = 0
    evidence_ids: set[str] = set()
    last_evidence_count = len(evidence_ids)
    last_move_sig: str | None = None
    last_move_type: str | None = None
    last_domain: str | None = None
    last_domain_key: str | None = None
    last_query_family: str | None = None
    move_repeat_streak = 0
    domain_same_streak = 0
    recent_query_families: deque[str] = deque(maxlen=max(QUERY_MUTATION_BUDGET, 1))
    official_domain_hints: set[str] = set()
    official_domains_checked: set[str] = set()
    independent_domains_checked: set[str] = set()
    domain_attempts: dict[str, int] = {}

    def _normalize_domain(domain: str | None) -> str | None:
        if not domain:
            return None
        d = domain.lower()
        if d.startswith("www."):
            d = d[4:]
        return d

    def _is_search_domain(domain: str | None) -> bool:
        if not domain:
            return False
        d = _normalize_domain(domain) or ""
        return any(
            d.endswith(x)
            for x in (
                "google.com",
                "bing.com",
                "duckduckgo.com",
                "search.brave.com",
                "yahoo.com",
            )
        )

    def _task_domain_tokens(task_text: str) -> set[str]:
        if not task_text:
            return set()
        tokens = re.findall(r"[A-Za-z0-9]{3,}", task_text)
        stop = {
            "the", "a", "an", "of", "for", "and", "to", "in", "on", "with", "by",
            "from", "official", "launch", "released", "release", "version", "report",
            "true", "false", "yet", "still", "actually", "already",
        }
        out = set()
        for t in tokens:
            tl = t.lower()
            if tl in stop:
                continue
            out.add(tl)
        return out

    task_domain_tokens = _task_domain_tokens(task)

    def _is_negative_claim_task(task_text: str) -> bool:
        if not task_text:
            return False
        t = task_text.lower()
        return bool(
            re.search(
                r"\b(not|no|never|false|yet|still|actually|really)\b", t
            )
            or re.search(r"\b(has\s+.*\s+launched|released)\b", t)
            or re.search(r"\b(is|are)\s+.*\b(out|launched|released)\b", t)
        )

    negative_claim_task = _is_negative_claim_task(task)

    def _negative_claim_budget(max_steps_val: int) -> int:
        if max_steps_val > 0:
            return max(1, int(max_steps_val * NEGATIVE_CLAIM_THRESHOLD_PCT))
        return max(1, NEGATIVE_CLAIM_MAX_STEPS)

    negative_claim_budget_steps = _negative_claim_budget(max_steps)

    def _is_official_domain(domain: str | None) -> bool:
        if not domain:
            return False
        d = _normalize_domain(domain) or ""
        if d in official_domain_hints:
            return True
        if d.endswith((".gov", ".int", ".eu")):
            return True
        for tok in task_domain_tokens:
            if tok and tok in d:
                return True
        return False

    if negative_claim_task:
        epistemic.add_constraint(
            "Negative-claim task: require ≥2 official domains and ≥1 independent domain before concluding "
            "'no official announcement found in sources checked'. Do not assert non-launch; explicit denial is "
            "optional (only cite it if found)."
        )

    def _extract_urls(text: str) -> list[str]:
        if not text:
            return []
        return re.findall(r"https?://[^\s\"'<>]+", text)

    def _extract_domain(url: str) -> str | None:
        try:
            return _normalize_domain(urlparse(url).netloc)
        except Exception:
            return None

    def _extract_query_from_url(url: str) -> str | None:
        try:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query or "")
            for key in ("q", "query", "search", "s", "text", "keyword", "term"):
                if key in qs and qs[key]:
                    return unquote(str(qs[key][0]))
            path = unquote(parsed.path or "")
            for marker in ("/search/", "/query/", "/name/", "/compound/name/", "/wiki/"):
                if marker in path:
                    tail = path.split(marker, 1)[-1]
                    tail = tail.strip("/")
                    if tail and len(tail) < 120:
                        return tail.replace("_", " ")
        except Exception:
            return None
        return None

    def _normalize_query(q: str) -> str:
        if not q:
            return ""
        q = unquote(q).lower()
        tokens = re.findall(r"[a-z0-9]+", q)
        stop = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "with", "by", "from"}
        tokens = [t for t in tokens if t not in stop]
        return " ".join(tokens)

    def _classify_source(url: str | None, domain: str | None) -> str:
        if not domain:
            return "unknown"
        d = domain.lower()
        if _is_official_domain(d):
            return "official"
        if d.endswith(".gov") or d.endswith(".eu") or d.endswith(".int"):
            return "regulatory"
        if any(x in d for x in ("pubchem", "chemspider", "drugbank", "clinicaltrials", "who.int")):
            return "registry"
        if any(x in d for x in ("ncbi.nlm.nih.gov", "nih.gov", "pubmed", "pmc")):
            return "primary_literature"
        if any(x in d for x in ("arxiv.org", "biorxiv.org", "medrxiv.org", "doi.org")):
            return "primary_literature"
        if any(x in d for x in ("wikipedia.org", "stackexchange.com", "reddit.com")):
            return "commentary"
        if url and re.search(r"\.pdf(\?|$)", url, flags=re.I):
            return "primary_literature"
        return "commentary"

    def _classify_move(domain: str | None, query_family: str | None, source_class: str | None) -> str:
        nonlocal last_domain, last_query_family, last_source_class
        if not domain and not query_family:
            return "non_search"
        if last_domain is None:
            return "initial"
        if domain == last_domain:
            if query_family and query_family == last_query_family:
                return "retry"
            if query_family and query_family != last_query_family:
                return "reformulate"
            return "same_domain"
        if source_class and last_source_class and source_class != last_source_class:
            return "source_shift"
        return "domain_shift"

    def record_evidence(step: int, tool: str, args: dict, obs: dict) -> str | None:
        try:
            nonlocal evidence_counter, last_failure_type, last_failure_streak
            evidence_counter += 1
            ev_id = f"ev_{evidence_counter:04d}"
            cmd = ""
            if tool == "shell" and isinstance(args, dict):
                cmd = str(args.get("cmd") or "")
            output = ""
            if isinstance(obs, dict):
                output = str(obs.get("output") or "")
            urls = _extract_urls(cmd + "\n" + output)
            failure_type = ""
            error_type = str((obs or {}).get("error_type") or "")
            error_msg = str((obs or {}).get("error") or "")
            exit_code = (obs or {}).get("exit_code")
            if error_type.startswith("notes_"):
                failure_type = ""
            elif error_type or error_msg:
                failure_type = error_type or "tool_error"
            elif isinstance(exit_code, int) and exit_code != 0:
                failure_type = "tool_error"

            if tool == "shell" and cmd:
                if re.search(r"\b(403|forbidden|access denied|captcha|cloudflare)\b", output, flags=re.I):
                    failure_type = failure_type or "access_blocked"
                if re.search(r"\b(401|unauthorized)\b", output, flags=re.I):
                    failure_type = failure_type or "auth_required"
                if re.search(r"\b(429|rate limit|too many requests)\b", output, flags=re.I):
                    failure_type = failure_type or "rate_limited"
                if any(k in cmd for k in ("curl", "wget")) and not output.strip():
                    failure_type = failure_type or "empty_response"

            if failure_type:
                if failure_type == last_failure_type:
                    last_failure_streak += 1
                else:
                    last_failure_type = failure_type
                    last_failure_streak = 1
                detail = failure_type
                if cmd:
                    detail = f"{failure_type}: {cmd[:200]}"
                epistemic.add_blocked(detail)
                epistemic.status = "BLOCKED"
            else:
                last_failure_type = None
                last_failure_streak = 0

            ev = {
                "id": ev_id,
                "ts": time.time(),
                "step": step,
                "tool": tool,
                "args": args,
                "obs": {
                    "exit_code": (obs or {}).get("exit_code"),
                    "error_type": (obs or {}).get("error_type"),
                    "error": (obs or {}).get("error"),
                    "output": _clip_text(output, 2000),
                },
                "urls": urls[:20],
                "failure_type": failure_type or None,
            }
            with evidence_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            evidence_ids.add(ev_id)
            return ev_id
        except Exception:
            return None

    def _append_jsonl(path: Path, payload: dict) -> None:
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            return

    def record_move(step: int, tool: str, cmd: str, url: str | None, domain: str | None,
                    query: str | None, query_family: str | None, source_class: str, move_type: str,
                    move_sig: str, failure_type: str | None, outcome: str) -> str:
        nonlocal move_counter
        move_counter += 1
        mv_id = f"mv_{move_counter:04d}"
        payload = {
            "id": mv_id,
            "ts": time.time(),
            "step": step,
            "tool": tool,
            "cmd": cmd,
            "url": url,
            "domain": domain,
            "query": query,
            "query_family": query_family,
            "source_class": source_class,
            "move_type": move_type,
            "move_sig": move_sig,
            "failure_type": failure_type,
            "outcome": outcome,
        }
        _append_jsonl(move_path, payload)
        return mv_id

    def record_query(step: int, url: str | None, domain: str | None, query: str | None, query_family: str | None,
                     source_class: str, move_type: str, outcome: str) -> str:
        nonlocal query_counter
        query_counter += 1
        q_id = f"q_{query_counter:04d}"
        payload = {
            "id": q_id,
            "ts": time.time(),
            "step": step,
            "url": url,
            "domain": domain,
            "query": query,
            "query_family": query_family,
            "source_class": source_class,
            "move_type": move_type,
            "outcome": outcome,
        }
        _append_jsonl(query_path, payload)
        return q_id

    def _extract_status_update(text: str) -> str | None:
        if not text:
            return None
        m = re.search(r"\bSTATUS_UPDATE\s*:\s*(.+)", text, flags=re.I)
        if not m:
            return None
        return m.group(1).strip()

    def _extract_evidence_used(text: str) -> list[str]:
        if not text:
            return []
        m = re.search(r"\bEVIDENCE_USED\s*:\s*(.+)", text, flags=re.I)
        if not m:
            return []
        blob = m.group(1).strip()
        # Try JSON list
        try:
            data = json.loads(blob)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
        # Fallback: comma/space separated
        parts = re.split(r"[,\s]+", blob)
        return [p for p in parts if p]

    def _finalization_intent(text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        markers = [
            "final answer",
            "final output",
            "final deliverable",
            "final deliverables",
            "final report",
            "final summary",
            "all the information i need",
            "complete final",
            "deliverables as requested",
        ]
        return any(m in t for m in markers)

    def _writes_final_like_file(cmd: str) -> bool:
        if not cmd:
            return False
        c = cmd.lower()
        if ">" not in c and "tee" not in c:
            return False
        if "/work" not in c and "cd /work" not in c:
            return False
        keywords = ["final", "deliverable", "answer", "summary", "report", "output", "visual", "stability"]
        return any(k in c for k in keywords)
    try:
        step = 0
        while True:
            step += 1
            if max_steps > 0 and step > max_steps:
                break
            if force_tool_next:
                hint = "STAGNATION DETECTED: You must run a tool now to obtain new evidence."
                if last_failure_type:
                    hint += f" Previous failures: {last_failure_type}. Try a different source/tool."
                if last_failure_streak >= FAILURE_ESCALATION_LIMIT and last_failure_type:
                    hint += " Escalate to a different acquisition path (alternate domain, API, or browser automation)."
                history.append({"role": "user", "content": hint})
            if force_query_mutation:
                history.append(
                    {
                        "role": "user",
                        "content": (
                            "QUERY MUTATION REQUIRED: propose a materially different query before retrying. "
                            "Use different keywords, synonyms, or a different formulation."
                        ),
                    }
                )
            if force_move_change:
                history.append(
                    {
                        "role": "user",
                        "content": (
                            "MOVE CHANGE REQUIRED: change your search move type (reformulate or domain shift). "
                            "Avoid repeating the same move."
                        ),
                    }
                )
            if force_source_shift:
                history.append(
                    {
                        "role": "user",
                        "content": (
                            "SOURCE CLASS SHIFT REQUIRED: switch to a different source class "
                            "(e.g., registry → primary literature → regulatory → commentary)."
                        ),
                    }
                )
            if force_domain_shift:
                history.append(
                    {
                        "role": "user",
                        "content": (
                            "DOMAIN SHIFT REQUIRED: use a different domain than the last attempt. "
                            "For negative-claim tasks, ensure at least "
                            f"{NEGATIVE_CLAIM_MIN_OFFICIAL} official domains and "
                            f"{NEGATIVE_CLAIM_MIN_INDEPENDENT} independent domains are checked."
                        ),
                    }
                )
            if NOTES_UPDATE_INTERVAL > 0 and step > 0 and (step % NOTES_UPDATE_INTERVAL == 0):
                notes_required = True
            notes_content = read_notes_content()
            history_tail = history[-ACTION_TAIL_MESSAGES:]
            context_messages = build_context(
                task,
                history_tail,
                notes_content,
                max_chars=CONTEXT_MAX_CHARS,
                constraints=epistemic.constraints,
                blocked=epistemic.blocked,
                unresolved=epistemic.unresolved,
                status=epistemic.status,
            )
            if notes_required:
                intervention = (
                    f"SYSTEM INTERVENTION: It has been {NOTES_UPDATE_INTERVAL} steps. "
                    "You must update /work/notes.md with your latest findings/failures before proceeding."
                )
                while True:
                    context_messages = build_context(
                        task,
                        history_tail,
                        notes_content,
                        max_chars=CONTEXT_MAX_CHARS,
                        constraints=epistemic.constraints,
                        blocked=epistemic.blocked,
                        unresolved=epistemic.unresolved,
                        status=epistemic.status,
                    )
                    context_messages.append({"role": "user", "content": intervention})
                    total_chars = sum(len(m.get("content", "")) for m in context_messages if isinstance(m, dict))
                    if total_chars <= CONTEXT_MAX_CHARS or not history_tail:
                        break
                    history_tail = history_tail[1:]
            raw = client.chat_raw(context_messages, temperature=temperature, max_tokens=1200)
            resp = raw["choices"][0]["message"]["content"]
            last = resp
            finish_reason = ((raw.get("choices") or [{}])[0].get("finish_reason"))
            trace_event({"type": "assistant", "step": step, "content": resp[:20000]})
            usage = raw.get("usage") or {}
            trace_event(
                {
                    "type": "model_io",
                    "step": step,
                    "request": {
                        "messages_total": len(context_messages),
                        "messages": _compact_messages(context_messages),
                        "temperature": temperature,
                        "max_tokens": 1200,
                        "model": raw.get("model") or model_name,
                        "system_role": system_role,
                    },
                    "response": {
                        "content": _clip_text(resp, MAX_MODEL_IO_RESPONSE_CHARS),
                        "finish_reason": finish_reason,
                        "usage": usage,
                    },
                }
            )
            trace_event(
                {
                    "type": "model",
                    "step": step,
                    "scope": "agent",
                    "latency_s": float(raw.get("_latency_s") or 0.0),
                    "usage": usage,
                    "finish_reason": finish_reason,
                    "n_messages": len(context_messages),
                    "input_chars": sum(len(m.get("content", "")) for m in context_messages if isinstance(m, dict)),
                }
            )

            # Try to extract multiple tool calls first (batching support)
            tool_calls = _extract_tool_calls(resp)
            
            # If no tools found via extraction, fall back to standard parser (handles errors/thoughts)
            parsed = None
            if not tool_calls:
                parsed = parse_with_thought(resp)

            if not tool_calls and parsed and parsed.error:
                if finish_reason == "length":
                    length_nudges += 1
                    trace_event(
                        {
                            "type": "policy_length_nudge",
                            "step": step,
                            "count": length_nudges,
                        }
                    )
                    history.append({"role": "assistant", "content": resp})
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                "Your response was truncated due to length limits. "
                                "Please try again, but output a shorter response or split the content into multiple steps."
                            ),
                        }
                    )
                    log_model_output(step, resp, "length_truncation")
                    continue

                parse_error_hits += 1
                trace_event(
                    {
                        "type": "policy_parse_error",
                        "step": step,
                        "error": parsed.error,
                        "count": parse_error_hits,
                    }
                )
                history.append({"role": "assistant", "content": resp})
                history.append({"role": "user", "content": f"SYSTEM FORMAT ERROR: {parsed.error}"})
                log_model_output(step, resp, "parse_error")
                if parse_error_hits >= 5:
                    return "Stopped due to repeated format errors (missing THOUGHT/ACTION). See /work/notes.md."
                continue

            if not tool_calls and parsed and parsed.tool_name:
                tool_calls = [{"tool": parsed.tool_name, "args": parsed.tool_args or {}}]

            if pending_gradient is not None and not tool_calls:
                gradient_reminders += 1
                trace_event(
                    {
                        "type": "policy_reminder",
                        "step": step,
                        "gradient_reminders": gradient_reminders,
                    }
                )
                if gradient_reminders <= 4:
                    history.append({"role": "assistant", "content": resp})
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                "You have verifier feedback. Use tools to gather missing evidence and make progress now. "
                                "Prefer next_actions when helpful, but you may choose any sensible action."
                            ),
                        }
                    )
                    continue
                if gradient_reminders > 6:
                    pending_gradient = None

            if not tool_calls:
                # If the model produced a final JSON payload, prefer it over raw output.
                answer_text = resp
                if parsed and isinstance(parsed.tool_args, dict) and parsed.tool_args.get("final"):
                    answer_text = str(parsed.tool_args.get("final"))
                log_model_output(step, resp, "no_tool")
                enforce_contract = tool_calls_made >= 3 or _finalization_intent(resp)
                if enforce_contract:
                    status_update = _extract_status_update(resp)
                    evidence_used = _extract_evidence_used(resp)
                    if not status_update:
                        epistemic.status = "UNRESOLVED"
                        epistemic.add_constraint("Missing STATUS_UPDATE")
                    else:
                        if "UNRESOLVED" in status_update.upper():
                            epistemic.status = "UNRESOLVED"
                            epistemic.add_unresolved(status_update)
                        elif "BLOCKED" in status_update.upper():
                            epistemic.status = "BLOCKED"
                            epistemic.add_blocked(status_update)
                        elif "VERIFIED" in status_update.upper() and not epistemic.constraints:
                            epistemic.set_verified()
                    if evidence_used:
                        missing = [e for e in evidence_used if e not in evidence_ids]
                        if missing:
                            epistemic.status = "UNRESOLVED"
                            epistemic.add_constraint(f"Unknown EVIDENCE_USED ids: {', '.join(missing)}")
                    else:
                        epistemic.status = "UNRESOLVED"
                        epistemic.add_constraint("Missing EVIDENCE_USED")
                if negative_claim_task and step >= negative_claim_budget_steps:
                    if (
                        len(official_domains_checked) >= NEGATIVE_CLAIM_MIN_OFFICIAL
                        and len(independent_domains_checked) >= NEGATIVE_CLAIM_MIN_INDEPENDENT
                    ):
                        epistemic.status = "UNRESOLVED"
                        epistemic.add_unresolved("negative_claim_evidence_exhausted")
                # Stagnation detector: unresolved without new evidence/tool activity
                if epistemic.status == "UNRESOLVED":
                    if len(evidence_ids) == last_evidence_count:
                        stagnation_streak += 1
                    else:
                        stagnation_streak = 0
                else:
                    stagnation_streak = 0
                if stagnation_streak >= STAGNATION_LIMIT and not force_tool_next:
                    force_tool_next = True
                    epistemic.add_constraint(
                        f"Stagnation: no new evidence for {stagnation_streak} consecutive turns"
                    )
                    trace_event(
                        {
                            "type": "policy_stagnation",
                            "step": step,
                            "streak": stagnation_streak,
                            "limit": STAGNATION_LIMIT,
                            "failure_type": last_failure_type,
                            "failure_streak": last_failure_streak,
                        }
                    )
                # Early-phase gating: don't let the verifier micromanage before initial exploration.
                if tool_calls_made < 3:
                    pre_tool_nudges += 1
                    trace_event(
                        {
                            "type": "policy_pre_tool_nudge",
                            "step": step,
                            "count": pre_tool_nudges,
                        }
                    )
                    if pre_tool_nudges <= 6:
                        history.append({"role": "assistant", "content": resp})
                        history.append(
                            {
                                "role": "user",
                                "content": (
                                    "You have not used tools yet. Use the shell now to make concrete progress. "
                                    "You can chain commands with && to do multiple steps in one tool call."
                                ),
                            }
                        )
                        continue
                    # Do not run the verifier before initial exploration; it causes analysis paralysis.
                    history.append({"role": "assistant", "content": resp})
                    history.append(
                        {
                            "role": "user",
                            "content": "Stop planning and run a shell command that gathers evidence.",
                        }
                    )
                    continue

                # If the model hits the token cap, nudge it to emit a tool call instead of more thinking.
                if finish_reason == "length":
                    length_nudges += 1
                    trace_event(
                        {
                            "type": "policy_length_nudge",
                            "step": step,
                            "count": length_nudges,
                        }
                    )
                    if length_nudges <= 4:
                        history.append({"role": "assistant", "content": resp})
                        history.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your previous response was truncated. Keep it short and run a shell command now."
                                ),
                            }
                        )
                        continue

                verifier_rounds += 1
                v0 = time.perf_counter()
                decision = deep_verify(
                    task=task,
                    answer=answer_text,
                    notes_snapshot=notes_content,
                    trace_path=str(trace_path),
                    evidence_path=str(evidence_path),
                    client=client,
                    tb=tb,
                    max_tool_steps_per_check=4,
                    trace_cb=trace_event,
                    parent_step=step,
                )
                vdt = time.perf_counter() - v0
                decision.meta["duration_s"] = vdt
                trace_event({"type": "verifier", "step": step, "decision": decision.to_dict()})

                if decision.score < 3:
                    epistemic.status = "UNRESOLVED"
                    for ins in (decision.instructions or [])[:5]:
                        epistemic.add_constraint(ins)
                    meta = decision.meta or {}
                    cap_reasons = meta.get("cap_reasons") if isinstance(meta, dict) else None
                    if isinstance(cap_reasons, list):
                        for r in cap_reasons:
                            epistemic.add_unresolved(str(r))

                if decision.score >= 3:
                    epistemic.set_verified()
                    return resp
                if verifier_rounds >= max_verifier_rounds:
                    epistemic.status = "UNRESOLVED"
                    epistemic.add_unresolved("verification_budget_exhausted")
                    return (
                        "Verifier could not confirm correctness within the verification budget. "
                        "See /work/trace.jsonl and /work/notes.md.\n\n"
                        + resp
                    )

                feedback = format_verifier_feedback(decision)
                trace_event(
                    {
                        "type": "verifier_to_agent",
                        "step": step,
                        "score": decision.score,
                        "content": feedback[:20000],
                    }
                )
                grad = (decision.meta or {}).get("gradient") if isinstance(decision.meta, dict) else None
                if isinstance(grad, dict) and grad:
                    pending_gradient = grad
                    gradient_reminders = 0
                    trace_event({"type": "verifier_gradient", "step": step, "gradient": grad})
                    if decision.score < 3:
                        epistemic.status = "UNRESOLVED"
                        missing = grad.get("missing") or []
                        wrong = grad.get("wrong") or []
                        if isinstance(missing, list):
                            for m in missing:
                                epistemic.add_constraint(str(m))
                        if isinstance(wrong, list):
                            for w in wrong:
                                if isinstance(w, dict):
                                    epistemic.add_constraint(str(w.get("item") or w.get("why") or ""))
                                else:
                                    epistemic.add_constraint(str(w))
                history.append({"role": "assistant", "content": resp})
                history.append({"role": "user", "content": feedback})
                trace_event(
                    {
                        "type": "agent_from_verifier",
                        "step": step,
                        "n_messages": len(history),
                    }
                )
                continue

            # reset parse error streak on any tool call
            parse_error_hits = 0

            # Execute all tool calls found in the response sequentially
            history.append({"role": "assistant", "content": resp})

            for call in tool_calls:
                tool = call.get("tool")
                args = call.get("args", {}) or {}
                
                if pending_gradient is not None:
                    suggested = []
                    na = pending_gradient.get("next_actions") or []
                    if isinstance(na, list):
                        for item in na:
                            if not isinstance(item, dict):
                                continue
                            tools = item.get("suggested_tools") or []
                            if isinstance(tools, list):
                                for t in tools:
                                    if isinstance(t, dict):
                                        suggested.append(t)
                    matched = False
                    for t in suggested:
                        if t.get("tool") == tool and isinstance(args, dict):
                            cmd = args.get("cmd")
                            if cmd and cmd == t.get("cmd"):
                                matched = True
                                break
                    trace_event(
                        {
                            "type": "policy_choice",
                            "step": step,
                            "matched": matched,
                            "tool": tool,
                            "args": args,
                        }
                    )
                    pending_gradient = None
                    gradient_reminders = 0

                cmd = (args or {}).get("cmd", "") if isinstance(args, dict) else ""
                notes_mode = _notes_write_mode(cmd) if tool == "shell" else None
                urls = _extract_urls(cmd) if tool == "shell" else []
                primary_url = urls[0] if urls else None
                domain = _extract_domain(primary_url) if primary_url else None
                query = _extract_query_from_url(primary_url) if primary_url else None
                query_family = _normalize_query(query) if query else None
                source_class = _classify_source(primary_url, domain)
                move_type = _classify_move(domain, query_family, source_class)
                move_sig = f"{move_type}:{domain or '-'}:{query_family or '-'}"

                if tool == "shell" and notes_mode == "overwrite":
                    obs = {
                        "error": "Action Blocked: Overwriting notes.md is not allowed. Use append (>> or tee -a).",
                        "error_type": "notes_overwrite_blocked",
                    }
                    trace_event(
                        {
                            "type": "policy_notes_guard",
                            "step": step,
                            "required": notes_required,
                            "allowed": False,
                            "mode": notes_mode,
                            "tool": tool,
                            "args": args,
                        }
                    )
                    history.append(
                        {
                            "role": "user",
                            "content": "OBSERVATION:\n" + json.dumps({"tool": tool, "obs": obs}, ensure_ascii=False)[:12000],
                        }
                    )
                    trace_event({"type": "tool", "step": step, "tool": tool, "args": args, "obs": obs})
                    ev_id = record_evidence(step, tool, args, obs)
                    tool_calls_made += 1
                    notes_append(
                        f"\n\n## Step {step}\n"
                        f"TOOL: {tool}\n"
                        f"ARGS: {json.dumps(args, ensure_ascii=False)}\n"
                        f"OBS: {json.dumps(obs, ensure_ascii=False)[:2000]}\n"
                        f"EVIDENCE_ID: {ev_id}\n"
                    )
                    force_tool_next = False
                    stagnation_streak = 0
                    last_evidence_count = len(evidence_ids)
                    continue

                if notes_required and tool == "shell" and notes_mode != "append":
                    obs = {
                        "error": "Action Blocked: You must update notes.md first (append-only).",
                        "error_type": "notes_update_required",
                    }
                    trace_event(
                        {
                            "type": "policy_notes_gate",
                            "step": step,
                            "required": True,
                            "allowed": False,
                            "mode": notes_mode,
                            "tool": tool,
                            "args": args,
                        }
                    )
                    history.append(
                        {
                            "role": "user",
                            "content": "OBSERVATION:\n" + json.dumps({"tool": tool, "obs": obs}, ensure_ascii=False)[:12000],
                        }
                    )
                    trace_event({"type": "tool", "step": step, "tool": tool, "args": args, "obs": obs})
                    ev_id = record_evidence(step, tool, args, obs)
                    tool_calls_made += 1
                    notes_append(
                        f"\n\n## Step {step}\n"
                        f"TOOL: {tool}\n"
                        f"ARGS: {json.dumps(args, ensure_ascii=False)}\n"
                        f"OBS: {json.dumps(obs, ensure_ascii=False)[:2000]}\n"
                        f"EVIDENCE_ID: {ev_id}\n"
                    )
                    force_tool_next = False
                    stagnation_streak = 0
                    last_evidence_count = len(evidence_ids)
                    continue

                if tool == "shell" and query_family and query_family in recent_query_families and len(recent_query_families) < QUERY_MUTATION_BUDGET:
                    obs = {
                        "error": (
                            "Action Blocked: query mutation required before retrying. "
                            f"Need {QUERY_MUTATION_BUDGET} distinct query families; seen {len(recent_query_families)}."
                        ),
                        "error_type": "query_mutation_required",
                    }
                    trace_event(
                        {
                            "type": "policy_query_mutation",
                            "step": step,
                            "required": QUERY_MUTATION_BUDGET,
                            "seen": len(recent_query_families),
                            "query_family": query_family,
                            "domain": domain,
                        }
                    )
                    history.append(
                        {
                            "role": "user",
                            "content": "OBSERVATION:\n" + json.dumps({"tool": tool, "obs": obs}, ensure_ascii=False)[:12000],
                        }
                    )
                    trace_event({"type": "tool", "step": step, "tool": tool, "args": args, "obs": obs})
                    ev_id = record_evidence(step, tool, args, obs)
                    record_move(
                        step,
                        tool,
                        cmd,
                        primary_url,
                        domain,
                        query,
                        query_family,
                        source_class,
                        move_type,
                        move_sig,
                        last_failure_type,
                        "blocked",
                    )
                    record_query(
                        step,
                        primary_url,
                        domain,
                        query,
                        query_family,
                        source_class,
                        move_type,
                        "blocked",
                    )
                    tool_calls_made += 1
                    notes_append(
                        f"\n\n## Step {step}\n"
                        f"TOOL: {tool}\n"
                        f"ARGS: {json.dumps(args, ensure_ascii=False)}\n"
                        f"OBS: {json.dumps(obs, ensure_ascii=False)[:2000]}\n"
                        f"EVIDENCE_ID: {ev_id}\n"
                    )
                    force_query_mutation = True
                    force_tool_next = False
                    stagnation_streak = 0
                    last_evidence_count = len(evidence_ids)
                    continue

                domain_key = domain
                if negative_claim_task and domain_key and last_domain_key == domain_key and domain_same_streak >= DOMAIN_SHIFT_LIMIT:
                    if (
                        len(official_domains_checked) < NEGATIVE_CLAIM_MIN_OFFICIAL
                        or len(independent_domains_checked) < NEGATIVE_CLAIM_MIN_INDEPENDENT
                    ):
                        force_domain_shift = True

                if negative_claim_task and force_domain_shift and domain_key and last_domain_key == domain_key:
                    obs = {
                        "error": (
                            "Action Blocked: domain shift required for negative-claim tasks. "
                            "Use a different domain to meet official/independent source minimums."
                        ),
                        "error_type": "domain_shift_required",
                    }
                    trace_event(
                        {
                            "type": "policy_domain_shift",
                            "step": step,
                            "domain": domain_key,
                            "official_checked": len(official_domains_checked),
                            "independent_checked": len(independent_domains_checked),
                            "limit": DOMAIN_SHIFT_LIMIT,
                        }
                    )
                    history.append(
                        {
                            "role": "user",
                            "content": "OBSERVATION:\n" + json.dumps({"tool": tool, "obs": obs}, ensure_ascii=False)[:12000],
                        }
                    )
                    trace_event({"type": "tool", "step": step, "tool": tool, "args": args, "obs": obs})
                    ev_id = record_evidence(step, tool, args, obs)
                    record_move(
                        step,
                        tool,
                        cmd,
                        primary_url,
                        domain,
                        query,
                        query_family,
                        source_class,
                        move_type,
                        move_sig,
                        last_failure_type,
                        "blocked",
                    )
                    if query_family:
                        record_query(
                            step,
                            primary_url,
                            domain,
                            query,
                            query_family,
                            source_class,
                            move_type,
                            "blocked",
                        )
                    tool_calls_made += 1
                    notes_append(
                        f"\n\n## Step {step}\n"
                        f"TOOL: {tool}\n"
                        f"ARGS: {json.dumps(args, ensure_ascii=False)}\n"
                        f"OBS: {json.dumps(obs, ensure_ascii=False)[:2000]}\n"
                        f"EVIDENCE_ID: {ev_id}\n"
                    )
                    force_tool_next = False
                    stagnation_streak = 0
                    last_evidence_count = len(evidence_ids)
                    continue

                try:
                    if tool != "shell":
                        obs = {
                            "error": f"Unknown tool (shell-only mode): {tool}",
                            "hint": "Use the shell tool only. If you need the internet, do it from the shell.",
                        }
                    else:
                        obs = tb.shell(args.get("cmd", ""))
                        if obs.get("exit_code") != 0:
                            c = args.get("cmd", "")
                            if "echo" in c and "'" in c and "command not found" in obs.get("output", ""):
                                obs["hint"] = "Check your quotes. You might have an unescaped single quote inside a single-quoted string."
                except Exception as e:
                    obs = {"error": str(e), "error_type": e.__class__.__name__}

                history.append({"role": "user", "content": "OBSERVATION:\n" + json.dumps({"tool": tool, "obs": obs}, ensure_ascii=False)[:12000]})
                trace_event({"type": "tool", "step": step, "tool": tool, "args": args, "obs": obs})
                ev_id = record_evidence(step, tool, args, obs)
                tool_calls_made += 1
                failure_type = last_failure_type
                outcome = "ok" if not failure_type else "failed"
                record_move(
                    step,
                    tool,
                    cmd,
                    primary_url,
                    domain,
                    query,
                    query_family,
                    source_class,
                    move_type,
                    move_sig,
                    failure_type,
                    outcome,
                )
                if query_family:
                    record_query(
                        step,
                        primary_url,
                        domain,
                        query,
                        query_family,
                        source_class,
                        move_type,
                        outcome,
                    )
                if notes_required and tool == "shell" and notes_mode == "append":
                    notes_required = False

                # log
                notes_append(
                    f"\n\n## Step {step}\n"
                    f"TOOL: {tool}\n"
                    f"ARGS: {json.dumps(args, ensure_ascii=False)}\n"
                    f"OBS: {json.dumps(obs, ensure_ascii=False)[:2000]}\n"
                    f"EVIDENCE_ID: {ev_id}\n"
                )
                if domain_key:
                    domain_attempts[domain_key] = domain_attempts.get(domain_key, 0) + 1
                    if negative_claim_task and not _is_search_domain(domain_key) and not official_domain_hints:
                        official_domain_hints.add(domain_key)
                    is_official = _is_official_domain(domain_key) or source_class in {"official", "regulatory", "registry"}
                    if is_official:
                        official_domains_checked.add(domain_key)
                    elif not _is_search_domain(domain_key):
                        independent_domains_checked.add(domain_key)
                    if last_domain_key == domain_key:
                        domain_same_streak += 1
                    else:
                        domain_same_streak = 0
                        force_domain_shift = False
                    last_domain_key = domain_key
                if query_family and query_family not in recent_query_families:
                    recent_query_families.append(query_family)
                    force_query_mutation = False
                if move_sig == last_move_sig and move_type == last_move_type:
                    move_repeat_streak += 1
                else:
                    move_repeat_streak = 0
                    force_move_change = False
                if domain:
                    last_domain = domain
                if query_family:
                    last_query_family = query_family
                last_move_sig = move_sig
                last_move_type = move_type
                if source_class:
                    if last_source_class == source_class and failure_type:
                        source_class_failure_streak += 1
                    else:
                        source_class_failure_streak = 0
                    if source_class_failure_streak >= FAILURE_ESCALATION_LIMIT and failure_type:
                        force_source_shift = True
                        epistemic.add_constraint(
                            f"Source class stalled: {source_class} failed {source_class_failure_streak} times"
                        )
                    if source_class != last_source_class:
                        force_source_shift = False
                    last_source_class = source_class
                if epistemic.status == "UNRESOLVED" and move_repeat_streak >= MOVE_REPEAT_LIMIT:
                    force_move_change = True
                    epistemic.add_constraint(
                        f"Move stagnation: repeated {move_type} {move_repeat_streak} times"
                    )
                force_tool_next = False
                stagnation_streak = 0
                last_evidence_count = len(evidence_ids)

                # Stop tool-call loops when the agent is repeatedly "finalizing" deliverables.
                if tool == "shell" and _finalization_intent(resp) and _writes_final_like_file(args.get("cmd", "")):
                    finalization_hits += 1
                    trace_event(
                        {
                            "type": "policy_finalization_stop",
                            "step": step,
                            "hits": finalization_hits,
                            "cmd": (args.get("cmd") or "")[:500],
                        }
                    )
                    if finalization_hits >= 3:
                        return "Final deliverables appear to be written under /work. Stopping to prevent a tool loop."

        if max_steps > 0 and step >= max_steps:
            if epistemic.status != "VERIFIED":
                return (
                    "UNRESOLVED: Evidence requirements not satisfied within the step budget.\n"
                    f"Status: {epistemic.status}\n"
                    f"Constraints: {epistemic.constraints}\n"
                    f"Blocked: {epistemic.blocked}\n"
                    f"Unresolved: {epistemic.unresolved}\n"
                    "See /work/notes.md and /work/evidence.jsonl."
                )
            return "Did not reach a verifiable final answer within the step budget. See /work/notes.md."
        if epistemic.status != "VERIFIED":
            return (
                "UNRESOLVED: Evidence requirements not satisfied.\n"
                f"Status: {epistemic.status}\n"
                f"Constraints: {epistemic.constraints}\n"
                f"Blocked: {epistemic.blocked}\n"
                f"Unresolved: {epistemic.unresolved}\n"
                "See /work/notes.md and /work/evidence.jsonl."
            )
        return "Stopped without a verifiable final answer. See /work/notes.md."
    finally:
        sm.stop(sandbox)

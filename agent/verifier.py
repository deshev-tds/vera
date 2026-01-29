# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import re
from urllib.parse import urlparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .parse import try_parse_tool_call


AUDITOR_SYSTEM_PROMPT = """You are an adversarial auditor. You do not care about the agent's thought process.
You only care about safety, evidence quality, and whether claims are supported.

###PROTOCOL: STRICT DATA PROVENANCE (THE "MAGIC CONSTANT" RULE)
You are the guardian of TRUTH against HALLUCINATION. The Agent is prone to inventing complex data (parametric memory hallucination).

**1. Definition of "Magic Constants" (High-Entropy Data):**
Any specific, complex string used as a critical input parameter that is NOT common knowledge. Examples:
- Chemical identifiers (SMILES, InChI, complex IUPAC names).
- Hash sums (SHA, MD5), UUIDs, or specific API Keys/IDs.
- Mathematical constants that are non-standard (e.g., specific coefficients).
- IP addresses or specific non-public URLs.
- Any other domain-specific complex identifier.

**2. The Verification Algorithm:**
When the Agent proposes an Action using a "Magic Constant":
- **SEARCH** the `OBS` (Observation/Tool Output) history.
- **IF** the constant appears verbatim in a previous successful tool output:
  -> **PASS** (Grounding confirmed).
- **IF** the constant appears ONLY in `THOUGHT`, `NOTES`, or the Action itself (and was never returned by a tool):
  -> **FAIL / REJECT**.

**3. Rejection Template:**
If you trigger this rule, your explanation MUST follow this pattern:
"SECURITY ALERT: You are using a complex parameter ('{VALUE}') derived solely from internal memory/training data. This violates Epistemic Security. You must first retrieve/verify this value against the environment (e.g., using grep, curl, or a script) before using it in a critical step."

**EXCEPTION:** Do not flag simple filenames (e.g., "script.py"), standard paths ("/work"), or common sense values. Focus on High-Entropy technical data.

Be strict, skeptical, and concise. Do not assume missing facts."""


def build_verifier_context(
    task: str,
    action_proposal: Dict[str, Any],
    notes_snapshot: str,
    evidence_snapshot: str | None = None,
) -> List[Dict[str, str]]:
    notes_snapshot = notes_snapshot or "(notes empty)"
    payload = (
        f"TASK:\n{task}\n\n"
        f"PROPOSED_ACTION:\n{json.dumps(action_proposal, ensure_ascii=False)}\n\n"
        f"NOTES:\n{notes_snapshot}"
    )
    if evidence_snapshot:
        payload += f"\n\nEVIDENCE_LOG:\n{evidence_snapshot}"
    return [{"role": "system", "content": AUDITOR_SYSTEM_PROMPT}, {"role": "user", "content": payload}]


FAILURE_TAXONOMY_V0 = [
    "Source acquisition failure (wrong/low-quality/outdated source)",
    "Evidence extraction failure (misquote/wrong number/wrong section)",
    "Reasoning/aggregation failure (jump to conclusion/mix jurisdictions/entities)",
    "Tool execution failure (ignored errors/wrong path/partial extraction)",
    "Safety/ops failure (destructive commands/data leakage)",
]


@dataclass
class VerifierDecision:
    score: int
    explanation: str
    instructions: List[str] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "explanation": self.explanation,
            "instructions": self.instructions,
            "checks": self.checks,
            "meta": self.meta,
        }


def _is_negative_answer(answer: str) -> bool:
    a = (answer or "").strip().lower()
    first = a.splitlines()[0] if a else ""
    return bool(re.match(r"^(none|no one|nobody|no member|no members)\b", first))


def _evidence_urls(checks_with_results: List[Dict[str, Any]]) -> List[str]:
    urls: List[str] = []
    for item in checks_with_results:
        res = (item.get("result") or {})
        ev = res.get("evidence") or []
        if not isinstance(ev, list):
            continue
        for e in ev:
            if not isinstance(e, dict):
                continue
            if e.get("type") != "url":
                continue
            ref = str(e.get("ref") or "").strip()
            if ref.startswith("http://") or ref.startswith("https://"):
                urls.append(ref)
    return urls


def _distinct_domains(urls: List[str]) -> List[str]:
    domains: List[str] = []
    seen = set()
    for u in urls:
        try:
            netloc = urlparse(u).netloc.lower()
        except Exception:
            continue
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if not netloc or netloc in seen:
            continue
        seen.add(netloc)
        domains.append(netloc)
    return domains


def _check_unknown(res: Dict[str, Any]) -> bool:
    ans = str(res.get("answer") or "").strip().lower()
    if ans in ("unknown", "", "n/a"):
        return True
    tool_log = res.get("tool_log") or []
    if isinstance(tool_log, list):
        for item in tool_log:
            if not isinstance(item, dict):
                continue
            obs = item.get("obs") or {}
            if not isinstance(obs, dict):
                continue
            if obs.get("error") or obs.get("soft_error"):
                return True
            if item.get("tool") == "shell" and obs.get("exit_code") not in (None, 0, "0"):
                return True
    ev = res.get("evidence") or []
    if not isinstance(ev, list) or len(ev) == 0:
        return True
    return False


def _needs_coverage(task: str) -> bool:
    """
    Heuristic: tasks that imply a complete candidate set should require a coverage check
    (Scope → Candidates → Outcomes), especially for "which/who" and "any/ever/never" style queries.
    """
    t = (task or "").lower()
    patterns = [
        r"\bwhich\b.*\bmember\b",
        r"\bwhich\b.*\bperson\b",
        r"\bwho\b.*\bmember\b",
        r"\bwho\b",
        r"\bany\b.*\bmember\b",
        r"\bever\b",
        r"\bnever\b",
        r"\bno one\b",
        r"\bnobody\b",
        r"\bnone\b",
        r"\bearliest\b",
        r"\blatest\b",
        r"\bonly\b",
        r"\ball\b.*\bmembers\b",
        r"\btouring member\b",
        r"\bgig\b",
        r"\bsession musician\b",
    ]
    return any(re.search(p, t) for p in patterns)


def _tool_signature(tool: str, args: Dict[str, Any], obs: Dict[str, Any]) -> Tuple[str, str, str, str]:
    key = ""
    if tool == "shell":
        key = str(args.get("cmd") or "")
    else:
        key = json.dumps(args, ensure_ascii=False, sort_keys=True)
    status = str(obs.get("exit_code") if tool == "shell" else (obs.get("status") or ""))
    soft = str(obs.get("soft_error") or "")
    err = (str(obs.get("error_type") or "") + ":" + str(obs.get("error") or "")).strip(":")
    return tool, key, status, soft + "|" + err


def _extract_first_json(text: str) -> Optional[Any]:
    """
    Best-effort extraction of a single JSON object/array from model output.
    We scan lines and try json.loads on candidates.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if (line.startswith("{") and line.endswith("}")) or (line.startswith("[") and line.endswith("]")):
            try:
                return json.loads(line)
            except Exception:
                continue
    # Fallback: try to find a JSON block in the text.
    m = re.search(r"(\{.*\}|\[.*\])", text, flags=re.S)
    if not m:
        return None
    blob = m.group(1).strip()
    try:
        return json.loads(blob)
    except Exception:
        return None


def _summarize_trace(trace_path: str, max_chars: int = 6000, notes_max_chars: int = 2000) -> str:
    """
    Summarize tool trajectory for decomposition/judging without replaying the whole trace.
    """
    p = Path(trace_path)
    if not p.exists():
        return "(no trace available)"

    lines: List[str] = []
    try:
        raw_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return "(failed to read trace)"

    for raw in raw_lines[-200:]:
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        t = ev.get("type")
        if t == "tool":
            step = ev.get("step")
            tool = ev.get("tool")
            args = ev.get("args", {})
            obs = ev.get("obs", {})
            if tool == "shell":
                lines.append(
                    f"Step {step}: shell cmd={args.get('cmd','')!r} exit={obs.get('exit_code')}"
                )
            else:
                lines.append(f"Step {step}: {tool} args={args}")
        elif t == "assistant":
            # only keep very small hints to avoid leaking full answer back
            step = ev.get("step")
            snippet = (ev.get("content") or "").strip().replace("\n", " ")
            if snippet:
                lines.append(f"Step {step}: assistant said ~{snippet[:140]!r}")

    trace_out = "\n".join(lines)

    notes_out = ""
    notes_path = p.with_name("notes.md")
    if notes_path.exists():
        try:
            notes_lines = notes_path.read_text(encoding="utf-8", errors="replace").splitlines()
            notes_tail = "\n".join(notes_lines[-120:]).strip()
            if notes_tail:
                notes_out = notes_tail[-notes_max_chars:]
        except Exception:
            notes_out = ""

    if notes_out:
        combined = trace_out + "\n\nNOTES_TAIL:\n" + notes_out
    else:
        combined = trace_out
    return combined[-max_chars:]


def _summarize_evidence_log(evidence_path: str, max_chars: int = 3000, max_lines: int = 40) -> str:
    p = Path(evidence_path)
    if not p.exists():
        return ""
    try:
        raw_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    if not raw_lines:
        return ""
    tail = raw_lines[-max_lines:]
    summaries: List[str] = []
    for raw in tail:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        step = obj.get("step")
        tool = obj.get("tool")
        failure = obj.get("failure_type")
        urls = obj.get("urls") or []
        obs = obj.get("obs") or {}
        exit_code = obs.get("exit_code")
        snippet = {
            "step": step,
            "tool": tool,
            "exit_code": exit_code,
            "failure_type": failure,
            "urls": urls[:3],
        }
        summaries.append(json.dumps(snippet, ensure_ascii=False))
    return ("\n".join(summaries))[:max_chars]


def _parse_judge_score(text: str) -> int:
    m = re.search(r"\bScore\s*:\s*([1-4])\b", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\b([1-4])\b", text.strip())
    if m:
        return int(m.group(1))
    return 2


def _parse_instructions(text: str, limit: int = 3) -> List[str]:
    instr: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(Instruction\s*\d+:\s*)(.*)$", line, flags=re.I)
        if m:
            instr.append(m.group(2).strip())
        elif line.startswith("- "):
            instr.append(line[2:].strip())
        if len(instr) >= limit:
            break
    return [i for i in instr if i][:limit]

def _sanitize_no_formula(value: Any) -> Any:
    """
    Ensure the word 'formula' does not appear in JSON keys or values.
    Replaces 'formula' (case-insensitive) with 'composition'.
    """
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            nk = re.sub(r"formula", "composition", str(k), flags=re.I)
            out[nk] = _sanitize_no_formula(v)
        return out
    if isinstance(value, list):
        return [_sanitize_no_formula(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"formula", "composition", value, flags=re.I)
    return value


def _decompose_checks(client, task: str, answer: str, trace_summary: str) -> List[Dict[str, Any]]:
    sys = (
        "You are a decomposition module for a Deep Research Agent verifier.\n"
        "Your job: propose the fewest high-leverage verification checks.\n"
        "Use the failure taxonomy to look for risk.\n"
        "Do NOT re-solve the task.\n"
        "Return EXACTLY ONE LINE: a JSON array of up to 3 check objects.\n"
        "Each check must be answerable via tools and must be yes/no.\n"
        "Schema: [{\"kind\":\"coverage|support\",\"claim\":\"...\",\"question\":\"...\",\"source_hint\":\"(url or file path or search query)\",\"taxonomy\":\"...\"}]\n"
        f"Failure taxonomy: {FAILURE_TAXONOMY_V0}\n"
    )
    usr = (
        f"TASK:\n{task}\n\n"
        f"UNVERIFIED_ANSWER:\n{answer}\n\n"
        f"TRAJECTORY_SUMMARY:\n{trace_summary}\n\n"
        "Generate checks now."
    )
    raw = client.chat_raw(
        [{"role": "system", "content": sys}, {"role": "user", "content": usr}],
        temperature=0.0,
        max_tokens=600,
    )
    resp = raw["choices"][0]["message"]["content"]
    data = _extract_first_json(resp)
    if not isinstance(data, list):
        return []
    checks: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        claim = str(item.get("claim", "")).strip()
        q = str(item.get("question", "")).strip()
        src = str(item.get("source_hint", "")).strip()
        tax = str(item.get("taxonomy", "")).strip()
        if not claim or not q:
            continue
        if kind not in ("coverage", "support"):
            kind = "support"
        checks.append({"kind": kind, "claim": claim, "question": q, "source_hint": src, "taxonomy": tax})
        if len(checks) >= 3:
            break
    return checks


def _run_verification_mini_agent(
    *,
    client,
    tb,
    check: Dict[str, Any],
    max_steps: int,
    trace_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    parent_step: Optional[int] = None,
    check_idx: int = 0,
) -> Dict[str, Any]:
    """
    A small tool-using loop dedicated to verifying ONE check.
    Returns a structured result with evidence hooks.
    """
    sys = (
        "You are a verification agent.\n"
        "You must answer the question using tools, and provide evidence hooks.\n"
        "Rules:\n"
        "- Prefer primary sources; avoid random blogs when possible.\n"
        "- If a tool fails, acknowledge it and try an alternative.\n"
        "- Do NOT re-solve the whole task. Only answer the check.\n"
        "Tooling: there is only ONE tool: a shell command runner.\n"
        "If you need the internet, do it from the shell.\n"
        "Tool-call format: output EXACTLY ONE single-line JSON object with fields: tool, args.\n"
        "When done, output EXACTLY ONE JSON line:\n"
        "{\"answer\":\"yes|no|unknown\",\"evidence\":[{\"type\":\"url|file|cmd\",\"ref\":\"...\",\"snippet\":\"...\"}],\"notes\":\"...\"}\n"
    )
    usr = (
        f"CLAIM: {check.get('claim','')}\n"
        f"QUESTION (yes/no): {check.get('question','')}\n"
        f"SOURCE_HINT: {check.get('source_hint','')}\n"
    )
    messages = [{"role": "system", "content": sys}, {"role": "user", "content": usr}]
    tool_log: List[Dict[str, Any]] = []
    model_stats = {"calls": 0, "latency_s": 0.0, "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
    seen_signatures: Dict[Tuple[str, str, str, str], int] = {}

    for _ in range(max_steps):
        raw = client.chat_raw(messages, temperature=0.0, max_tokens=800)
        resp = raw["choices"][0]["message"]["content"]
        if trace_cb:
            trace_cb(
                {
                    "type": "model",
                    "scope": "verifier_check",
                    "parent_step": parent_step,
                    "check_idx": check_idx,
                    "latency_s": float(raw.get("_latency_s") or 0.0),
                    "usage": raw.get("usage") or {},
                }
            )
            trace_cb(
                {
                    "type": "assistant",
                    "scope": "verifier_check",
                    "parent_step": parent_step,
                    "check_idx": check_idx,
                    "content": resp[:20000],
                }
            )
        model_stats["calls"] += 1
        model_stats["latency_s"] += float(raw.get("_latency_s") or 0.0)
        usage = raw.get("usage") or {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = usage.get(k)
            if isinstance(v, int):
                model_stats["usage"][k] += v
        call = try_parse_tool_call(resp)
        if not call:
            data = _extract_first_json(resp)
            if isinstance(data, dict) and "answer" in data:
                data.setdefault("tool_log", tool_log[-10:])
                data.setdefault("model_stats", model_stats)
                return data
            return {
                "answer": "unknown",
                "evidence": [],
                "notes": "Verifier returned unstructured output.",
                "raw": resp[:2000],
                "tool_log": tool_log[-10:],
                "model_stats": model_stats,
            }

        tool = call.get("tool")
        args = call.get("args", {}) or {}
        if tool not in {"shell"}:
            obs = {
                "error": f"Tool not allowed in verifier (shell-only mode): {tool}",
                "hint": "Use the shell tool only. If you need the internet, do it from the shell.",
            }
        else:
            cmd = str(args.get("cmd") or "").strip()
            try:
                obs = tb.shell(cmd)
            except Exception as e:
                obs = {"error": str(e), "error_type": e.__class__.__name__}

        sig = _tool_signature(tool or "", args, obs if isinstance(obs, dict) else {})
        seen_signatures[sig] = seen_signatures.get(sig, 0) + 1
        if seen_signatures[sig] >= 3 and (isinstance(obs, dict) and (obs.get("error") or obs.get("soft_error"))):
            # Early stop: repeated failure signature.
            final = {
                "answer": "unknown",
                "evidence": [],
                "notes": "Stopped verification early due to repeated identical failures (loop-killer).",
                "tool_log": tool_log[-10:] + [{"tool": tool, "args": args, "obs": obs}],
                "model_stats": model_stats,
                "loop_killer": {"signature": sig, "count": seen_signatures[sig]},
            }
            return final

        if trace_cb:
            trace_cb(
                {
                    "type": "tool",
                    "scope": "verifier",
                    "parent_step": parent_step,
                    "check_idx": check_idx,
                    "tool": tool,
                    "args": args,
                    "obs": obs,
                }
            )

        tool_log.append({"tool": tool, "args": args, "obs": obs})
        messages.append({"role": "assistant", "content": resp})
        messages.append(
            {
                "role": "user",
                "content": "OBSERVATION:\n"
                + json.dumps({"tool": tool, "obs": obs}, ensure_ascii=False)[:12000],
            }
        )

    return {
        "answer": "unknown",
        "evidence": [],
        "notes": "Verifier hit step limit.",
        "tool_log": tool_log[-10:],
        "model_stats": model_stats,
    }


def _judge(
    client,
    task: str,
    answer: str,
    notes_snapshot: str,
    evidence_snapshot: str,
    checks_with_results: List[Dict[str, Any]],
    trace_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    parent_step: Optional[int] = None,
) -> VerifierDecision:
    sys = (
        AUDITOR_SYSTEM_PROMPT
        + "\n"
        "You are a judge module for a Deep Research Agent verifier.\n"
        "You receive: task, unverified answer, notes snapshot, and results of targeted verification checks.\n"
        "Score 1-4: 1=entirely incorrect, 2=mostly incorrect, 3=mostly correct, 4=entirely correct.\n"
        "Return a single-line JSON object called a 'gradient' with this minimal schema:\n"
        "{\n"
        "  \"score\": 1,\n"
        "  \"explanation\": \"...\",\n"
        "  \"missing\": [\"...\"],\n"
        "  \"wrong\": [{\"item\":\"...\",\"why\":\"...\"}],\n"
        "  \"next_actions\": [\n"
        "     {\"goal\":\"...\",\"suggested_tools\":[{\"tool\":\"shell\",\"cmd\":\"...\"}],\"success_criteria\":\"...\"}\n"
        "  ],\n"
        "  \"stop_when\": [\"...\"],\n"
        "  \"tool_waste\": [\"...\"],\n"
        "  \"preferred_source\": [\"...\"]\n"
        "}\n"
        "Important: do NOT use the word 'formula' anywhere in the JSON keys or values.\n"
        "Do NOT add extra text outside the JSON.\n"
    )
    base_ctx = build_verifier_context(task, {"answer": answer}, notes_snapshot, evidence_snapshot)
    base_user = base_ctx[1]["content"] if len(base_ctx) > 1 else ""
    usr = (
        base_user
        + "\n\nUNVERIFIED_ANSWER:\n"
        + answer
        + "\n\nCHECK_RESULTS:\n"
        + json.dumps(checks_with_results, ensure_ascii=False)[:12000]
        + "\n"
    )
    raw = client.chat_raw(
        [{"role": "system", "content": sys}, {"role": "user", "content": usr}],
        temperature=0.0,
        max_tokens=700,
    )
    resp = raw["choices"][0]["message"]["content"]
    if trace_cb:
        trace_cb(
            {
                "type": "model",
                "scope": "verifier_judge",
                "parent_step": parent_step,
                "latency_s": float(raw.get("_latency_s") or 0.0),
                "usage": raw.get("usage") or {},
            }
        )
        trace_cb(
            {
                "type": "assistant",
                "scope": "verifier_judge",
                "parent_step": parent_step,
                "content": resp[:20000],
            }
        )
    data = _extract_first_json(resp)
    gradient: Dict[str, Any] = {}
    score = _parse_judge_score(resp)
    explanation = ""
    instructions: List[str] = []

    if isinstance(data, dict):
        gradient = _sanitize_no_formula(data)
        if isinstance(gradient.get("score"), int):
            score = int(gradient["score"])
        explanation = str(gradient.get("explanation") or "").strip()
        if score <= 2:
            na = gradient.get("next_actions") or []
            if isinstance(na, list):
                for item in na[:3]:
                    if not isinstance(item, dict):
                        continue
                    goal = str(item.get("goal") or "").strip()
                    success = str(item.get("success_criteria") or "").strip()
                    if goal or success:
                        instructions.append(f"{goal} | success: {success}".strip(" |"))
        instructions = [i for i in instructions if i][:3]
    else:
        m = re.search(r"Explanation\s*:\s*(.+)", resp)
        if m:
            explanation = m.group(1).strip()
        else:
            explanation = resp.strip().splitlines()[0][:500] if resp.strip() else ""
        instructions = _parse_instructions(resp, limit=3) if score <= 2 else []

    return VerifierDecision(
        score=score,
        explanation=explanation,
        instructions=instructions,
        checks=checks_with_results,
        meta={"gradient": gradient} if gradient else {},
    )


def deep_verify(
    *,
    task: str,
    answer: str,
    notes_snapshot: str,
    trace_path: str,
    evidence_path: str | None = None,
    client,
    tb,
    max_tool_steps_per_check: int = 4,
    trace_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    parent_step: Optional[int] = None,
) -> VerifierDecision:
    """
    DeepVerifier-style test-time verification:
    - Decompose into ≤3 yes/no checks (asymmetry of verification)
    - Verify each check with a small tool-using loop
    - Judge score 1-4 and produce ≤3 corrective instructions
    """
    trace_summary = _summarize_trace(trace_path)
    evidence_summary = _summarize_evidence_log(evidence_path, max_chars=3000) if evidence_path else ""
    action_proposal = {"answer": answer}
    # Decompose
    decomp_sys = (
        AUDITOR_SYSTEM_PROMPT
        + "\n"
        "You are a decomposition module for a Deep Research Agent verifier.\n"
        "Your job: propose the fewest high-leverage verification checks.\n"
        "Use the failure taxonomy to look for risk.\n"
        "Do NOT re-solve the task.\n"
        "Return EXACTLY ONE LINE: a JSON array of up to 3 check objects.\n"
        "Each check must be answerable via tools and must be yes/no.\n"
        "Schema: [{\"kind\":\"coverage|support\",\"claim\":\"...\",\"question\":\"...\",\"source_hint\":\"(url or file path or search query)\",\"taxonomy\":\"...\"}]\n"
        f"Failure taxonomy: {FAILURE_TAXONOMY_V0}\n"
    )
    base_ctx = build_verifier_context(task, action_proposal, notes_snapshot, evidence_summary)
    base_user = base_ctx[1]["content"] if len(base_ctx) > 1 else ""
    raw = client.chat_raw(
        [
            {"role": "system", "content": decomp_sys},
            {"role": "user", "content": base_user + "\n\nGenerate checks now."},
        ],
        temperature=0.0,
        max_tokens=600,
    )
    decomp_resp = raw["choices"][0]["message"]["content"]
    if trace_cb:
        trace_cb(
            {
                "type": "model",
                "scope": "verifier_decompose",
                "parent_step": parent_step,
                "latency_s": float(raw.get("_latency_s") or 0.0),
                "usage": raw.get("usage") or {},
            }
        )
        trace_cb(
            {
                "type": "assistant",
                "scope": "verifier_decompose",
                "parent_step": parent_step,
                "content": decomp_resp[:20000],
            }
        )
    data = _extract_first_json(decomp_resp)
    checks: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip().lower()
            claim = str(item.get("claim", "")).strip()
            q = str(item.get("question", "")).strip()
            src = str(item.get("source_hint", "")).strip()
            tax = str(item.get("taxonomy", "")).strip()
            if not claim or not q:
                continue
            if kind not in ("coverage", "support"):
                kind = "support"
            checks.append({"kind": kind, "claim": claim, "question": q, "source_hint": src, "taxonomy": tax})
            if len(checks) >= 3:
                break

    negative = _is_negative_answer(answer)
    need_coverage = negative or _needs_coverage(task)
    if need_coverage and not any(c.get("kind") == "coverage" for c in checks):
        checks.insert(
            0,
            {
                "kind": "coverage",
                "claim": "The task requires reasoning over a complete candidate set under a stated scope/time window.",
                "question": "Does the source explicitly enumerate the complete candidate set under the relevant scope/time window for the task (so a 'none' or selection claim is justified)?",
                "source_hint": "authoritative complete list of candidates for the entity in the task",
                "taxonomy": "Problem understanding / decomposition failure",
            },
        )
        checks = checks[:3]

    checks_with_results: List[Dict[str, Any]] = []
    verifier_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    verifier_model_latency_s = 0.0
    verifier_model_calls = 0
    verifier_tool_calls = 0
    verifier_tool_errors = 0
    for idx, check in enumerate(checks[:3], 1):
        result = _run_verification_mini_agent(
            client=client,
            tb=tb,
            check=check,
            max_steps=max_tool_steps_per_check,
            trace_cb=trace_cb,
            parent_step=parent_step,
            check_idx=idx,
        )
        ms = result.get("model_stats") or {}
        if isinstance(ms, dict):
            verifier_model_calls += int(ms.get("calls") or 0)
            verifier_model_latency_s += float(ms.get("latency_s") or 0.0)
            u = ms.get("usage") or {}
            if isinstance(u, dict):
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    v = u.get(k)
                    if isinstance(v, int):
                        verifier_usage[k] += v
        tl = result.get("tool_log") or []
        if isinstance(tl, list):
            for item in tl:
                if not isinstance(item, dict):
                    continue
                verifier_tool_calls += 1
                obs = item.get("obs") or {}
                if isinstance(obs, dict) and obs.get("error"):
                    verifier_tool_errors += 1
                exit_code = obs.get("exit_code")
                if isinstance(exit_code, int) and exit_code != 0:
                    verifier_tool_errors += 1
        checks_with_results.append({"check": check, "result": result})

    decision = _judge(
        client,
        task,
        answer,
        notes_snapshot,
        evidence_summary,
        checks_with_results,
        trace_cb=trace_cb,
        parent_step=parent_step,
    )
    decision.explanation = decision.explanation or "No explanation."

    # Instruction drift / concreteness proxies (simple, auditable metrics)
    instr_text = "\n".join(decision.instructions)
    decision.meta.update(
        {
            "n_checks": len(checks_with_results),
            "verifier_model_calls": verifier_model_calls,
            "verifier_model_latency_s": verifier_model_latency_s,
            "verifier_usage": verifier_usage,
            "verifier_tool_calls": verifier_tool_calls,
            "verifier_tool_errors": verifier_tool_errors,
            "instruction_count": len(decision.instructions),
            "instruction_chars": len(instr_text),
            "instruction_has_url": bool(re.search(r"https?://", instr_text)),
            "instruction_has_path": ("/input/" in instr_text) or ("/work/" in instr_text),
            "instruction_has_cmd": bool(re.search(r"\b(rg|grep|curl|python3|pip|jq)\b", instr_text)),
            "negative_claim": negative,
            "needs_coverage": need_coverage,
        }
    )

    # SCOUT gating (Scope→Candidates→Outcomes): prevent overconfident "none"/negative answers without coverage + citations.
    unknown_checks = 0
    coverage_ok: Optional[bool] = None
    for item in checks_with_results:
        chk = item.get("check") or {}
        res = item.get("result") or {}
        if not isinstance(chk, dict) or not isinstance(res, dict):
            continue
        if _check_unknown(res):
            unknown_checks += 1
        if chk.get("kind") == "coverage":
            coverage_ok = (str(res.get("answer") or "").strip().lower() == "yes") and not _check_unknown(res)

    urls = _evidence_urls(checks_with_results)
    domains = _distinct_domains(urls)

    cap_reasons: List[str] = []
    score_before = decision.score
    if unknown_checks > 0:
        cap_reasons.append("unknown_checks_present")
    if len(domains) < 2:
        cap_reasons.append("insufficient_independent_citations")
    if need_coverage and not coverage_ok:
        cap_reasons.append("missing_coverage_proof")

    decision.meta.update(
        {
            "unknown_checks": unknown_checks,
            "evidence_url_count": len(urls),
            "distinct_domains": domains,
            "distinct_domain_count": len(domains),
            "coverage_ok": coverage_ok,
        }
    )

    if cap_reasons:
        decision.meta["score_before_cap"] = score_before
        decision.meta["score_capped"] = True
        decision.meta["cap_reasons"] = cap_reasons
        decision.score = min(decision.score, 2)
        if not decision.instructions:
            decision.instructions = []
        if "insufficient_independent_citations" in cap_reasons:
            decision.instructions.append(
                "Add at least two independent citations from different domains that directly support the key claim."
            )
        if "missing_coverage_proof" in cap_reasons:
            decision.instructions.append(
                "State the scope (what counts as a candidate) and cite a source that enumerates the complete candidate set under that scope; then verify the predicate for all candidates."
            )
        if "unknown_checks_present" in cap_reasons:
            decision.instructions.append(
                "Resolve unknown checks by retrying with alternative sources/tools; do not claim high confidence while a load-bearing check is unknown."
            )
        decision.instructions = decision.instructions[:3]
        decision.explanation = decision.explanation + " [SCOUT gating applied: score capped due to " + ", ".join(cap_reasons) + "]"

    return decision


def format_verifier_feedback(decision: VerifierDecision) -> str:
    gradient = (decision.meta or {}).get("gradient") if isinstance(decision.meta, dict) else None
    if isinstance(gradient, dict) and gradient:
        payload = _sanitize_no_formula(gradient)
        return (
            "VERIFIER_GRADIENT_JSON:\n"
            + json.dumps(payload, ensure_ascii=False)
            + "\n"
            "Use this as coaching. Make progress with tools now. Prefer next_actions when helpful, but they are not mandatory."
        )

    parts = [
        f"VERIFICATION SCORE: {decision.score}/4",
        f"EXPLANATION: {decision.explanation}",
    ]
    if decision.instructions:
        parts.append("INSTRUCTIONS (follow strictly; max 3):")
        for i, ins in enumerate(decision.instructions, 1):
            parts.append(f"{i}. {ins}")
    else:
        parts.append("INSTRUCTIONS: (none)")

    parts.append("CHECK RESULTS (evidence hooks):")
    parts.append(json.dumps(decision.checks, ensure_ascii=False)[:8000])
    parts.append(
        "Now revise the answer. Add concrete evidence hooks (URLs with short quotes, or /input|/work paths + commands). "
        "Call tools if needed."
    )
    return "\n".join(parts)

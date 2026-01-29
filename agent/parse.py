# -*- coding: utf-8 -*-

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class AgentOutput:
    thought: str
    tool_name: str | None
    tool_args: Dict[str, Any] | None
    error: str | None


def _json_loads_lenient(blob: str) -> Optional[Any]:
    try:
        return json.loads(blob)
    except Exception:
        pass
    # Escape raw newlines inside quoted strings
    out = []
    in_str = False
    esc = False
    for ch in blob:
        if in_str:
            if esc:
                esc = False
                out.append(ch)
                continue
            if ch == "\\":
                esc = True
                out.append(ch)
                continue
            if ch == "\"":
                in_str = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            out.append(ch)
        else:
            if ch == "\"":
                in_str = True
            out.append(ch)
    try:
        return json.loads("".join(out))
    except Exception:
        return None


def _extract_json_block(text: str) -> Optional[Any]:
    if not isinstance(text, str):
        return None
    # Fenced blocks first
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, flags=re.S):
        blob = m.group(1).strip()
        val = _json_loads_lenient(blob)
        if val is not None:
            return val
        continue
    # Line-based
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if (line.startswith("{") and line.endswith("}")) or (line.startswith("[") and line.endswith("]")):
            val = _json_loads_lenient(line)
            if val is not None:
                return val
            continue
    # Fallback: first JSON-ish block
    m = re.search(r"(\{.*\}|\[.*\])", text, flags=re.S)
    if not m:
        return None
    blob = m.group(1).strip()
    return _json_loads_lenient(blob)


def parse_with_thought(text: str) -> AgentOutput:
    if not isinstance(text, str) or not text.strip():
        return AgentOutput("", None, None, "Missing THOUGHT block. You must plan before acting.")

    m = re.search(r"\bTHOUGHT:\s*", text)
    if not m:
        # Fallback: accept a direct tool/action JSON without THOUGHT
        fallback = try_parse_tool_call(text)
        if fallback and isinstance(fallback, dict) and fallback.get("tool"):
            return AgentOutput(thought="", tool_name=fallback.get("tool"), tool_args=fallback.get("args"), error=None)
        json_obj = _extract_json_block(text)
        if json_obj is not None:
            normalized = try_parse_tool_call(json.dumps(json_obj))
            if normalized and isinstance(normalized, dict) and normalized.get("tool"):
                return AgentOutput(thought="", tool_name=normalized.get("tool"), tool_args=normalized.get("args"), error=None)
        # No THOUGHT and no JSON action: treat as a final/freeform response
        return AgentOutput(thought="", tool_name=None, tool_args=None, error=None)
    thought_start = m.end()
    remainder = text[thought_start:]
    action_match = re.search(r"\bACTION:\s*", text)
    if action_match:
        action_text = text[action_match.end():]
        json_obj = _extract_json_block(action_text)
    else:
        json_obj = _extract_json_block(remainder)
        if json_obj is None:
            json_obj = _extract_json_block(text)
    if json_obj is None:
        return AgentOutput("", None, None, "Invalid or missing JSON Action.")

    thought = remainder.strip()
    # Trim thought at first JSON block if present
    brace_idx = thought.find("{")
    bracket_idx = thought.find("[")
    cut_idx = min([i for i in (brace_idx, bracket_idx) if i >= 0], default=-1)
    if cut_idx >= 0:
        thought = thought[:cut_idx].strip()

    tool_name = None
    tool_args: Dict[str, Any] | None = None
    if not isinstance(json_obj, dict):
        return AgentOutput(thought, None, None, "Invalid or missing JSON Action.")

    tool_name = json_obj.get("tool")
    if isinstance(tool_name, str):
        tool_name = tool_name.strip()

    # Allow direct command-only payloads
    if tool_name is None and isinstance(json_obj.get("command"), str):
        tool_name = "shell"
        tool_args = {"cmd": json_obj.get("command")}
    elif tool_name is None:
        tool_args = json_obj
    else:
        args = json_obj.get("args")
        if isinstance(args, str):
            tool_args = {"cmd": args}
        elif isinstance(args, dict):
            if "cmd" not in args and "command" in args:
                args = dict(args)
                args["cmd"] = args.get("command")
            tool_args = args
        else:
            tool_args = {}

        if tool_name and tool_name.lower() == "shell" and not tool_args:
            cmd = json_obj.get("cmd") or json_obj.get("command")
            if isinstance(cmd, str) and cmd.strip():
                tool_args = {"cmd": cmd}

    return AgentOutput(thought=thought, tool_name=tool_name, tool_args=tool_args, error=None)

def try_parse_tool_call(text: str) -> Optional[Dict[str, Any]]:
    """
    Tool call format (simple, UX-friendly):
    The model outputs a single line JSON object like:
    {"tool":"shell","args":{"cmd":"rg -n 'foo' /input"}}

    We scan lines and try json.loads on each.
    First valid object with 'tool' is accepted.
    """
    def _strip_ws_outside_strings(s: str) -> str:
        out = []
        in_str = False
        esc = False
        for ch in s:
            if in_str:
                out.append(ch)
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == "\"":
                    in_str = False
            else:
                if ch == "\"":
                    in_str = True
                    out.append(ch)
                elif ch in " \t\r\n":
                    continue
                else:
                    out.append(ch)
        return "".join(out)

    def _strip_keys(obj: Any) -> Any:
        if isinstance(obj, dict):
            new = {}
            for k, v in obj.items():
                if isinstance(k, str):
                    nk = re.sub(r"\s+", "", k)
                else:
                    nk = k
                new[nk] = _strip_keys(v)
            return new
        if isinstance(obj, list):
            return [_strip_keys(x) for x in obj]
        return obj

    def _despace_url(s: str) -> str:
        return re.sub(r"\s+", "", s or "")

    def _despace_path(s: str) -> str:
        return re.sub(r"\s+", "", s or "")

    def _quote_if_needed(s: str) -> str:
        if not isinstance(s, str):
            return ""
        if not s:
            return "''"
        if re.search(r"\s", s):
            return "'" + s.replace("'", "'\"'\"'") + "'"
        return s

    def _normalize_command_str(s: str) -> str:
        if not isinstance(s, str):
            return ""
        s = s.replace("\t", " ").replace("\n", " ")
        # collapse whitespace
        s = re.sub(r"\s+", " ", s).strip()
        # fix tokenized flags: "- la" -> "-la"
        s = re.sub(r"(?:(?<=^)|(?<=\s))-\s+([A-Za-z])", r"-\1", s)
        # re-join tokenized path chunks like "/ work /" -> "/work/"
        tokens = s.split(" ")
        out = []
        current = None
        separators = {"|", "&&", ";", "||"}
        for tok in tokens:
            if tok in separators:
                if current is not None:
                    out.append(current)
                    current = None
                out.append(tok)
                continue
            if current is not None:
                current += tok
                continue
            if tok == "/" or tok.startswith("/") or tok.endswith("/"):
                current = tok
                continue
            if "/" in tok and not tok.startswith("http"):
                out.append(tok)
                continue
            out.append(tok)
        if current is not None:
            out.append(current)
        s = " ".join(out)
        # collapse whitespace again
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _reconstruct_curl_cmd(cmdline: str) -> str:
        tokens = cmdline.split()
        if not tokens:
            return cmdline
        # join tokens between url and next flag
        def is_flag(tok: str) -> bool:
            return tok.startswith("-") and len(tok) > 1

        try:
            url_idx = next(i for i, t in enumerate(tokens) if t.startswith("http"))
        except StopIteration:
            url_idx = -1
        if url_idx >= 0:
            j = url_idx + 1
            while j < len(tokens) and not is_flag(tokens[j]):
                j += 1
            url = "".join(tokens[url_idx:j])
            tokens = tokens[:url_idx] + [url] + tokens[j:]

        # join tokens after -o until next flag
        try:
            o_idx = next(i for i, t in enumerate(tokens) if t in ("-o", "--output"))
        except StopIteration:
            o_idx = -1
        if o_idx >= 0 and o_idx + 1 < len(tokens):
            j = o_idx + 1
            while j < len(tokens) and not is_flag(tokens[j]):
                j += 1
            path = "".join(tokens[o_idx + 1:j])
            tokens = tokens[: o_idx + 1] + [path] + tokens[j:]
        return " ".join(tokens)

    def normalize(obj: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(obj, dict):
            return None
        obj = _strip_keys(obj)

        # simple action schema: {"action":"run","command":"..."}
        action = obj.get("action")
        if isinstance(action, str) and action.strip().lower() in ("run", "shell"):
            cmd = obj.get("command") or obj.get("cmd")
            if isinstance(cmd, str) and cmd.strip():
                return {"tool": "shell", "args": {"cmd": cmd}}

        # file-write action: {"action":"write_file"|"write","path":"...","content":"..."}
        if isinstance(action, str) and action.strip().lower() in ("write_file", "writefile", "write"):
            path = obj.get("path")
            content = obj.get("content")
            if isinstance(path, str) and path.strip() and isinstance(content, str):
                p = _despace_path(path)
                # notes.md must be append-only
                if p.endswith("notes.md"):
                    cmd = f"cat >> {p} << 'EOF'\n{content}\nEOF"
                else:
                    cmd = f"cat > {p} << 'EOF'\n{content}\nEOF"
                return {"tool": "shell", "args": {"cmd": cmd}}

        # Canonical format: {"tool": "...", "args": {...}}
        tool = None
        if "tool" in obj:
            tool = obj.get("tool")
            args = obj.get("args")
            if isinstance(tool, str) and isinstance(args, dict):
                # Some smaller models mistakenly set tool="args" (confusing the field name for the value).
                # Treat that as a shell invocation when a command is present.
                if tool.strip().lower() == "args" and ("cmd" in args or "command" in args):
                    args = dict(args)
                    if "cmd" not in args and "command" in args:
                        args["cmd"] = args.pop("command")
                    return {"tool": "shell", "args": args}
                return {"tool": tool, "args": args}
        # Common mistake: {"tool":"shell","command":"..."}
        if isinstance(tool, str) and tool.strip().lower() == "shell":
            cmd = obj.get("command") or obj.get("cmd")
            if isinstance(cmd, str) and cmd.strip():
                return {"tool": "shell", "args": {"cmd": cmd}}

        # iquest-style: top-level tool_name + command_line
        if "tool_name" in obj or "command_line" in obj:
            tool_name = re.sub(r"\s+", "", str(obj.get("tool_name") or "")).strip().lower()
            cmdline = obj.get("command_line") or obj.get("command") or obj.get("cmd")
            if isinstance(cmdline, str) and cmdline.strip():
                cmdline = _normalize_command_str(cmdline)
                if tool_name in ("curl", "wget"):
                    cmdline = _reconstruct_curl_cmd(cmdline)
                return {"tool": "shell", "args": {"cmd": cmdline}}
            param = obj.get("parameter") or obj.get("parameters")
            if tool_name and isinstance(param, str) and param.strip():
                param = _normalize_command_str(param)
                return {"tool": "shell", "args": {"cmd": f"{tool_name} {param}"}}

        # iquest-style: {"command": {"tool": "curl", "parameters": {"url": "..."}}}
        if "command" in obj and isinstance(obj.get("command"), dict):
            cmd_obj = _strip_keys(obj.get("command") or {})
            if isinstance(cmd_obj, dict):
                tool_name = re.sub(r"\s+", "", str(cmd_obj.get("tool") or cmd_obj.get("name") or "")).strip().lower()
                params = _strip_keys(cmd_obj.get("parameters") or cmd_obj.get("args") or {})
                if tool_name in ("sh", "bash", "shell") and isinstance(params, dict):
                    cmdline = params.get("command") or params.get("cmd")
                    if isinstance(cmdline, str) and cmdline.strip():
                        cmdline = _normalize_command_str(cmdline)
                        return {"tool": "shell", "args": {"cmd": cmdline}}
                if tool_name in ("curl", "wget") and isinstance(params, dict):
                    cmdline_direct = params.get("command") or params.get("cmd")
                    if isinstance(cmdline_direct, str) and cmdline_direct.strip():
                        cmdline_direct = _normalize_command_str(cmdline_direct)
                        cmdline_direct = _reconstruct_curl_cmd(cmdline_direct)
                        return {"tool": "shell", "args": {"cmd": cmdline_direct}}
                if tool_name in ("curl", "wget") and isinstance(params, dict):
                    url = params.get("url") or params.get("href") or params.get("link")
                    if isinstance(url, str) and url.strip():
                        url = _despace_url(url)
                        out_path = params.get("output") or params.get("out")
                        if isinstance(out_path, str) and out_path.strip():
                            out_path = _despace_path(out_path)
                            return {"tool": "shell", "args": {"cmd": f"{tool_name} -sL {_quote_if_needed(url)} -o {_quote_if_needed(out_path)}"}}
                        return {"tool": "shell", "args": {"cmd": f"{tool_name} -sL {_quote_if_needed(url)}"}}
                if isinstance(params, dict):
                    cmdline = params.get("command") or params.get("cmd")
                    if isinstance(cmdline, str) and cmdline.strip():
                        cmdline = _normalize_command_str(cmdline)
                        return {"tool": "shell", "args": {"cmd": cmdline}}
                    file_path = params.get("file_path") or params.get("filepath") or params.get("path") or params.get("file")
                    if isinstance(file_path, str) and file_path.strip():
                        file_path = _despace_path(file_path)
                        return {"tool": "shell", "args": {"cmd": f"{tool_name} {_quote_if_needed(file_path)}"}}

        # iquest-style: {"commands": [{"tool": "...", "parameters": {...}}]}
        if "commands" in obj and isinstance(obj.get("commands"), list):
            cmds = obj.get("commands") or []
            for c in cmds:
                if not isinstance(c, dict):
                    continue
                c = _strip_keys(c)
                tool_name = re.sub(r"\s+", "", str(c.get("tool") or c.get("name") or "")).strip().lower()
                params = _strip_keys(c.get("parameters") or c.get("args") or {})
                # Some models use singular "parameter" or embed a direct command string.
                param = c.get("parameter")
                cmdline_direct = c.get("command") or c.get("cmd")
                if tool_name in ("sh", "bash", "shell") and isinstance(params, dict):
                    cmdline = params.get("command") or params.get("cmd")
                    if isinstance(cmdline, str) and cmdline.strip():
                        cmdline = _normalize_command_str(cmdline)
                        return {"tool": "shell", "args": {"cmd": cmdline}}
                if tool_name in ("sh", "bash", "shell") and isinstance(cmdline_direct, str) and cmdline_direct.strip():
                    cmdline_direct = _normalize_command_str(cmdline_direct)
                    return {"tool": "shell", "args": {"cmd": cmdline_direct}}
                if tool_name in ("curl", "wget") and isinstance(params, dict):
                    url = params.get("url") or params.get("href") or params.get("link")
                    if isinstance(url, str) and url.strip():
                        url = _despace_url(url)
                        out_path = params.get("output") or params.get("out")
                        if isinstance(out_path, str) and out_path.strip():
                            out_path = _despace_path(out_path)
                            return {"tool": "shell", "args": {"cmd": f"{tool_name} -sL {_quote_if_needed(url)} -o {_quote_if_needed(out_path)}"}}
                        return {"tool": "shell", "args": {"cmd": f"{tool_name} -sL {_quote_if_needed(url)}"}}
                if tool_name in ("curl", "wget") and isinstance(param, str) and param.strip():
                    url = _despace_url(param)
                    return {"tool": "shell", "args": {"cmd": f"{tool_name} -sL {_quote_if_needed(url)}"}}
                if tool_name in ("which", "ls", "cat", "head", "tail", "grep", "rg", "sed", "awk", "jq", "python", "python3"):
                    if isinstance(cmdline_direct, str) and cmdline_direct.strip():
                        cmdline_direct = _normalize_command_str(cmdline_direct)
                        return {"tool": "shell", "args": {"cmd": cmdline_direct}}
                    if isinstance(param, str) and param.strip():
                        param = _normalize_command_str(param)
                        return {"tool": "shell", "args": {"cmd": f"{tool_name} {param}"}}
                if isinstance(params, dict):
                    cmdline = params.get("command") or params.get("cmd")
                    if isinstance(cmdline, str) and cmdline.strip():
                        cmdline = _normalize_command_str(cmdline)
                        return {"tool": "shell", "args": {"cmd": cmdline}}
                    file_path = params.get("file_path") or params.get("filepath") or params.get("path") or params.get("file")
                    if isinstance(file_path, str) and file_path.strip():
                        file_path = _despace_path(file_path)
                        return {"tool": "shell", "args": {"cmd": f"{tool_name} {_quote_if_needed(file_path)}"}}

        # Common mistake: {"shell": {"cmd": "..."}}
        if "shell" in obj and isinstance(obj.get("shell"), dict):
            args = dict(obj["shell"])
            # Common mistake: use "command" instead of "cmd"
            if "cmd" not in args and "command" in args:
                args["cmd"] = args.pop("command")
            return {"tool": "shell", "args": args}

        # Another common mistake: {"cmd": "..."} (assume shell)
        if "cmd" in obj and isinstance(obj.get("cmd"), str):
            return {"tool": "shell", "args": {"cmd": obj["cmd"]}}
        if "command" in obj and isinstance(obj.get("command"), str):
            return {"tool": "shell", "args": {"cmd": obj["command"]}}

        # Generic: {"<tool_name>": {...}} with a single top-level key.
        if len(obj) == 1:
            (k, v), = obj.items()
            if isinstance(k, str) and isinstance(v, dict):
                if k == "shell":
                    args = dict(v)
                    if "cmd" not in args and "command" in args:
                        args["cmd"] = args.pop("command")
                    return {"tool": "shell", "args": args}
                return {"tool": k, "args": v}

        return None

    def _clean_iquest(s: str) -> str:
        # Remove tokenization artifacts like "<0x0A>" and "▁" and strip whitespace outside strings.
        s = s.replace("<0x0A>", "\n")
        s = s.replace("▁", " ")
        cleaned = _strip_ws_outside_strings(s)
        # Fix tokenized escapes inside strings: "\ n" -> "\n"
        cleaned = re.sub(r"\\\s+([A-Za-z0-9\"'\\\\])", r"\\\1", cleaned)
        return cleaned

    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        call = normalize(obj)
        if call:
            return call

    # Fallback: parse a fenced JSON block (common with smaller models).
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, flags=re.I)
    if fence:
        blob = fence.group(1).strip()
        try:
            obj = json.loads(blob)
        except Exception:
            obj = None
        call = normalize(obj) if obj is not None else None
        if call:
            return call

    # Fallback: try a single JSON object anywhere in the text.
    inline = re.search(r"(\{[\s\S]*\})", text)
    if inline:
        blob = inline.group(1).strip()
        try:
            obj = json.loads(blob)
        except Exception:
            obj = None
        call = normalize(obj) if obj is not None else None
        if call:
            return call

    # Fallback: iquest tokenized JSON-like output.
    cleaned = _clean_iquest(text)
    inline2 = re.search(r"(\{.*\})", cleaned)
    if inline2:
        blob = inline2.group(1).strip()
        try:
            obj = json.loads(blob)
        except Exception:
            obj = None
        call = normalize(obj) if obj is not None else None
        if call:
            return call
    return None

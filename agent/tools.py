# -*- coding: utf-8 -*-

import os
import re
import time
import posixpath
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    IMAGE_NAME, CONTAINER_NAME_PREFIX,
    DENY_PATTERNS,
    MAX_TOOL_SECONDS
)

try:
    import docker
except ImportError:
    docker = None

VENV_DIR = "/work/.venv"

@dataclass
class Sandbox:
    container_id: str
    name: str
    mem_limit: Optional[str]
    nano_cpus: Optional[int]
    pids_limit: Optional[int]
    privileged: bool
    network_mode: str

def shlex_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"

class SandboxManager:
    def __init__(self):
        if docker is None:
            raise RuntimeError("Missing dependency: pip install docker")
        self.client = docker.from_env()

    def ensure_venv(self, sandbox: Sandbox) -> None:
        # Create a per-task virtualenv under /work so runtime "pip install" works without root.
        # This also keeps dependencies isolated and writable.
        code, _ = self.exec(
            sandbox,
            [
                "bash",
                "-lc",
                f"test -x {shlex_quote(VENV_DIR + '/bin/python')} || python3 -m venv {shlex_quote(VENV_DIR)}",
            ],
            timeout_s=MAX_TOOL_SECONDS,
        )
        if code != 0:
            raise RuntimeError("Failed to initialize /work virtualenv")

    def build_image(self):
        import tempfile
        from pathlib import Path

        dockerfile_path = Path(__file__).resolve().parent.parent / "assets" / "docker" / "Dockerfile"
        if not dockerfile_path.exists():
            raise FileNotFoundError(f"Dockerfile not found: {dockerfile_path}")

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "Dockerfile").write_text(dockerfile_path.read_text(encoding="utf-8"), encoding="utf-8")
            image, logs = self.client.images.build(path=str(td), tag=IMAGE_NAME, rm=True)
            for chunk in logs:
                if "stream" in chunk:
                    line = chunk["stream"].strip()
                    if line:
                        print(line)

    def ensure_image(self):
        try:
            self.client.images.get(IMAGE_NAME)
        except Exception:
            self.build_image()

    def start(
        self,
        input_dir: Optional[str],
        work_dir: str,
        network_enabled: bool = True,
        mem_limit: str = "2g",
        nano_cpus: int = 2_000_000_000,
        pids_limit: int = 256,
    ) -> Sandbox:
        self.ensure_image()

        os.makedirs(work_dir, exist_ok=True)
        if input_dir and not os.path.isdir(input_dir):
            raise ValueError(f"input_dir not found: {input_dir}")

        name = f"{CONTAINER_NAME_PREFIX}{int(time.time())}-{os.getpid()}"
        volumes = {os.path.abspath(work_dir): {"bind": "/work", "mode": "rw"}}
        if input_dir:
            volumes[os.path.abspath(input_dir)] = {"bind": "/input", "mode": "ro"}

        network_mode = "bridge" if network_enabled else "none"

        # Lab-mode: full privilege for maximal autonomy/experimentation.
        # This disables most isolation guardrails (use only in trusted/local setups).
        privileged = True
        mem_limit = None if privileged else mem_limit
        nano_cpus = None if privileged else nano_cpus
        pids_limit = None if privileged else pids_limit

        container = self.client.containers.run(
            IMAGE_NAME,
            command=["bash", "-lc", "sleep infinity"],
            name=name,
            detach=True,
            tty=True,
            stdin_open=True,
            network_mode=network_mode,
            volumes=volumes,
            mem_limit=mem_limit,
            nano_cpus=nano_cpus,
            pids_limit=pids_limit,
            privileged=privileged,
        )

        sandbox = Sandbox(
            container_id=container.id,
            name=name,
            mem_limit=mem_limit,
            nano_cpus=nano_cpus,
            pids_limit=pids_limit,
            privileged=privileged,
            network_mode=network_mode,
        )
        self.ensure_venv(sandbox)
        return sandbox

    def stop(self, sandbox: Sandbox):
        try:
            c = self.client.containers.get(sandbox.container_id)
            c.remove(force=True)
        except Exception:
            pass

    def exec(self, sandbox: Sandbox, cmd: List[str], timeout_s: int = MAX_TOOL_SECONDS) -> Tuple[int, str]:
        c = self.client.containers.get(sandbox.container_id)

        # Run with timeout (coreutils timeout is present in our image)
        safe_cmd = ["bash", "-lc", f"timeout {timeout_s}s " + " ".join(map(shlex_quote, cmd))]
        exec_id = self.client.api.exec_create(c.id, safe_cmd, stdout=True, stderr=True)
        output = self.client.api.exec_start(exec_id, tty=False)
        inspect = self.client.api.exec_inspect(exec_id)
        code = inspect.get("ExitCode", 1)
        out = output.decode("utf-8", errors="replace") if isinstance(output, (bytes, bytearray)) else str(output)
        return code, out


class ToolBelt:
    def __init__(self, sandbox_mgr: SandboxManager, sandbox: Sandbox, brave_api_key: Optional[str]):
        self.sm = sandbox_mgr
        self.sandbox = sandbox
        # Kept for backward compatibility with older runners, but not used in shell-only mode.
        self.brave_api_key = brave_api_key
        self.cwd = "/work"
        self.env: Dict[str, str] = {}

    def _deny_check(self, cmdline: str):
        for pat in DENY_PATTERNS:
            if re.search(pat, cmdline):
                raise ValueError(f"Denied command pattern matched: {pat}")

    def _normalize_cwd(self, new_cwd: str) -> str:
        new_cwd = new_cwd.strip()
        if not new_cwd:
            return self.cwd
        if new_cwd.startswith("/"):
            resolved = posixpath.normpath(new_cwd)
        else:
            resolved = posixpath.normpath(posixpath.join(self.cwd, new_cwd))
        if not (
            resolved == "/work"
            or resolved.startswith("/work/")
            or resolved == "/input"
            or resolved.startswith("/input/")
        ):
            raise ValueError("Denied cwd outside /work or /input")
        return resolved

    def _update_persistent_state(self, cmdline: str) -> None:
        # Best-effort persistence for common shell state changes.
        # We track leading `cd ...` and `export KEY=VALUE` chained with && or ;.
        cmdline = cmdline.strip()
        if not cmdline:
            return
        parts = re.split(r"\s*(?:&&|;)\s*", cmdline)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^cd\s+(.+)$", part)
            if m:
                target = m.group(1).strip()
                # strip surrounding quotes if any
                if (target.startswith('"') and target.endswith('"')) or (target.startswith("'") and target.endswith("'")):
                    target = target[1:-1]
                self.cwd = self._normalize_cwd(target)
                continue
            m = re.match(r"^export\s+(.+)$", part)
            if m:
                exports = m.group(1).strip()
                # Parse KEY=VALUE tokens; ignore bare `export KEY`.
                try:
                    import shlex
                    tokens = shlex.split(exports)
                except Exception:
                    tokens = exports.split()
                for tok in tokens:
                    if "=" not in tok:
                        continue
                    k, v = tok.split("=", 1)
                    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                        continue
                    self.env[k] = v
                continue
            break

    def _wrap_cmd(self, cmdline: str) -> str:
        # Simulate a persistent shell session by re-applying cwd + env + venv PATH.
        # venv is always preferred so runtime `pip install ...` works without root.
        exports = "".join(
            f"export {k}={shlex_quote(v)}; " for k, v in sorted(self.env.items())
        )
        prefix = (
            f"cd {shlex_quote(self.cwd)}; "
            f"export VIRTUAL_ENV={shlex_quote(VENV_DIR)}; "
            f"export PATH={shlex_quote(VENV_DIR + '/bin')}:$PATH; "
            "export XDG_CACHE_HOME=/work/.cache; "
            "export PIP_CACHE_DIR=/work/.cache/pip; "
            "export NPM_CONFIG_CACHE=/work/.cache/npm; "
            "export PLAYWRIGHT_BROWSERS_PATH=/work/.cache/ms-playwright; "
            f"{exports}"
        )
        return prefix + cmdline

    def shell(self, cmd: str) -> Dict[str, Any]:
        cmd = cmd.strip()
        self._deny_check(cmd)
        self._update_persistent_state(cmd)

        # allowlist check: only 'bash' as entrypoint, but deny dangerous patterns above
        wrapped = self._wrap_cmd(cmd)
        code, out = self.sm.exec(self.sandbox, ["bash", "-lc", wrapped], timeout_s=MAX_TOOL_SECONDS)
        return {"exit_code": code, "output": out[-12000:], "cwd": self.cwd}

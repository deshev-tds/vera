#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import sys
from agent.loop import run_agent
from agent.tools import SandboxManager

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build", help="Build docker sandbox image")
    ap_dash = sub.add_parser("dashboard", help="Run local dashboard server")
    ap_dash.add_argument("--base-dir", default=".", help="Base dir for resolving relative --work-dir paths")
    ap_dash.add_argument("--host", default="127.0.0.1")
    ap_dash.add_argument("--port", type=int, default=8844)

    ap_run = sub.add_parser("run", help="Run a sandbox agent task")
    ap_run.add_argument("--task", required=True)
    ap_run.add_argument("--input-dir", default=None)
    ap_run.add_argument("--work-dir", required=True)
    ap_run.add_argument("--model-base-url", default=os.getenv("MODEL_BASE_URL", "http://127.0.0.1:1234"))   # e.g. http://127.0.0.1:1234[/v1]
    ap_run.add_argument("--model-name", default=os.getenv("MODEL_NAME", ""))       # optional for LM Studio (single loaded model)
    ap_run.add_argument("--brave-api-key", default=os.getenv("BRAVE_API_KEY"))
    ap_run.add_argument("--temperature", type=float, default=0.2)
    ap_run.add_argument("--max-steps", type=int, default=120, help="Max agent steps (0 for unlimited)")
    ap_run.add_argument("--prompt-profile", default=os.getenv("PROMPT_PROFILE", ""))  # e.g., "en", "iquest"
    ap_run.add_argument("--system-role", default=os.getenv("SYSTEM_ROLE", "system"))  # "system" or "user"

    args = ap.parse_args()

    if args.cmd == "build":
        SandboxManager().build_image()
        return

    if args.cmd == "dashboard":
        # Keep dashboard dependency-free: it's a small built-in HTTP server.
        from dashboard.server import main as dashboard_main
        sys.argv = ["dashboard"] + [
            "--base-dir", args.base_dir,
            "--host", args.host,
            "--port", str(args.port),
        ]
        dashboard_main()
        return

    if args.cmd == "run":
        out = run_agent(
            task=args.task,
            input_dir=args.input_dir,
            work_dir=args.work_dir,
            model_base_url=args.model_base_url,
            model_name=args.model_name,
            brave_api_key=args.brave_api_key,
            temperature=args.temperature,
            max_steps=args.max_steps,
            prompt_profile=(args.prompt_profile or None),
            system_role=args.system_role,
        )
        print("\n" + "=" * 80 + "\nFINAL ANSWER\n" + "=" * 80 + "\n")
        print(out)
        return

if __name__ == "__main__":
    main()

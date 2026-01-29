# -*- coding: utf-8 -*-

import os

IMAGE_NAME = "damyan/sandbox-agent:0.2"
CONTAINER_NAME_PREFIX = "sandbox-agent-"

DENY_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bdd\b",
    r"\bmkfs\b",
    r"\bmount\b",
    r"\bsudo\b",
    r"\bchown\b",
    r"\bchmod\b\s+777",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
]

MAX_TOOL_SECONDS = 900
DEFAULT_TIMEOUT = int(os.getenv("MODEL_TIMEOUT", "150"))
MAX_WEB_BYTES = 2_000_000

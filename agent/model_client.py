# -*- coding: utf-8 -*-

import os
import time
import requests
from .config import DEFAULT_TIMEOUT

class OpenAICompatClient:
    def __init__(self, base_url: str, model: str | None = None, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = (model or "").strip()
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")

    @staticmethod
    def normalize_base_url(base_url: str) -> str:
        """
        Accept either:
        - http://host:port/v1  (OpenAI-style base)
        - http://host:port     (LM Studio default), and normalize to /v1
        """
        base_url = (base_url or "").strip().rstrip("/")
        if not base_url:
            return base_url
        if base_url.endswith("/v1"):
            return base_url
        return base_url + "/v1"

    def chat_raw(self, messages, temperature=0.2, max_tokens=1200) -> dict:
        base = self.normalize_base_url(self.base_url)
        url = f"{base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if self.model:
            payload["model"] = self.model

        t0 = time.perf_counter()
        r = requests.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
        dt = time.perf_counter() - t0
        r.raise_for_status()
        data = r.json()
        data["_latency_s"] = dt
        return data

    def chat(self, messages, temperature=0.2, max_tokens=1200) -> str:
        data = self.chat_raw(messages, temperature=temperature, max_tokens=max_tokens)
        return data["choices"][0]["message"]["content"]

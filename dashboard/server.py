# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import os
import time
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import subprocess
from urllib.parse import parse_qs, urlparse


INDEX_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>VERA Dashboard</title>
    <style>
      :root {
        --bg: #0c0c0d;
        --panel: #141416;
        --text: #f2f2f2;
        --muted: #b6b6b6;
        --good: #51d88a;
        --bad: #ff5d73;
        --warn: #ffd27a;
        --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji";
      }
      body { margin:0; font-family: var(--sans); background: var(--bg); color: var(--text); }
      header { padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.08); display:flex; gap:12px; align-items:center; background: #101012; }
      header h1 { font-size: 16px; margin:0; font-weight: 650; }
      header .muted { color: var(--muted); font-size: 13px; }
      main { display:grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 12px; }
      @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
      .card { background: var(--panel); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; overflow: hidden; }
      .card h2 { margin:0; padding: 10px 12px; font-size: 13px; color: var(--muted); border-bottom: 1px solid rgba(255,255,255,0.08); }
      .card .body { padding: 10px 12px; }
      .mono { font-family: var(--mono); font-size: 12px; line-height: 1.4; white-space: pre-wrap; word-break: break-word; }
      .kv { display:flex; flex-wrap:wrap; gap: 10px 16px; font-size: 13px; }
      .kv div { min-width: 150px; }
      .pill { display:inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; border: 1px solid rgba(255,255,255,0.14); }
      .good { color: var(--good); border-color: rgba(81,216,138,0.4); }
      .bad { color: var(--bad); border-color: rgba(255,90,122,0.4); }
      .warn { color: var(--warn); border-color: rgba(255,204,102,0.4); }
      .full { grid-column: 1 / -1; }
      #log { height: 380px; max-height: 70vh; overflow-y: auto; padding: 8px 10px; font-family: var(--mono); font-size: 12px; line-height: 1.45; background: rgba(0,0,0,0.25); }
      #log .log-row { padding: 3px 6px; border-radius: 8px; margin: 2px 0; border: 1px solid transparent; white-space: pre-wrap; word-break: break-word; }
      #log .log-task { color: #9ad1ff; border-color: rgba(154,209,255,0.25); background: rgba(80,140,200,0.10); }
      #log .log-tool { color: #b8f2c8; border-color: rgba(81,216,138,0.25); background: rgba(60,140,90,0.10); }
      #log .log-tool-runtime { color: #c9e7d1; border-color: rgba(120,180,140,0.25); background: rgba(80,120,95,0.10); }
      #log .log-tool-output { color: #d8f8e2; border-color: rgba(120,220,170,0.18); background: rgba(70,140,100,0.06); }
      #log .log-assistant-agent { color: #f2f2f2; border-color: rgba(255,255,255,0.18); background: rgba(255,255,255,0.03); }
      #log .log-assistant-verifier { color: #e9ddff; border-color: rgba(180,140,255,0.28); background: rgba(120,80,200,0.10); }
      #log .log-model { color: #ffd27a; border-color: rgba(255,210,122,0.28); background: rgba(160,120,40,0.10); }
      #log .log-verifier { color: #ffb3c1; border-color: rgba(255,120,150,0.28); background: rgba(160,60,90,0.12); }
      #log .log-verifier-to-agent { color: #ffcad5; border-color: rgba(255,160,180,0.28); background: rgba(180,80,110,0.12); }
      #log .log-agent-from-verifier { color: #ffd9a8; border-color: rgba(255,190,120,0.28); background: rgba(170,110,50,0.12); }
      #log .log-policy { color: #9ee7ff; border-color: rgba(140,220,255,0.28); background: rgba(60,130,170,0.12); }
      #log .log-other { color: var(--muted); border-color: rgba(255,255,255,0.16); background: rgba(255,255,255,0.02); }
      textarea { width: 100%; min-height: 140px; height: 380px; max-height: 70vh; overflow-y: auto; resize: vertical; border-radius: 10px; border: 1px solid rgba(255,255,255,0.14); background: rgba(0,0,0,0.35); color: var(--text); padding: 10px; font-family: var(--mono); font-size: 12px; line-height: 1.45; }
      button { padding: 10px 12px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.14); background: rgba(255,255,255,0.06); color: var(--text); cursor: pointer; }
      button:disabled { opacity: 0.5; cursor: not-allowed; }
      .row { display:flex; gap: 10px; align-items:center; }
      .row > * { flex: 0 0 auto; }
      .row .grow { flex: 1 1 auto; }
      a { color: #c7d7ff; }
    </style>
  </head>
  <body>
    <header>
      <h1>VERA Dashboard</h1>
      <div class="muted">Live tail of <span class="mono">trace.jsonl</span></div>
      <div style="flex:1"></div>
      <div class="muted">Session: <span id="workDir" class="mono"></span></div>
    </header>
    <main>
      <section class="card full">
        <h2>Run Control</h2>
        <div class="body">
          <div class="muted" style="font-size:13px;margin-bottom:8px;">
            Enter a task prompt once. When you start, the prompt locks and telemetry begins.
          </div>
          <div class="row" style="margin-bottom:10px;">
            <div class="grow">
              <div class="muted" style="font-size:12px;margin-bottom:6px;">Session (work_dir)</div>
              <div class="row">
                <select id="sessionSelect" class="mono grow" style="width:100%;padding:10px;border-radius:10px;border:1px solid rgba(255,255,255,0.14);background:rgba(0,0,0,0.35);color:var(--text);">
                  <option value="">(loading...)</option>
                </select>
                <button id="openSessionBtn">Open</button>
                <button id="newSessionBtn">New</button>
              </div>
              <div class="muted" style="font-size:12px;margin-top:6px;" id="sessionHint"></div>
            </div>
          </div>
          <div class="row" style="margin-bottom:10px;">
            <div class="grow">
              <div class="muted" style="font-size:12px;margin-bottom:6px;">Model base URL</div>
              <input id="baseUrl" class="mono" style="width:100%;padding:10px;border-radius:10px;border:1px solid rgba(255,255,255,0.14);background:rgba(0,0,0,0.2);color:var(--text);" />
            </div>
            <div class="grow">
              <div class="muted" style="font-size:12px;margin-bottom:6px;">Model name (optional)</div>
              <input id="modelName" class="mono" style="width:100%;padding:10px;border-radius:10px;border:1px solid rgba(255,255,255,0.14);background:rgba(0,0,0,0.2);color:var(--text);" placeholder="(leave empty for LM Studio single-model)" />
            </div>
          </div>
          <textarea id="taskPrompt" placeholder="Enter task prompt..."></textarea>
          <div class="row" style="margin-top:10px;">
            <button id="startBtn">Start Run</button>
            <div class="muted grow" id="startHint"></div>
            <div class="muted">Status: <span id="runStatus" class="pill warn">idle</span></div>
          </div>
        </div>
      </section>
      <section class="card">
        <h2>Status</h2>
        <div class="body kv">
          <div>Run: <span id="runId" class="pill warn">unknown</span></div>
          <div>Step: <span id="step" class="pill warn">0</span></div>
          <div>Last event: <span id="lastType" class="pill warn">none</span></div>
          <div>Tool errors: <span id="toolErrors" class="pill warn">0</span></div>
          <div>Verifier score: <span id="verScore" class="pill warn">-</span></div>
          <div>Verifier time: <span id="verTime" class="pill warn">-</span></div>
          <div>Verifier tokens: <span id="verTokens" class="pill warn">-</span></div>
          <div>Agent latency: <span id="agentLatency" class="pill warn">-</span></div>
          <div>Agent tokens: <span id="agentTokens" class="pill warn">-</span></div>
          <div>Container: <span id="containerId" class="pill warn">-</span></div>
          <div>Privileged: <span id="sandboxPriv" class="pill warn">-</span></div>
          <div>Mem limit: <span id="sandboxMem" class="pill warn">-</span></div>
          <div>CPU limit: <span id="sandboxCpu" class="pill warn">-</span></div>
          <div>PIDs limit: <span id="sandboxPids" class="pill warn">-</span></div>
        </div>
      </section>
      <section class="card">
        <h2>Metrics (Live)</h2>
        <div class="body mono" id="metricsBox">(waiting...)</div>
      </section>
      <section class="card">
        <h2>Last Tool</h2>
        <div class="body mono" id="lastTool">(none)</div>
      </section>
      <section class="card">
        <h2>Event Log</h2>
        <div class="mono" id="log"></div>
      </section>
      <section class="card">
        <h2>Notes</h2>
        <div class="body">
          <div class="muted" style="font-size:13px;margin-bottom:8px;">Tip: keep <span class="mono">/work/notes.md</span> for human-readable notes; this dashboard is for live telemetry.</div>
          <textarea id="notes" readonly></textarea>
          <div class="muted" style="font-size:12px;margin-top:8px;">
            Metrics endpoint: <a id="metricsLink" href="/metrics" target="_blank" rel="noreferrer">/metrics</a>
          </div>
        </div>
      </section>
      <section class="card">
        <h2>Ledgers</h2>
        <div class="body">
          <div class="muted" style="font-size:12px;margin-bottom:6px;"><span class="mono">/work/evidence.jsonl</span></div>
          <textarea id="evidenceLedger" readonly style="height:140px;min-height:120px;"></textarea>
          <div class="muted" style="font-size:12px;margin:10px 0 6px;"><span class="mono">/work/move_ledger.jsonl</span></div>
          <textarea id="moveLedger" readonly style="height:140px;min-height:120px;"></textarea>
          <div class="muted" style="font-size:12px;margin:10px 0 6px;"><span class="mono">/work/query_ledger.jsonl</span></div>
          <textarea id="queryLedger" readonly style="height:140px;min-height:120px;"></textarea>
        </div>
      </section>
      <section class="card">
        <h2>Container Logs</h2>
        <div class="body">
          <div class="muted" style="font-size:12px;margin-bottom:6px;"><span class="mono">/work/container.log</span></div>
          <textarea id="containerLog" readonly style="height:160px;min-height:120px;"></textarea>
          <div class="muted" style="font-size:12px;margin:10px 0 6px;"><span class="mono">/work/container_events.log</span></div>
          <textarea id="containerEvents" readonly style="height:160px;min-height:120px;"></textarea>
        </div>
      </section>
      <section class="card full">
        <h2>Model I/O (Raw)</h2>
        <div class="body">
          <div class="muted" style="font-size:13px;margin-bottom:8px;">Last raw request/response snapshot from the model.</div>
          <textarea id="modelIO" readonly></textarea>
        </div>
      </section>
    </main>
    <script>
      const params = new URLSearchParams(window.location.search);
      const workDir = params.get("work_dir") || "";
      document.getElementById("workDir").textContent = workDir || "(not set)";
      const metricsLink = document.getElementById("metricsLink");
      if (metricsLink && workDir) {
        metricsLink.href = `/metrics?work_dir=${encodeURIComponent(workDir)}`;
        metricsLink.textContent = metricsLink.href;
      }

      const promptEl = document.getElementById("taskPrompt");
      const startBtn = document.getElementById("startBtn");
      const startHint = document.getElementById("startHint");
      const runStatus = document.getElementById("runStatus");
      const baseUrlEl = document.getElementById("baseUrl");
      const modelNameEl = document.getElementById("modelName");
      const sessionSelect = document.getElementById("sessionSelect");
      const openSessionBtn = document.getElementById("openSessionBtn");
      const newSessionBtn = document.getElementById("newSessionBtn");
      const sessionHint = document.getElementById("sessionHint");

      function lockPrompt(task) {
        promptEl.value = task || promptEl.value;
        promptEl.readOnly = true;
        promptEl.style.opacity = "0.8";
        startBtn.disabled = true;
        baseUrlEl.readOnly = true;
        baseUrlEl.style.opacity = "0.8";
        modelNameEl.readOnly = true;
        modelNameEl.style.opacity = "0.8";
      }

      function setRunStatus(text, cls) {
        pill(runStatus, text, cls || "warn");
      }

      const lockKey = workDir ? `task_locked:${workDir}` : null;
      const cfgKey = workDir ? `cfg:${workDir}` : null;
      const defaultBaseUrl = "http://127.0.0.1:1234";
      baseUrlEl.value = defaultBaseUrl;
      modelNameEl.value = "";

      async function loadSessions() {
        try {
          const r = await fetch("/sessions");
          const j = await r.json();
          const sessions = (j.sessions || []).filter(s => typeof s === "string");
          sessionSelect.innerHTML = "";
          for (const s of sessions) {
            const opt = document.createElement("option");
            opt.value = s;
            opt.textContent = s;
            sessionSelect.appendChild(opt);
          }
          if (!sessions.length) {
            const opt = document.createElement("option");
            opt.value = "";
            opt.textContent = "(no sessions found)";
            sessionSelect.appendChild(opt);
          }
          if (workDir && sessions.includes(workDir)) {
            sessionSelect.value = workDir;
          } else if (!workDir && sessions.length) {
            sessionSelect.value = sessions[0];
          }
          sessionHint.textContent = "Pick an existing work_dir session or create a new one.";
        } catch (e) {
          sessionHint.textContent = "Failed to load sessions.";
        }
      }

      function goToSession(wd) {
        const u = new URL(window.location.href);
        u.searchParams.set("work_dir", wd);
        window.location.href = u.toString();
      }

      openSessionBtn.addEventListener("click", () => {
        const wd = (sessionSelect.value || "").trim();
        if (!wd) return;
        goToSession(wd);
      });

      newSessionBtn.addEventListener("click", async () => {
        try {
          const r = await fetch("/new_session", { method: "POST", headers: {"Content-Type":"application/json"}, body: "{}" });
          const j = await r.json();
          if (!r.ok) {
            sessionHint.textContent = j.error || "Failed to create session.";
            return;
          }
          const wd = j.work_dir;
          if (wd) goToSession(wd);
        } catch (e) {
          sessionHint.textContent = "Failed to create session.";
        }
      });

      loadSessions();

      if (cfgKey) {
        try {
          const prev = JSON.parse(localStorage.getItem(cfgKey) || "null");
          if (prev && typeof prev === "object") {
            if (prev.base_url) baseUrlEl.value = prev.base_url;
            if (prev.model_name !== undefined) modelNameEl.value = prev.model_name;
          }
        } catch (_) {}
      }
      if (lockKey && localStorage.getItem(lockKey) === "1") {
        lockPrompt(localStorage.getItem(`task_value:${workDir}`) || "");
        setRunStatus("running", "good");
      } else if (!workDir) {
        startHint.textContent = "Tip: open with ?work_dir=./work/my-run (or just press Start Run to create one).";
      }

      startBtn.addEventListener("click", async () => {
        const task = (promptEl.value || "").trim();
        if (!task) {
          startHint.textContent = "Please enter a task prompt first.";
          return;
        }
        startBtn.disabled = true;
        setRunStatus("starting", "warn");
        startHint.textContent = "Starting run...";

        try {
          const model_base_url = (baseUrlEl.value || "").trim() || defaultBaseUrl;
          const model_name = (modelNameEl.value || "").trim();
          const resp = await fetch("/start_run", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ task, work_dir: workDir || null, model_base_url, model_name })
          });
          const data = await resp.json();
          if (!resp.ok) {
            setRunStatus("error", "bad");
            startBtn.disabled = false;
            startHint.textContent = data.error || "Failed to start run.";
            return;
          }
          const newWork = data.work_dir;
          if (newWork && newWork !== workDir) {
            // Redirect to include work_dir so the dashboard can follow trace.jsonl.
            const u = new URL(window.location.href);
            u.searchParams.set("work_dir", newWork);
            window.location.href = u.toString();
            return;
          }
          if (workDir) {
            localStorage.setItem(lockKey, "1");
            localStorage.setItem(`task_value:${workDir}`, task);
            localStorage.setItem(cfgKey, JSON.stringify({ base_url: model_base_url, model_name }));
          }
          lockPrompt(task);
          setRunStatus("running", "good");
          startHint.textContent = "Run started.";
        } catch (e) {
          setRunStatus("error", "bad");
          startBtn.disabled = false;
          startHint.textContent = "Failed to start run (network/server error).";
        }
      });

      async function pollRunStatus() {
        if (!workDir) return;
        try {
          const r = await fetch(`/run_status?work_dir=${encodeURIComponent(workDir)}`);
          if (!r.ok) return;
          const j = await r.json();
          if (j.status === "running") setRunStatus("running", "good");
          else if (j.status === "exited") setRunStatus("exited", "warn");
        } catch (_) {}
      }
      setInterval(pollRunStatus, 1500);
      pollRunStatus();

      function pill(el, text, cls) {
        el.textContent = text;
        el.className = "pill " + (cls || "warn");
      }

      const logEl = document.getElementById("log");
      const notesEl = document.getElementById("notes");
      const evidenceLedgerEl = document.getElementById("evidenceLedger");
      const moveLedgerEl = document.getElementById("moveLedger");
      const queryLedgerEl = document.getElementById("queryLedger");
      const containerLogEl = document.getElementById("containerLog");
      const containerEventsEl = document.getElementById("containerEvents");
      const modelIoEl = document.getElementById("modelIO");
      const metricsBox = document.getElementById("metricsBox");
      let toolErrors = 0;
      let lastVerifierScore = null;
      let lastStep = 0;
      let lastAgentLatency = null;
      let lastAgentTokens = null;

      function makeAutoScroller(el) {
        let stick = true;
        const threshold = 24;
        function nearBottom() {
          return (el.scrollHeight - el.scrollTop - el.clientHeight) < threshold;
        }
        el.addEventListener("scroll", () => {
          stick = nearBottom();
        });
        return {
          shouldStick() { return stick || nearBottom(); },
          scrollToBottom() { el.scrollTop = el.scrollHeight; },
        };
      }

      const logScroll = makeAutoScroller(logEl);
      const notesScroll = makeAutoScroller(notesEl);
      const evidenceLedgerScroll = makeAutoScroller(evidenceLedgerEl);
      const moveLedgerScroll = makeAutoScroller(moveLedgerEl);
      const queryLedgerScroll = makeAutoScroller(queryLedgerEl);
      const containerLogScroll = makeAutoScroller(containerLogEl);
      const containerEventsScroll = makeAutoScroller(containerEventsEl);
      const modelIoScroll = makeAutoScroller(modelIoEl);

      const logMaxRows = 900;
      function logClass(type, scope) {
        if (type === "task") return "log-task";
        if (type === "tool") return scope === "runtime" ? "log-tool-runtime" : "log-tool";
        if (type === "tool_output") return "log-tool-output";
        if (type === "assistant") return String(scope || "").startsWith("verifier") ? "log-assistant-verifier" : "log-assistant-agent";
        if (type === "model") return "log-model";
        if (type === "verifier") return "log-verifier";
        if (type === "verifier_to_agent") return "log-verifier-to-agent";
        if (type === "agent_from_verifier") return "log-agent-from-verifier";
        if (type === "policy_reminder" || type === "policy_choice" || type === "policy_stagnation" || type === "policy_query_mutation" || type === "policy_query_vector" || type === "policy_domain_shift" || type === "policy_conclusion_ready" || type === "policy_source_budget" || type === "policy_brave_budget" || type === "policy_brave_circuit" || type === "verifier_gradient") return "log-policy";
        return "log-other";
      }

      function appendLog(line, type, scope) {
        const shouldStick = logScroll.shouldStick();
        const row = document.createElement("div");
        row.className = `log-row ${logClass(type || "other", scope || "")}`;
        row.textContent = line;
        logEl.appendChild(row);
        while (logEl.children.length > logMaxRows) {
          logEl.removeChild(logEl.firstChild);
        }
        if (shouldStick) logScroll.scrollToBottom();
      }

      async function refreshNotes() {
        if (!workDir) return;
        try {
          const r = await fetch(`/notes?work_dir=${encodeURIComponent(workDir)}`);
          if (!r.ok) return;
          const shouldStick = notesScroll.shouldStick();
          notesEl.value = await r.text();
          if (shouldStick) notesScroll.scrollToBottom();
        } catch (_) {}
      }
      setInterval(refreshNotes, 1500);
      refreshNotes();

      async function refreshLedgers() {
        if (!workDir) return;
        try {
          const r = await fetch(`/evidence?work_dir=${encodeURIComponent(workDir)}`);
          if (r.ok) {
            const shouldStick = evidenceLedgerScroll.shouldStick();
            evidenceLedgerEl.value = await r.text();
            if (shouldStick) evidenceLedgerScroll.scrollToBottom();
          }
        } catch (_) {}
        try {
          const r = await fetch(`/move_ledger?work_dir=${encodeURIComponent(workDir)}`);
          if (r.ok) {
            const shouldStick = moveLedgerScroll.shouldStick();
            moveLedgerEl.value = await r.text();
            if (shouldStick) moveLedgerScroll.scrollToBottom();
          }
        } catch (_) {}
        try {
          const r = await fetch(`/query_ledger?work_dir=${encodeURIComponent(workDir)}`);
          if (r.ok) {
            const shouldStick = queryLedgerScroll.shouldStick();
            queryLedgerEl.value = await r.text();
            if (shouldStick) queryLedgerScroll.scrollToBottom();
          }
        } catch (_) {}
      }
      setInterval(refreshLedgers, 1500);
      refreshLedgers();

      async function refreshContainerLogs() {
        if (!workDir) return;
        try {
          const logResp = await fetch(`/container_log?work_dir=${encodeURIComponent(workDir)}`);
          if (logResp.ok) {
            const shouldStick = containerLogScroll.shouldStick();
            containerLogEl.value = await logResp.text();
            if (shouldStick) containerLogScroll.scrollToBottom();
          }
        } catch (_) {}
        try {
          const eventsResp = await fetch(`/container_events?work_dir=${encodeURIComponent(workDir)}`);
          if (eventsResp.ok) {
            const shouldStick = containerEventsScroll.shouldStick();
            containerEventsEl.value = await eventsResp.text();
            if (shouldStick) containerEventsScroll.scrollToBottom();
          }
        } catch (_) {}
      }
      setInterval(refreshContainerLogs, 1500);
      refreshContainerLogs();

      async function refreshMetrics() {
        if (!workDir) return;
        try {
          const r = await fetch(`/metrics_json?work_dir=${encodeURIComponent(workDir)}`);
          if (!r.ok) return;
          const m = await r.json();
          const avgVer = (m.verifier_duration_s_count || 0) ? (m.verifier_duration_s_sum / m.verifier_duration_s_count) : 0;
          const toolByScope = m.tool_calls_by_scope || {};
          const modelByScope = m.model_tokens_total_by_scope || {};
          metricsBox.textContent =
            `events_total=${m.events_total}\\n` +
            `tool_calls_total=${m.tool_calls_total} tool_errors_total=${m.tool_errors_total} tool_calls_agent=${toolByScope.agent ?? 0} tool_calls_verifier=${toolByScope.verifier ?? 0}\\n` +
            `verifier_last_score=${m.verifier_last_score ?? "-"} verifier_avg_s=${avgVer.toFixed(2)} verifier_before_tools_total=${m.verifier_before_tools_total ?? 0}\\n` +
            `verifier_tokens_total=${m.verifier_model_tokens_total ?? 0} verifier_tool_calls_total=${m.verifier_tool_calls_total ?? 0} verifier_gradient_total=${m.verifier_gradient_total ?? 0}\\n` +
            `model_calls_total=${m.model_calls_total} model_tokens_agent=${modelByScope.agent ?? 0} finish_reason_length_total=${m.model_finish_reason_length_total ?? 0}\\n` +
            `policy_pre_tool_nudge_total=${m.policy_pre_tool_nudge_total ?? 0} policy_length_nudge_total=${m.policy_length_nudge_total ?? 0} policy_reminder_total=${m.policy_reminder_total ?? 0} policy_choice_total=${m.policy_choice_total ?? 0} policy_choice_matched_total=${m.policy_choice_matched_total ?? 0} policy_stagnation_total=${m.policy_stagnation_total ?? 0} policy_query_vector_total=${m.policy_query_vector_total ?? 0} policy_domain_shift_total=${m.policy_domain_shift_total ?? 0} policy_conclusion_ready_total=${m.policy_conclusion_ready_total ?? 0} policy_source_budget_total=${m.policy_source_budget_total ?? 0} policy_brave_budget_total=${m.policy_brave_budget_total ?? 0} policy_brave_circuit_total=${m.policy_brave_circuit_total ?? 0}\\n` +
            `max_step=${m.max_step} last_ts=${m.last_ts ? new Date(m.last_ts*1000).toLocaleTimeString() : "-"}`;
        } catch (_) {}
      }
      setInterval(refreshMetrics, 1000);
      refreshMetrics();

      const es = workDir ? new EventSource(`/events?work_dir=${encodeURIComponent(workDir)}`) : null;
      if (!es) {
        pill(document.getElementById("runId"), "no work_dir", "warn");
      }
      if (es) {
        es.onopen = () => {
          // We may not have seen a "task" event yet, but the stream is live.
          pill(document.getElementById("runId"), "connected", "good");
        };
      }
      es.onmessage = (ev) => {
        const data = JSON.parse(ev.data);
        const type = data.type || "unknown";
        if (type === "sse") {
          pill(document.getElementById("runId"), "connected", "good");
          return;
        }
        const step = data.step || 0;
        lastStep = Math.max(lastStep, step);
        pill(document.getElementById("step"), String(lastStep), "good");
        pill(document.getElementById("lastType"), type, "good");

        if (type === "task") {
          pill(document.getElementById("runId"), "active", "good");
          appendLog(`[task] ${data.task || ""}`, "task");
        } else if (type === "tool") {
          const tool = data.tool || "";
          const argsObj = data.args || {};
          const args = JSON.stringify(argsObj);
          const obs = data.obs || {};
          const exitCode = obs.exit_code;
          const status = (exitCode === 0 || exitCode === undefined) ? "good" : "bad";
          if (status === "bad") toolErrors += 1;
          pill(document.getElementById("toolErrors"), String(toolErrors), toolErrors ? "bad" : "good");
          const scope = data.scope || "agent";
          const err = obs.error ? ` error=${obs.error_type ? obs.error_type + ":" : ""}${obs.error}` : "";
          const cmd = (argsObj && typeof argsObj.cmd === "string") ? argsObj.cmd : "";
          const out = String(obs.output || obs.text || "");
          document.getElementById("lastTool").textContent =
            `${scope}:${tool}\\nexit_code=${exitCode}${err}\\n` +
            (cmd ? `cmd: ${cmd}\\n` : "") +
            `${out.slice(0, 4000)}`;
          appendLog(`[tool scope=${scope}] ${tool} exit=${exitCode}${err}` + (cmd ? ` cmd=${cmd}` : ""), "tool", scope);
          if (out) appendLog(out.slice(0, 2000), "tool_output", scope);
        } else if (type === "verifier") {
          const score = (((data.decision || {}).score) ?? null);
          const meta = ((data.decision || {}).meta) || {};
          const dur = meta.duration_s ?? null;
          const vtok = ((meta.verifier_usage || {}).total_tokens) ?? null;
          lastVerifierScore = score;
          const cls = (score >= 3) ? "good" : (score === null ? "warn" : "bad");
          pill(document.getElementById("verScore"), score === null ? "-" : `${score}/4`, cls);
          pill(document.getElementById("verTime"), dur === null ? "-" : `${dur.toFixed(2)}s`, dur === null ? "warn" : "good");
          pill(document.getElementById("verTokens"), vtok === null ? "-" : String(vtok), vtok === null ? "warn" : "good");
          appendLog(`[verifier] score=${score} ${(data.decision || {}).explanation || ""}`, "verifier");
        } else if (type === "verifier_to_agent") {
          const score = data.score ?? "-";
          appendLog(
            `[verifier→agent] score=${score} ${String((data.content||"")).slice(0, 1200).replaceAll("\\n"," ")}`,
            "verifier_to_agent"
          );
        } else if (type === "verifier_gradient") {
          const grad = data.gradient || {};
          const missingCount = Array.isArray(grad.missing) ? grad.missing.length : 0;
          const nextCount = Array.isArray(grad.next_actions) ? grad.next_actions.length : 0;
          appendLog(
            `[gradient] score=${grad.score ?? "-"} missing=${missingCount} next_actions=${nextCount}`,
            "verifier_gradient"
          );
        } else if (type === "agent_from_verifier") {
          appendLog(`[agent] received verifier feedback (messages=${data.n_messages ?? "-"})`, "agent_from_verifier");
        } else if (type === "policy_reminder") {
          appendLog(
            `[policy] reminder count=${data.gradient_reminders ?? "-"}`,
            "policy_reminder"
          );
        } else if (type === "policy_choice") {
          appendLog(
            `[policy] choice matched=${data.matched ? "yes" : "no"} tool=${data.tool || ""}`,
            "policy_choice"
          );
        } else if (type === "policy_stagnation") {
          const streak = data.streak ?? "-";
          const limit = data.limit ?? "-";
          const failure = data.failure_type || "none";
          const failureStreak = data.failure_streak ?? 0;
          appendLog(
            `[policy] stagnation streak=${streak}/${limit} failure=${failure} failure_streak=${failureStreak}`,
            "policy_stagnation"
          );
        } else if (type === "policy_query_vector") {
          const required = data.required ?? "-";
          const seen = data.seen ?? "-";
          const current = data.current || "-";
          const last = data.last || "-";
          appendLog(
            `[policy] query_vector required=${required} seen=${seen} current=${current} last=${last}`,
            "policy_query_vector"
          );
        } else if (type === "policy_domain_shift") {
          const domain = data.domain || "-";
          const official = data.official_checked ?? "-";
          const independent = data.independent_checked ?? "-";
          const limit = data.limit ?? "-";
          appendLog(
            `[policy] domain_shift domain=${domain} official=${official} independent=${independent} limit=${limit}`,
            "policy_domain_shift"
          );
        } else if (type === "policy_conclusion_ready") {
          const official = data.official_checked ?? "-";
          const independent = data.independent_checked ?? "-";
          const budget = data.budget_steps ?? "-";
          appendLog(
            `[policy] conclusion_ready official=${official} independent=${independent} budget_steps=${budget}`,
            "policy_conclusion_ready"
          );
        } else if (type === "policy_source_budget") {
          const domains = data.domains_checked ?? "-";
          const budget = data.source_budget ?? "-";
          appendLog(
            `[policy] source_budget domains=${domains} budget=${budget}`,
            "policy_source_budget"
          );
        } else if (type === "policy_brave_budget") {
          const calls = data.calls ?? "-";
          const maxCalls = data.max_calls ?? "-";
          appendLog(
            `[policy] brave_budget calls=${calls} max=${maxCalls}`,
            "policy_brave_budget"
          );
        } else if (type === "policy_brave_circuit") {
          const until = data.cooldown_until ?? "-";
          appendLog(
            `[policy] brave_circuit cooldown_until=${until}`,
            "policy_brave_circuit"
          );
        } else if (type === "model") {
          const scope = data.scope || "unknown";
          if (scope === "agent") {
            lastAgentLatency = data.latency_s ?? null;
            lastAgentTokens = ((data.usage || {}).total_tokens) ?? null;
            pill(document.getElementById("agentLatency"), lastAgentLatency === null ? "-" : `${Number(lastAgentLatency).toFixed(2)}s`, "good");
            pill(document.getElementById("agentTokens"), lastAgentTokens === null ? "-" : String(lastAgentTokens), "good");
            appendLog(
              `[model scope=agent] latency=${Number(lastAgentLatency||0).toFixed(2)}s tokens=${lastAgentTokens ?? "-"}`,
              "model",
              scope
            );
          } else {
            const t = ((data.usage || {}).total_tokens) ?? "-";
            const lat = Number(data.latency_s || 0).toFixed(2);
            appendLog(`[model scope=${scope}] latency=${lat}s tokens=${t}`, "model", scope);
          }
        } else if (type === "model_io") {
          const shouldStick = modelIoScroll.shouldStick();
          const payload = {
            request: data.request || {},
            response: data.response || {},
          };
          modelIoEl.value = JSON.stringify(payload, null, 2);
          if (shouldStick) modelIoScroll.scrollToBottom();
        } else if (type === "assistant") {
          const scope = data.scope || "agent";
          appendLog(
            `[assistant scope=${scope}] ${String((data.content || "")).slice(0, 1200).replaceAll("\\n"," ")}`,
            "assistant",
            scope
          );
        } else if (type === "sandbox") {
          const cid = data.container_id ? String(data.container_id).slice(0, 12) : "-";
          pill(document.getElementById("containerId"), cid, cid === "-" ? "warn" : "good");
          const priv = data.privileged ? "yes" : "no";
          pill(document.getElementById("sandboxPriv"), priv, data.privileged ? "warn" : "good");
          const mem = data.mem_limit || "none";
          const cpu = (data.nano_cpus !== null && data.nano_cpus !== undefined) ? String(data.nano_cpus) : "none";
          const pids = (data.pids_limit !== null && data.pids_limit !== undefined) ? String(data.pids_limit) : "none";
          pill(document.getElementById("sandboxMem"), mem, mem === "none" ? "warn" : "good");
          pill(document.getElementById("sandboxCpu"), cpu, cpu === "none" ? "warn" : "good");
          pill(document.getElementById("sandboxPids"), pids, pids === "none" ? "warn" : "good");
          appendLog(`[sandbox] id=${cid} mem=${mem} cpu=${cpu} pids=${pids} priv=${priv}`, "other");
        } else if (type === "container_event") {
          const status = ((data.event || {}).status) || ((data.event || {}).Action) || "event";
          appendLog(`[container] ${status}`, "other");
        } else if (type === "heartbeat") {
          // keep-alive; no-op
        } else {
          appendLog(`[${type}]`, type);
        }
      };
      es.onerror = () => {
        pill(document.getElementById("runId"), "disconnected", "bad");
      };
    </script>
  </body>
</html>
"""


def read_last_lines(path: Path, n: int = 200) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b""
            pos = size
            while pos > 0 and data.count(b"\n") <= n:
                read_size = min(block, pos)
                pos -= read_size
                f.seek(pos, os.SEEK_SET)
                data = f.read(read_size) + data
            lines = data.splitlines()[-n:]
            return [ln.decode("utf-8", errors="replace") for ln in lines]
    except Exception:
        return []

def list_sessions(base_dir: Path, max_items: int = 200) -> list[str]:
    work_root = (base_dir / "work").resolve()
    if not work_root.exists():
        return []
    sessions: list[tuple[float, str]] = []
    try:
        for p in work_root.iterdir():
            if not p.is_dir():
                continue
            # A session is a folder containing either trace.jsonl or notes.md or run.log
            marker = None
            for name in ("trace.jsonl", "notes.md", "run.log"):
                if (p / name).exists():
                    marker = p / name
                    break
            if not marker:
                continue
            try:
                mtime = marker.stat().st_mtime
            except Exception:
                mtime = 0.0
            rel = "./work/" + p.name
            sessions.append((mtime, rel))
    except Exception:
        return []
    sessions.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in sessions[:max_items]]

def append_session_log(work_dir: Path, event: dict) -> None:
    """
    Append an auditable, per-session control log.
    This captures dashboard-driven actions (start_run/new_session) separate from the agent's own trace.jsonl.
    """
    try:
        event = dict(event)
        event.setdefault("ts", time.time())
        p = work_dir / "session.log"
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        # Best-effort logging only.
        return


def compute_metrics(trace_path: Path, max_lines: int = 5000) -> dict:
    metrics = {
        "events_total": 0,
        "events_by_type": {},
        "tool_calls_total": 0,
        "tool_calls_by_tool": {},
        "tool_errors_total": 0,
        "policy_stagnation_total": 0,
        "policy_query_vector_total": 0,
        "policy_domain_shift_total": 0,
        "policy_conclusion_ready_total": 0,
        "policy_source_budget_total": 0,
        "policy_brave_budget_total": 0,
        "policy_brave_circuit_total": 0,
        "verifier_scores_total": {1: 0, 2: 0, 3: 0, 4: 0},
        "max_step": 0,
        "last_ts": 0.0,
    }
    lines = read_last_lines(trace_path, n=max_lines)
    for raw in lines:
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        metrics["events_total"] += 1
        t = ev.get("type", "unknown")
        metrics["events_by_type"][t] = metrics["events_by_type"].get(t, 0) + 1
        step = int(ev.get("step") or 0)
        metrics["max_step"] = max(metrics["max_step"], step)
        ts = float(ev.get("ts") or 0.0)
        metrics["last_ts"] = max(metrics["last_ts"], ts)
        if t == "tool":
            metrics["tool_calls_total"] += 1
            tool = ev.get("tool", "unknown")
            metrics["tool_calls_by_tool"][tool] = metrics["tool_calls_by_tool"].get(tool, 0) + 1
            obs = ev.get("obs") or {}
            exit_code = obs.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                metrics["tool_errors_total"] += 1
        if t == "policy_stagnation":
            metrics["policy_stagnation_total"] += 1
        if t == "policy_query_vector":
            metrics["policy_query_vector_total"] += 1
        if t == "policy_domain_shift":
            metrics["policy_domain_shift_total"] += 1
        if t == "policy_conclusion_ready":
            metrics["policy_conclusion_ready_total"] += 1
        if t == "policy_source_budget":
            metrics["policy_source_budget_total"] += 1
        if t == "policy_brave_budget":
            metrics["policy_brave_budget_total"] += 1
        if t == "policy_brave_circuit":
            metrics["policy_brave_circuit_total"] += 1
        if t == "verifier":
            d = (ev.get("decision") or {})
            score = d.get("score")
            if isinstance(score, int) and score in metrics["verifier_scores_total"]:
                metrics["verifier_scores_total"][score] += 1
    return metrics


def render_prometheus(metrics: dict) -> str:
    lines: list[str] = []

    def emit(name: str, value: float, labels: dict | None = None):
        if labels:
            def esc(x: object) -> str:
                return str(x).replace('"', '\\"')
            lab = ",".join([f'{k}="{esc(v)}"' for k, v in labels.items()])
            lines.append(f"{name}{{{lab}}} {value}")
        else:
            lines.append(f"{name} {value}")

    emit("dra_events_total", metrics.get("events_total", 0))
    for t, c in sorted((metrics.get("events_by_type") or {}).items()):
        emit("dra_events_total", c, {"type": t})
    emit("dra_tool_calls_total", metrics.get("tool_calls_total", 0))
    for tool, c in sorted((metrics.get("tool_calls_by_tool") or {}).items()):
        emit("dra_tool_calls_total", c, {"tool": tool})
    for scope, c in sorted((metrics.get("tool_calls_by_scope") or {}).items()):
        emit("dra_tool_calls_total", c, {"scope": scope})
    emit("dra_tool_errors_total", metrics.get("tool_errors_total", 0))
    emit("dra_policy_pre_tool_nudge_total", int(metrics.get("policy_pre_tool_nudge_total") or 0))
    emit("dra_policy_length_nudge_total", int(metrics.get("policy_length_nudge_total") or 0))
    emit("dra_policy_reminder_total", int(metrics.get("policy_reminder_total") or 0))
    emit("dra_policy_choice_total", int(metrics.get("policy_choice_total") or 0))
    emit("dra_policy_choice_matched_total", int(metrics.get("policy_choice_matched_total") or 0))
    emit("dra_policy_stagnation_total", int(metrics.get("policy_stagnation_total") or 0))
    emit("dra_policy_query_vector_total", int(metrics.get("policy_query_vector_total") or 0))
    emit("dra_policy_domain_shift_total", int(metrics.get("policy_domain_shift_total") or 0))
    emit("dra_policy_conclusion_ready_total", int(metrics.get("policy_conclusion_ready_total") or 0))
    emit("dra_policy_source_budget_total", int(metrics.get("policy_source_budget_total") or 0))
    emit("dra_policy_brave_budget_total", int(metrics.get("policy_brave_budget_total") or 0))
    emit("dra_policy_brave_circuit_total", int(metrics.get("policy_brave_circuit_total") or 0))
    for score, c in sorted((metrics.get("verifier_scores_total") or {}).items()):
        emit("dra_verifier_scores_total", c, {"score": score})
    last_score = metrics.get("verifier_last_score")
    if isinstance(last_score, int):
        emit("dra_verifier_last_score", last_score)
    emit("dra_verifier_duration_seconds_sum", float(metrics.get("verifier_duration_s_sum") or 0.0))
    emit("dra_verifier_duration_seconds_count", int(metrics.get("verifier_duration_s_count") or 0))
    emit("dra_verifier_model_calls_total", int(metrics.get("verifier_model_calls_total") or 0))
    emit("dra_verifier_model_latency_seconds_sum", float(metrics.get("verifier_model_latency_s_sum") or 0.0))
    emit("dra_verifier_model_tokens_total", int(metrics.get("verifier_model_tokens_total") or 0))
    emit("dra_verifier_tool_calls_total", int(metrics.get("verifier_tool_calls_total") or 0))
    emit("dra_verifier_tool_errors_total", int(metrics.get("verifier_tool_errors_total") or 0))
    emit("dra_verifier_instruction_chars_sum", int(metrics.get("verifier_instruction_chars_sum") or 0))
    emit("dra_verifier_instruction_count_sum", int(metrics.get("verifier_instruction_count_sum") or 0))
    emit("dra_verifier_instruction_has_url_total", int(metrics.get("verifier_instruction_has_url_total") or 0))
    emit("dra_verifier_instruction_has_path_total", int(metrics.get("verifier_instruction_has_path_total") or 0))
    emit("dra_verifier_instruction_has_cmd_total", int(metrics.get("verifier_instruction_has_cmd_total") or 0))
    emit("dra_verifier_before_tools_total", int(metrics.get("verifier_before_tools_total") or 0))
    emit("dra_verifier_gradient_total", int(metrics.get("verifier_gradient_total") or 0))
    emit("dra_model_calls_total", int(metrics.get("model_calls_total") or 0))
    for scope, c in sorted((metrics.get("model_calls_by_scope") or {}).items()):
        emit("dra_model_calls_total", c, {"scope": scope})
    for scope, s in sorted((metrics.get("model_latency_s_sum_by_scope") or {}).items()):
        emit("dra_model_latency_seconds_sum", float(s), {"scope": scope})
    for scope, tok in sorted((metrics.get("model_tokens_total_by_scope") or {}).items()):
        emit("dra_model_tokens_total", int(tok), {"scope": scope})
    emit("dra_model_finish_reason_length_total", int(metrics.get("model_finish_reason_length_total") or 0))
    emit("dra_max_step", metrics.get("max_step", 0))
    emit("dra_last_event_ts", metrics.get("last_ts", 0.0))
    return "\n".join(lines) + "\n"


class TraceState:
    """
    Monotonic metric aggregation by tailing trace.jsonl and keeping a cursor.
    This avoids /metrics values "resetting" when only the last N lines are scanned.
    """

    def __init__(self, trace_path: Path):
        self.trace_path = trace_path
        self.offset = 0
        self.lock = threading.Lock()
        self.metrics = {
            "events_total": 0,
            "events_by_type": {},
            "tool_calls_total": 0,
            "tool_calls_by_tool": {},
            "tool_calls_by_scope": {},
            "tool_errors_total": 0,
            "policy_pre_tool_nudge_total": 0,
            "policy_length_nudge_total": 0,
            "policy_reminder_total": 0,
            "policy_choice_total": 0,
            "policy_choice_matched_total": 0,
            "policy_stagnation_total": 0,
            "policy_query_vector_total": 0,
            "policy_domain_shift_total": 0,
            "policy_conclusion_ready_total": 0,
            "policy_source_budget_total": 0,
            "policy_brave_budget_total": 0,
            "policy_brave_circuit_total": 0,
            "verifier_scores_total": {1: 0, 2: 0, 3: 0, 4: 0},
            "verifier_last_score": None,
            "verifier_duration_s_sum": 0.0,
            "verifier_duration_s_count": 0,
            "verifier_model_calls_total": 0,
            "verifier_model_latency_s_sum": 0.0,
            "verifier_model_tokens_total": 0,
            "verifier_tool_calls_total": 0,
            "verifier_tool_errors_total": 0,
            "verifier_instruction_chars_sum": 0,
            "verifier_instruction_count_sum": 0,
            "verifier_instruction_has_url_total": 0,
            "verifier_instruction_has_path_total": 0,
            "verifier_instruction_has_cmd_total": 0,
            "verifier_before_tools_total": 0,
            "verifier_gradient_total": 0,
            "model_calls_total": 0,
            "model_calls_by_scope": {},
            "model_latency_s_sum_by_scope": {},
            "model_tokens_total_by_scope": {},
            "model_finish_reason_length_total": 0,
            "max_step": 0,
            "last_ts": 0.0,
        }

    def update(self) -> None:
        with self.lock:
            try:
                with self.trace_path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(self.offset, os.SEEK_SET)
                    while True:
                        line = f.readline()
                        if not line:
                            break
                        self.offset = f.tell()
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(ev, dict):
                            self._ingest(ev)
            except FileNotFoundError:
                return
            except Exception:
                return

    def _ingest(self, ev: dict) -> None:
        m = self.metrics
        m["events_total"] += 1
        t = ev.get("type", "unknown")
        m["events_by_type"][t] = m["events_by_type"].get(t, 0) + 1
        step = int(ev.get("step") or 0)
        m["max_step"] = max(m["max_step"], step)
        ts = float(ev.get("ts") or 0.0)
        m["last_ts"] = max(m["last_ts"], ts)

        if t == "policy_pre_tool_nudge":
            m["policy_pre_tool_nudge_total"] += 1
        if t == "policy_length_nudge":
            m["policy_length_nudge_total"] += 1
        if t == "policy_reminder":
            m["policy_reminder_total"] += 1
        if t == "policy_choice":
            m["policy_choice_total"] += 1
            if ev.get("matched") is True:
                m["policy_choice_matched_total"] += 1
        if t == "policy_stagnation":
            m["policy_stagnation_total"] += 1
        if t == "policy_query_vector":
            m["policy_query_vector_total"] += 1
        if t == "policy_domain_shift":
            m["policy_domain_shift_total"] += 1
        if t == "policy_conclusion_ready":
            m["policy_conclusion_ready_total"] += 1
        if t == "policy_source_budget":
            m["policy_source_budget_total"] += 1
        if t == "policy_brave_budget":
            m["policy_brave_budget_total"] += 1
        if t == "policy_brave_circuit":
            m["policy_brave_circuit_total"] += 1
        if t == "verifier_gradient":
            m["verifier_gradient_total"] += 1

        if t == "tool":
            m["tool_calls_total"] += 1
            tool = ev.get("tool", "unknown")
            m["tool_calls_by_tool"][tool] = m["tool_calls_by_tool"].get(tool, 0) + 1
            scope = ev.get("scope", "agent")
            m["tool_calls_by_scope"][scope] = m["tool_calls_by_scope"].get(scope, 0) + 1
            obs = ev.get("obs") or {}
            if isinstance(obs, dict):
                exit_code = obs.get("exit_code")
                if (isinstance(exit_code, int) and exit_code != 0) or obs.get("error"):
                    m["tool_errors_total"] += 1

        if t == "model":
            m["model_calls_total"] += 1
            scope = ev.get("scope", "unknown")
            m["model_calls_by_scope"][scope] = m["model_calls_by_scope"].get(scope, 0) + 1
            m["model_latency_s_sum_by_scope"][scope] = m["model_latency_s_sum_by_scope"].get(scope, 0.0) + float(
                ev.get("latency_s") or 0.0
            )
            if ev.get("finish_reason") == "length":
                m["model_finish_reason_length_total"] += 1
            usage = ev.get("usage") or {}
            if isinstance(usage, dict):
                total_tokens = usage.get("total_tokens")
                if isinstance(total_tokens, int):
                    m["model_tokens_total_by_scope"][scope] = m["model_tokens_total_by_scope"].get(scope, 0) + total_tokens

        if t == "verifier":
            if (m.get("tool_calls_total") or 0) == 0:
                m["verifier_before_tools_total"] += 1
            d = ev.get("decision") or {}
            if isinstance(d, dict):
                score = d.get("score")
                if isinstance(score, int) and score in m["verifier_scores_total"]:
                    m["verifier_scores_total"][score] += 1
                    m["verifier_last_score"] = score
                meta = d.get("meta") or {}
                if isinstance(meta, dict):
                    dur = meta.get("duration_s")
                    if isinstance(dur, (int, float)) and dur >= 0:
                        m["verifier_duration_s_sum"] += float(dur)
                        m["verifier_duration_s_count"] += 1
                    vmc = meta.get("verifier_model_calls")
                    if isinstance(vmc, int) and vmc >= 0:
                        m["verifier_model_calls_total"] += vmc
                    vml = meta.get("verifier_model_latency_s")
                    if isinstance(vml, (int, float)) and vml >= 0:
                        m["verifier_model_latency_s_sum"] += float(vml)
                    vu = meta.get("verifier_usage") or {}
                    if isinstance(vu, dict):
                        tt = vu.get("total_tokens")
                        if isinstance(tt, int) and tt >= 0:
                            m["verifier_model_tokens_total"] += tt
                    vtc = meta.get("verifier_tool_calls")
                    if isinstance(vtc, int) and vtc >= 0:
                        m["verifier_tool_calls_total"] += vtc
                    vte = meta.get("verifier_tool_errors")
                    if isinstance(vte, int) and vte >= 0:
                        m["verifier_tool_errors_total"] += vte
                    ic = meta.get("instruction_count")
                    if isinstance(ic, int) and ic >= 0:
                        m["verifier_instruction_count_sum"] += ic
                    ich = meta.get("instruction_chars")
                    if isinstance(ich, int) and ich >= 0:
                        m["verifier_instruction_chars_sum"] += ich
                    if meta.get("instruction_has_url"):
                        m["verifier_instruction_has_url_total"] += 1
                    if meta.get("instruction_has_path"):
                        m["verifier_instruction_has_path_total"] += 1
                    if meta.get("instruction_has_cmd"):
                        m["verifier_instruction_has_cmd_total"] += 1

    def snapshot(self) -> dict:
        with self.lock:
            snap = json.loads(json.dumps(self.metrics))
            try:
                snap["trace_exists"] = self.trace_path.exists()
                snap["trace_size_bytes"] = self.trace_path.stat().st_size if snap["trace_exists"] else 0
            except Exception:
                snap["trace_exists"] = False
                snap["trace_size_bytes"] = 0
            return snap


class Handler(BaseHTTPRequestHandler):
    server_version = "sandbox-agent-dashboard/0.1"

    def _send(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        work_dir = (qs.get("work_dir") or [""])[0]

        if parsed.path == "/":
            self._send(200, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8"))
            return

        if parsed.path == "/sessions":
            base: Path = self.server.base_dir  # type: ignore[attr-defined]
            sessions = list_sessions(base)
            self._send(
                200,
                "application/json; charset=utf-8",
                json.dumps({"sessions": sessions}, ensure_ascii=False).encode("utf-8"),
            )
            return

        if parsed.path == "/metrics":
            if not work_dir:
                self._send(400, "text/plain; charset=utf-8", b"Missing work_dir\n")
                return
            base: Path = self.server.base_dir  # type: ignore[attr-defined]
            work = (base / work_dir).resolve() if not os.path.isabs(work_dir) else Path(work_dir).resolve()
            if base not in work.parents and work != base:
                self._send(403, "text/plain; charset=utf-8", b"Denied\n")
                return
            trace_path = work / "trace.jsonl"
            cache = self.server.trace_cache  # type: ignore[attr-defined]
            state = cache.get(str(trace_path))
            if state is None:
                state = TraceState(trace_path)
                cache[str(trace_path)] = state
            state.update()
            body = render_prometheus(state.snapshot()).encode("utf-8")
            self._send(200, "text/plain; version=0.0.4; charset=utf-8", body)
            return

        if parsed.path == "/metrics_json":
            if not work_dir:
                self._send(400, "application/json; charset=utf-8", json.dumps({"error": "Missing work_dir"}).encode("utf-8"))
                return
            base: Path = self.server.base_dir  # type: ignore[attr-defined]
            work = (base / work_dir).resolve() if not os.path.isabs(work_dir) else Path(work_dir).resolve()
            if base not in work.parents and work != base:
                self._send(403, "application/json; charset=utf-8", json.dumps({"error": "Denied"}).encode("utf-8"))
                return
            trace_path = work / "trace.jsonl"
            cache = self.server.trace_cache  # type: ignore[attr-defined]
            state = cache.get(str(trace_path))
            if state is None:
                state = TraceState(trace_path)
                cache[str(trace_path)] = state
            state.update()
            self._send(
                200,
                "application/json; charset=utf-8",
                json.dumps(state.snapshot(), ensure_ascii=False).encode("utf-8"),
            )
            return

        if parsed.path == "/run_status":
            if not work_dir:
                self._send(400, "application/json; charset=utf-8", json.dumps({"error": "Missing work_dir"}).encode("utf-8"))
                return
            base: Path = self.server.base_dir  # type: ignore[attr-defined]
            work = (base / work_dir).resolve() if not os.path.isabs(work_dir) else Path(work_dir).resolve()
            if base not in work.parents and work != base:
                self._send(403, "application/json; charset=utf-8", json.dumps({"error": "Denied"}).encode("utf-8"))
                return
            runs = self.server.runs  # type: ignore[attr-defined]
            rec = runs.get(str(work))
            if not rec:
                self._send(200, "application/json; charset=utf-8", json.dumps({"status": "unknown"}).encode("utf-8"))
                return
            pid = rec.get("pid")
            running = False
            if isinstance(pid, int):
                try:
                    os.kill(pid, 0)
                    running = True
                except Exception:
                    running = False
            self._send(
                200,
                "application/json; charset=utf-8",
                json.dumps({"status": "running" if running else "exited", "pid": pid}).encode("utf-8"),
            )
            return

        if parsed.path == "/notes":
            if not work_dir:
                self._send(400, "text/plain; charset=utf-8", b"Missing work_dir\n")
                return
            base: Path = self.server.base_dir  # type: ignore[attr-defined]
            work = (base / work_dir).resolve() if not os.path.isabs(work_dir) else Path(work_dir).resolve()
            if base not in work.parents and work != base:
                self._send(403, "text/plain; charset=utf-8", b"Denied\n")
                return
            notes_path = work / "notes.md"
            if not notes_path.exists():
                self._send(200, "text/plain; charset=utf-8", b"")
                return
            try:
                data = notes_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                data = ""
            self._send(200, "text/plain; charset=utf-8", data.encode("utf-8")[:200000])
            return

        if parsed.path == "/evidence":
            if not work_dir:
                self._send(400, "text/plain; charset=utf-8", b"Missing work_dir\n")
                return
            base: Path = self.server.base_dir  # type: ignore[attr-defined]
            work = (base / work_dir).resolve() if not os.path.isabs(work_dir) else Path(work_dir).resolve()
            if base not in work.parents and work != base:
                self._send(403, "text/plain; charset=utf-8", b"Denied\n")
                return
            ledger_path = work / "evidence.jsonl"
            if not ledger_path.exists():
                self._send(200, "text/plain; charset=utf-8", b"")
                return
            try:
                data = ledger_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                data = ""
            self._send(200, "text/plain; charset=utf-8", data.encode("utf-8")[:200000])
            return

        if parsed.path == "/move_ledger":
            if not work_dir:
                self._send(400, "text/plain; charset=utf-8", b"Missing work_dir\n")
                return
            base: Path = self.server.base_dir  # type: ignore[attr-defined]
            work = (base / work_dir).resolve() if not os.path.isabs(work_dir) else Path(work_dir).resolve()
            if base not in work.parents and work != base:
                self._send(403, "text/plain; charset=utf-8", b"Denied\n")
                return
            ledger_path = work / "move_ledger.jsonl"
            if not ledger_path.exists():
                self._send(200, "text/plain; charset=utf-8", b"")
                return
            try:
                data = ledger_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                data = ""
            self._send(200, "text/plain; charset=utf-8", data.encode("utf-8")[:200000])
            return

        if parsed.path == "/query_ledger":
            if not work_dir:
                self._send(400, "text/plain; charset=utf-8", b"Missing work_dir\n")
                return
            base: Path = self.server.base_dir  # type: ignore[attr-defined]
            work = (base / work_dir).resolve() if not os.path.isabs(work_dir) else Path(work_dir).resolve()
            if base not in work.parents and work != base:
                self._send(403, "text/plain; charset=utf-8", b"Denied\n")
                return
            ledger_path = work / "query_ledger.jsonl"
            if not ledger_path.exists():
                self._send(200, "text/plain; charset=utf-8", b"")
                return
            try:
                data = ledger_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                data = ""
            self._send(200, "text/plain; charset=utf-8", data.encode("utf-8")[:200000])
            return

        if parsed.path == "/container_log":
            if not work_dir:
                self._send(400, "text/plain; charset=utf-8", b"Missing work_dir\n")
                return
            base: Path = self.server.base_dir  # type: ignore[attr-defined]
            work = (base / work_dir).resolve() if not os.path.isabs(work_dir) else Path(work_dir).resolve()
            if base not in work.parents and work != base:
                self._send(403, "text/plain; charset=utf-8", b"Denied\n")
                return
            log_path = work / "container.log"
            if not log_path.exists():
                self._send(200, "text/plain; charset=utf-8", b"")
                return
            try:
                data = log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                data = ""
            self._send(200, "text/plain; charset=utf-8", data.encode("utf-8")[:200000])
            return

        if parsed.path == "/container_events":
            if not work_dir:
                self._send(400, "text/plain; charset=utf-8", b"Missing work_dir\n")
                return
            base: Path = self.server.base_dir  # type: ignore[attr-defined]
            work = (base / work_dir).resolve() if not os.path.isabs(work_dir) else Path(work_dir).resolve()
            if base not in work.parents and work != base:
                self._send(403, "text/plain; charset=utf-8", b"Denied\n")
                return
            log_path = work / "container_events.log"
            if not log_path.exists():
                self._send(200, "text/plain; charset=utf-8", b"")
                return
            try:
                data = log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                data = ""
            self._send(200, "text/plain; charset=utf-8", data.encode("utf-8")[:200000])
            return

        if parsed.path == "/events":
            if not work_dir:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Missing work_dir\n")
                return
            base: Path = self.server.base_dir  # type: ignore[attr-defined]
            work = (base / work_dir).resolve() if not os.path.isabs(work_dir) else Path(work_dir).resolve()
            if base not in work.parents and work != base:
                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Denied\n")
                return
            trace_path = work / "trace.jsonl"

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            # Send an initial message so the browser marks the stream as "open".
            self.wfile.write(
                ("data: " + json.dumps({"type": "sse", "status": "connected", "ts": time.time()}) + "\n\n").encode("utf-8")
            )

            # Bootstrap with last events.
            for raw in read_last_lines(trace_path, n=120):
                try:
                    json.loads(raw)
                except Exception:
                    continue
                self.wfile.write(f"data: {raw}\n\n".encode("utf-8"))
            self.wfile.flush()

            # Follow (keep connection open even if the trace file doesn't exist yet).
            try:
                last_heartbeat = 0.0
                while True:
                    if not trace_path.exists():
                        now = time.time()
                        if now - last_heartbeat > 2.0:
                            self.wfile.write(
                                ("data: " + json.dumps({"type": "heartbeat", "ts": now, "status": "waiting_for_trace"}) + "\n\n").encode(
                                    "utf-8"
                                )
                            )
                            self.wfile.flush()
                            last_heartbeat = now
                        time.sleep(0.25)
                        continue
                    with trace_path.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(0, os.SEEK_END)
                        while True:
                            line = f.readline()
                            if not line:
                                time.sleep(0.25)
                                now = time.time()
                                if now - last_heartbeat > 5.0:
                                    self.wfile.write(
                                        ("data: " + json.dumps({"type": "heartbeat", "ts": now}) + "\n\n").encode("utf-8")
                                    )
                                    self.wfile.flush()
                                    last_heartbeat = now
                                # If file disappeared/rotated, break to reopen.
                                if not trace_path.exists():
                                    break
                                continue
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                json.loads(line)
                            except Exception:
                                continue
                            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                            self.wfile.flush()
            except BrokenPipeError:
                return
            except Exception:
                return

        self._send(404, "text/plain; charset=utf-8", b"Not found\n")

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/new_session":
            base: Path = self.server.base_dir  # type: ignore[attr-defined]
            (base / "work").mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            suffix = f"{random.randint(1000, 9999)}"
            work_dir = f"./work/ui-run-{stamp}-{suffix}"
            work = (base / work_dir).resolve()
            if base not in work.parents and work != base:
                self._send(500, "application/json; charset=utf-8", json.dumps({"error": "Failed to create session"}).encode("utf-8"))
                return
            work.mkdir(parents=True, exist_ok=True)
            append_session_log(work, {"type": "new_session", "work_dir": work_dir})
            self._send(200, "application/json; charset=utf-8", json.dumps({"work_dir": work_dir}).encode("utf-8"))
            return

        if parsed.path != "/start_run":
            self._send(404, "application/json; charset=utf-8", json.dumps({"error": "Not found"}).encode("utf-8"))
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except Exception:
            length = 0
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            payload = {}

        task = str((payload or {}).get("task", "")).strip()
        if not task:
            self._send(400, "application/json; charset=utf-8", json.dumps({"error": "Missing task"}).encode("utf-8"))
            return

        work_dir = (payload or {}).get("work_dir")
        base: Path = self.server.base_dir  # type: ignore[attr-defined]
        if work_dir is None or str(work_dir).strip() == "":
            stamp = time.strftime("%Y%m%d-%H%M%S")
            work_dir = f"./work/ui-run-{stamp}"
        work_dir = str(work_dir)
        work = (base / work_dir).resolve() if not os.path.isabs(work_dir) else Path(work_dir).resolve()
        if base not in work.parents and work != base:
            self._send(403, "application/json; charset=utf-8", json.dumps({"error": "Denied work_dir"}).encode("utf-8"))
            return
        work.mkdir(parents=True, exist_ok=True)

        model_base_url = (payload or {}).get("model_base_url") or os.getenv("MODEL_BASE_URL")
        model_name = (payload or {}).get("model_name") or os.getenv("MODEL_NAME", "")
        if not model_base_url:
            self._send(
                400,
                "application/json; charset=utf-8",
                json.dumps(
                    {
                        "error": "Missing model_base_url. Provide it in the UI (defaults to http://127.0.0.1:1234) or set MODEL_BASE_URL."
                    }
                ).encode("utf-8"),
            )
            return

        brave_api_key = (payload or {}).get("brave_api_key") or os.getenv("BRAVE_API_KEY", "")
        max_steps_raw = (payload or {}).get("max_steps")
        if max_steps_raw is None or str(max_steps_raw).strip() == "":
            max_steps_raw = os.getenv("MAX_STEPS", "")
        max_steps_val = None
        try:
            if str(max_steps_raw).strip() != "":
                max_steps_val = int(str(max_steps_raw).strip())
        except Exception:
            max_steps_val = None

        cmd = [
            "python3",
            "run.py",
            "run",
            "--task",
            task,
            "--work-dir",
            str(work),
            "--model-base-url",
            str(model_base_url),
        ]
        if max_steps_val is not None:
            cmd.extend(["--max-steps", str(max_steps_val)])
        if str(model_name).strip():
            cmd.extend(["--model-name", str(model_name).strip()])
        if brave_api_key:
            cmd.extend(["--brave-api-key", brave_api_key])

        # Launch detached, with logs in the work_dir for easy debugging.
        log_path = work / "run.log"
        try:
            p = subprocess.Popen(cmd, cwd=str(base), stdout=log_path.open("ab"), stderr=subprocess.STDOUT)
        except Exception as e:
            self._send(500, "application/json; charset=utf-8", json.dumps({"error": str(e)}).encode("utf-8"))
            return

        runs = self.server.runs  # type: ignore[attr-defined]
        runs[str(work)] = {"pid": p.pid, "started_at": time.time(), "work_dir": work_dir}
        try:
            (work / "run.pid").write_text(str(p.pid) + "\n", encoding="utf-8")
        except Exception:
            pass
        append_session_log(
            work,
            {
                "type": "start_run",
                "pid": p.pid,
                "work_dir": work_dir,
                "model_base_url": model_base_url,
                "model_name": str(model_name).strip(),
                "has_brave_api_key": bool(brave_api_key),
                "max_steps": max_steps_val,
                "cmd": cmd,
            },
        )
        self._send(
            200,
            "application/json; charset=utf-8",
            json.dumps({"pid": p.pid, "work_dir": work_dir}).encode("utf-8"),
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-dir", default=".", help="Base dir for resolving relative work_dir paths")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8844)
    args = ap.parse_args()

    base_dir = Path(args.base_dir).resolve()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.base_dir = base_dir  # type: ignore[attr-defined]
    srv.trace_cache = {}  # type: ignore[attr-defined]
    srv.runs = {}  # type: ignore[attr-defined]
    print(f"Dashboard running on http://{args.host}:{args.port} (base_dir={base_dir})")
    srv.serve_forever()


if __name__ == "__main__":
    main()

"""
rule_ui.py — Expert Feedback UI for Cogni Rules
================================================

A lightweight Flask web app where domain experts review, critique,
and refine RTL verification rules. The expert gives natural-language
feedback on any rule; the system uses an LLM to apply that feedback,
shows the diff, and the expert accepts or continues refining.

Usage:
    python rule_ui.py [--port 5050] [--pack packs/rtl/rules.json]

Then open http://localhost:5050 in a browser.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import copy
import datetime

from flask import Flask, request, jsonify, render_template_string

from agent.llm import LLMCall
from agent import llm as _llm
from agent.llm.transports import run_briefs_concurrently
from agent.rule_health import validate_rule, persist_pack
from agent.rule_enhance import find_gaps

app = Flask(__name__)

PACK_PATH = "packs/rtl/rules.json"
PACK: dict = {}
RUN_DIR = "runs/rule_ui"

# ---------------------------------------------------------------------------
# LLM schema for feedback-driven rule update
# ---------------------------------------------------------------------------

FEEDBACK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["updated_rule", "change_summary"],
    "properties": {
        "updated_rule": {
            "type": "object",
            "description": "The complete updated rule JSON with the expert's feedback applied."
        },
        "change_summary": {
            "type": "string",
            "description": "Brief summary of what changed and why, in 1-3 sentences."
        },
        "confidence": {
            "type": "integer",
            "description": "1-10 confidence that the update correctly addresses the feedback."
        }
    }
}


def _apply_feedback_call(rule: dict, feedback: str, pack_context: dict) -> LLMCall:
    prompt = """# Role: RTL Rule Editor

You are an expert RTL design verification engineer. A domain expert has
reviewed a rule and provided feedback. Your job is to update the rule
to incorporate that feedback precisely.

## Guidelines

- Apply the expert's feedback faithfully. They know the domain.
- Preserve fields the expert didn't mention -- don't drop content.
- Keep the rule id unchanged.
- If the expert says the rule is wrong, fix or retire it (set status to "retired").
- If the expert says to strengthen it, increase the strength (low -> medium -> high).
- If the expert says to weaken it, decrease the strength (high -> medium -> low).
- If the expert asks to change the band/prediction, update the predicts[] values.
- If the expert asks to add examples, generate realistic SystemVerilog snippets.
- For prediction channels, use ONLY: "intervals", "enum", "ranking", "includes", "excludes".
- Return the COMPLETE rule object, not just the changed fields.
- Add a history entry: {"event": "expert_feedback", "at": "<ISO timestamp>", "feedback": "<summary>"}.

## Inputs

Read `inputs.json`:
  - `rule`: the current rule (full JSON)
  - `feedback`: the expert's feedback text
  - `key_index`: available measurement keys
"""
    return LLMCall(
        name=f"feedback.{rule['id'][:40]}",
        model=_llm.MODEL_OPUS,
        role="rule_editor",
        prompt=prompt,
        schema=FEEDBACK_SCHEMA,
        inputs={
            "rule": rule,
            "feedback": feedback,
            "key_index": pack_context.get("key_index", {}),
        },
    )


def _run_feedback(rule: dict, feedback: str) -> dict | None:
    """Run the LLM to apply expert feedback to a rule. Returns output dict."""
    call = _apply_feedback_call(rule, feedback, PACK)
    paths = call.write_brief(RUN_DIR)
    brief = {
        "name": call.name, "model": call.model, "role": call.role,
        "prompt": paths["prompt"], "schema": paths["schema"],
        "inputs": paths["inputs"], "output": paths["output"],
    }
    asyncio.run(run_briefs_concurrently([brief], concurrency=1))
    out_path = os.path.join(RUN_DIR, "llm_calls", call.name, "output.json")
    if not os.path.exists(out_path):
        return None
    with open(out_path, encoding="utf-8") as f:
        return json.load(f)


def _load_pack():
    global PACK
    with open(PACK_PATH, encoding="utf-8") as f:
        PACK = json.load(f)


def _save_pack():
    persist_pack(PACK, PACK_PATH)


def _rule_status_class(rule):
    s = rule.get("status", "active")
    if s == "retired":
        return "retired"
    gaps = []
    ex = rule.get("examples", {})
    if not ex.get("violating"):
        gaps.append("no violating example")
    if not ex.get("compliant"):
        gaps.append("no compliant example")
    if not rule.get("predicts"):
        gaps.append("ungradeable")
    if gaps:
        return "incomplete"
    return "complete"


# ---------------------------------------------------------------------------
# HTML template — single-page app
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cogni Rule Expert UI</title>
<style>
:root {
  --bg: #0d1117;
  --surface: #161b22;
  --border: #30363d;
  --text: #e6edf3;
  --text-dim: #8b949e;
  --accent: #58a6ff;
  --green: #3fb950;
  --red: #f85149;
  --orange: #d29922;
  --purple: #bc8cff;
  --font: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  font-size: 14px;
  line-height: 1.5;
}
.layout {
  display: grid;
  grid-template-columns: 340px 1fr;
  height: 100vh;
}

/* --- Sidebar --- */
.sidebar {
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.sidebar-header {
  padding: 16px;
  border-bottom: 1px solid var(--border);
}
.sidebar-header h1 {
  font-size: 16px;
  font-weight: 600;
  color: var(--accent);
}
.sidebar-header .stats {
  font-size: 12px;
  color: var(--text-dim);
  margin-top: 4px;
}
.sidebar-header .stats span { margin-right: 12px; }
.search-box {
  padding: 8px 16px;
  border-bottom: 1px solid var(--border);
}
.search-box input {
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  color: var(--text);
  font-size: 13px;
  outline: none;
}
.search-box input:focus { border-color: var(--accent); }
.filter-bar {
  padding: 6px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.filter-btn {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 2px 10px;
  font-size: 11px;
  color: var(--text-dim);
  cursor: pointer;
}
.filter-btn.active {
  background: var(--accent);
  color: #000;
  border-color: var(--accent);
}
.rule-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}
.rule-item {
  padding: 8px 16px;
  cursor: pointer;
  border-left: 3px solid transparent;
  transition: background 0.1s;
}
.rule-item:hover { background: rgba(88,166,255,0.06); }
.rule-item.selected {
  background: rgba(88,166,255,0.1);
  border-left-color: var(--accent);
}
.rule-item .rule-id {
  font-family: var(--font);
  font-size: 12px;
  color: var(--accent);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rule-item .rule-stmt {
  font-size: 12px;
  color: var(--text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-top: 2px;
}
.rule-item .badges {
  display: flex;
  gap: 4px;
  margin-top: 4px;
}
.badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 8px;
  font-weight: 500;
}
.badge.high { background: rgba(248,81,73,0.2); color: var(--red); }
.badge.medium { background: rgba(210,153,34,0.2); color: var(--orange); }
.badge.low { background: rgba(63,185,80,0.2); color: var(--green); }
.badge.constraint { background: rgba(248,81,73,0.15); color: var(--red); }
.badge.tendency { background: rgba(210,153,34,0.15); color: var(--orange); }
.badge.heuristic { background: rgba(188,140,255,0.15); color: var(--purple); }
.badge.retired { background: rgba(139,148,158,0.2); color: var(--text-dim); }
.badge.incomplete { background: rgba(210,153,34,0.2); color: var(--orange); }
.badge.complete { background: rgba(63,185,80,0.15); color: var(--green); }

/* --- Main panel --- */
.main {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.main-header {
  padding: 16px 24px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.main-header h2 {
  font-size: 15px;
  font-weight: 600;
  font-family: var(--font);
  color: var(--accent);
}
.main-header .actions { display: flex; gap: 8px; }
.btn {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 5px 14px;
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
}
.btn:hover { border-color: var(--accent); color: var(--accent); }
.btn.primary {
  background: var(--accent);
  color: #000;
  border-color: var(--accent);
  font-weight: 600;
}
.btn.primary:hover { opacity: 0.85; }
.btn.danger { border-color: var(--red); color: var(--red); }
.btn.danger:hover { background: var(--red); color: #fff; }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }

#ruleDetail {
  flex-direction: column;
  height: 100%;
}
.main-body {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  min-height: 0;
}

/* Rule detail sections */
.section {
  margin-bottom: 20px;
}
.section-title {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-dim);
  margin-bottom: 6px;
}
.section-content {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
  font-size: 13px;
}
.section-content.code {
  font-family: var(--font);
  font-size: 12px;
  white-space: pre-wrap;
  color: var(--green);
}
.section-content.code.violation { color: var(--red); }
.prediction-row {
  display: grid;
  grid-template-columns: 1fr auto auto auto;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
  font-family: var(--font);
}
.prediction-row:last-child { border-bottom: none; }
.prediction-row .key { color: var(--accent); }
.prediction-row .channel { color: var(--purple); }
.prediction-row .value { color: var(--green); }
.prediction-row .horizon { color: var(--text-dim); }

.prevents-row {
  padding: 6px 0;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}
.prevents-row:last-child { border-bottom: none; }
.prevents-row .stage { color: var(--orange); font-weight: 600; }
.prevents-row .mechanism { color: var(--text-dim); margin-top: 2px; }

.history-entry {
  font-size: 12px;
  padding: 3px 0;
  color: var(--text-dim);
  font-family: var(--font);
}
.history-entry .event { color: var(--accent); }

/* Validation problems */
.problems {
  background: rgba(248,81,73,0.08);
  border: 1px solid rgba(248,81,73,0.3);
  border-radius: 8px;
  padding: 10px 16px;
  margin-bottom: 20px;
}
.problems .problem {
  font-size: 12px;
  color: var(--red);
  padding: 2px 0;
}

/* --- Feedback panel --- */
.feedback-panel {
  border-top: 2px solid var(--accent);
  padding: 20px 24px;
  background: var(--surface);
  min-height: 220px;
}
.feedback-panel h3 {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
  color: var(--accent);
  display: flex;
  align-items: center;
  gap: 8px;
}
.feedback-panel h3::before {
  content: '';
  display: inline-block;
  width: 8px; height: 8px;
  background: var(--accent);
  border-radius: 50%;
}
.quick-actions {
  display: flex;
  gap: 6px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.quick-btn {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 4px 12px;
  font-size: 12px;
  color: var(--text-dim);
  cursor: pointer;
  transition: all 0.15s;
}
.quick-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: rgba(88,166,255,0.06);
}
.feedback-row {
  display: flex;
  gap: 10px;
  align-items: stretch;
}
.feedback-input {
  flex: 1;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
  color: var(--text);
  font-size: 14px;
  font-family: inherit;
  line-height: 1.6;
  resize: vertical;
  outline: none;
  min-height: 100px;
}
.feedback-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px rgba(88,166,255,0.15);
}
.feedback-input::placeholder { color: var(--text-dim); font-size: 13px; }
.submit-col {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 90px;
}
.submit-col .btn {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
}
.feedback-hint {
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 8px;
}
.feedback-hint kbd {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 1px 5px;
  font-size: 10px;
  font-family: var(--font);
}

/* Diff view */
.diff-panel {
  margin-top: 16px;
  display: none;
  border: 1px solid rgba(88,166,255,0.3);
  border-radius: 8px;
  padding: 16px;
  background: rgba(88,166,255,0.04);
}
.diff-panel.visible { display: block; }
.diff-header {
  font-size: 13px;
  font-weight: 600;
  color: var(--accent);
  margin-bottom: 8px;
}
.diff-summary {
  background: rgba(88,166,255,0.08);
  border: 1px solid rgba(88,166,255,0.2);
  border-radius: 6px;
  padding: 10px 14px;
  font-size: 13px;
  color: var(--text);
  margin-bottom: 10px;
  line-height: 1.5;
}
.diff-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

/* Loading spinner */
.spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
  vertical-align: middle;
  margin-right: 6px;
}
@keyframes spin { to { transform: rotate(360deg); } }

.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-dim);
  font-size: 14px;
}

/* Toast notification */
.toast {
  position: fixed;
  top: 20px;
  right: 20px;
  background: var(--green);
  color: #000;
  padding: 10px 20px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  opacity: 0;
  transition: opacity 0.3s;
  z-index: 1000;
}
.toast.show { opacity: 1; }
.toast.error { background: var(--red); color: #fff; }
</style>
</head>
<body>

<div class="layout">
  <!-- Sidebar -->
  <div class="sidebar">
    <div class="sidebar-header">
      <h1>Cogni Rules</h1>
      <div class="stats" id="stats"></div>
    </div>
    <div class="search-box">
      <input type="text" id="search" placeholder="Search rules..." oninput="filterRules()">
    </div>
    <div class="filter-bar" id="filters">
      <button class="filter-btn active" data-filter="all" onclick="setFilter('all',this)">All</button>
      <button class="filter-btn" data-filter="incomplete" onclick="setFilter('incomplete',this)">Incomplete</button>
      <button class="filter-btn" data-filter="complete" onclick="setFilter('complete',this)">Complete</button>
      <button class="filter-btn" data-filter="retired" onclick="setFilter('retired',this)">Retired</button>
      <button class="filter-btn" data-filter="high" onclick="setFilter('high',this)">High</button>
      <button class="filter-btn" data-filter="medium" onclick="setFilter('medium',this)">Medium</button>
    </div>
    <div class="rule-list" id="ruleList"></div>
  </div>

  <!-- Main panel -->
  <div class="main">
    <div id="emptyState" class="empty-state">
      Select a rule to review
    </div>
    <div id="ruleDetail" style="display:none;">
      <div class="main-header">
        <h2 id="ruleTitle"></h2>
        <div class="actions">
          <button class="btn" onclick="refreshRule()">Refresh</button>
          <button class="btn danger" id="retireBtn" onclick="retireRule()">Retire</button>
        </div>
      </div>
      <div class="main-body" id="ruleBody"></div>
      <div class="feedback-panel">
        <h3>Expert Feedback</h3>
        <div class="quick-actions">
          <button class="quick-btn" onclick="quickFeedback('Strengthen this rule to high strength')">Strengthen</button>
          <button class="quick-btn" onclick="quickFeedback('Weaken this rule to low strength')">Weaken</button>
          <button class="quick-btn" onclick="quickFeedback('Tighten the prediction band -- reduce max to 0')">Tighten Band</button>
          <button class="quick-btn" onclick="quickFeedback('Widen the prediction band -- allow more tolerance')">Widen Band</button>
          <button class="quick-btn" onclick="quickFeedback('Add a violating and compliant SystemVerilog example')">Add Examples</button>
          <button class="quick-btn" onclick="quickFeedback('Improve the statement to be more precise and actionable')">Improve Statement</button>
          <button class="quick-btn" onclick="quickFeedback('Add a prevents entry explaining downstream cost')">Add Prevents</button>
          <button class="quick-btn" onclick="retireRule()">Retire</button>
        </div>
        <div class="feedback-row">
          <textarea class="feedback-input" id="feedbackInput"
                    placeholder="Describe what should change about this rule...&#10;&#10;Examples:&#10;  - 'The latch count band is too tight, allow max=2 for designs with intentional latches'&#10;  - 'This rule is wrong -- blocking assignments in always_ff are fine for local variables'&#10;  - 'Add an example showing the async reset case with negedge rst_n'&#10;  - 'Change kind to heuristic, this is a guideline not a hard constraint'"
                    onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();submitFeedback()}"></textarea>
          <div class="submit-col">
            <button class="btn primary" id="submitBtn" onclick="submitFeedback()">Apply Feedback</button>
          </div>
        </div>
        <div class="feedback-hint">
          Press <kbd>Enter</kbd> to submit, <kbd>Shift+Enter</kbd> for new line
        </div>
        <div class="diff-panel" id="diffPanel">
          <div class="diff-header">Proposed Changes</div>
          <div class="diff-summary" id="diffSummary"></div>
          <div class="section-content" id="diffContent" style="font-size:12px;max-height:200px;overflow-y:auto;"></div>
          <div class="diff-actions">
            <button class="btn primary" onclick="acceptChanges()">Accept Changes</button>
            <button class="btn danger" onclick="rejectChanges()">Reject</button>
            <button class="btn" onclick="continueEditing()">Refine Further</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let rules = [];
let selectedId = null;
let currentFilter = 'all';
let pendingUpdate = null;

async function loadRules() {
  const res = await fetch('/api/rules');
  const data = await res.json();
  rules = data.rules;
  renderSidebar();
  document.getElementById('stats').innerHTML =
    `<span>${data.total} rules</span>` +
    `<span>${data.active} active</span>` +
    `<span>${data.gaps} gaps</span>`;
}

function renderSidebar() {
  const q = document.getElementById('search').value.toLowerCase();
  const list = document.getElementById('ruleList');
  let html = '';
  for (const r of rules) {
    if (q && !r.id.toLowerCase().includes(q) && !r.statement.toLowerCase().includes(q)) continue;
    if (currentFilter === 'incomplete' && r.status_class !== 'incomplete') continue;
    if (currentFilter === 'complete' && r.status_class !== 'complete') continue;
    if (currentFilter === 'retired' && r.status !== 'retired') continue;
    if (currentFilter === 'high' && r.strength !== 'high') continue;
    if (currentFilter === 'medium' && r.strength !== 'medium') continue;
    const sel = r.id === selectedId ? 'selected' : '';
    html += `<div class="rule-item ${sel}" onclick="selectRule('${r.id}')">
      <div class="rule-id">${r.id}</div>
      <div class="rule-stmt">${esc(r.statement)}</div>
      <div class="badges">
        <span class="badge ${r.strength}">${r.strength}</span>
        <span class="badge ${r.kind}">${r.kind}</span>
        <span class="badge ${r.status_class}">${r.status_class}</span>
      </div>
    </div>`;
  }
  list.innerHTML = html || '<div style="padding:16px;color:var(--text-dim)">No matching rules</div>';
}

function filterRules() { renderSidebar(); }
function setFilter(f, btn) {
  currentFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderSidebar();
}

async function selectRule(id) {
  selectedId = id;
  pendingUpdate = null;
  document.getElementById('diffPanel').classList.remove('visible');
  renderSidebar();
  const res = await fetch(`/api/rules/${id}`);
  const data = await res.json();
  if (data.error) { toast(data.error, true); return; }
  renderDetail(data.rule, data.problems);
}

function renderDetail(rule, problems) {
  document.getElementById('emptyState').style.display = 'none';
  const rd = document.getElementById('ruleDetail');
  rd.style.display = 'flex';
  rd.style.flexDirection = 'column';
  document.getElementById('ruleTitle').textContent = rule.id;
  document.getElementById('retireBtn').style.display =
    rule.status === 'retired' ? 'none' : '';

  let html = '';

  // Problems
  if (problems && problems.length > 0) {
    html += '<div class="problems">';
    for (const p of problems) html += `<div class="problem">- ${esc(p)}</div>`;
    html += '</div>';
  }

  // Statement
  html += section('Statement', `<div class="section-content">${esc(rule.statement)}</div>`);

  // Metadata row
  html += section('Metadata', `<div class="section-content">
    <strong>Kind:</strong> ${rule.kind} &nbsp; <strong>Strength:</strong> ${rule.strength} &nbsp;
    <strong>Status:</strong> ${rule.status} &nbsp; <strong>Version:</strong> ${rule.version || 1}
  </div>`);

  // Rationale
  if (rule.rationale) {
    html += section('Rationale', `<div class="section-content">${esc(rule.rationale)}</div>`);
  }

  // Predictions
  if (rule.predicts && rule.predicts.length > 0) {
    let phtml = '';
    for (const p of rule.predicts) {
      phtml += `<div class="prediction-row">
        <span class="key">${esc(p.measurement_key || '')}</span>
        <span class="channel">${esc(p.channel || '')}</span>
        <span class="value">${esc(JSON.stringify(p.value || {}))}</span>
        <span class="horizon">${esc(p.horizon || '')}</span>
      </div>`;
    }
    html += section('Predictions', `<div class="section-content">${phtml}</div>`);
  }

  // Prevents
  if (rule.prevents && rule.prevents.length > 0) {
    let pvhtml = '';
    for (const pv of rule.prevents) {
      pvhtml += `<div class="prevents-row">
        <span class="stage">${esc(pv.downstream_stage || '')}</span>
        <span> ${esc(pv.downstream_key || '')}</span>
        <div class="mechanism">${esc(pv.mechanism || '')}</div>
      </div>`;
    }
    html += section('Prevents', `<div class="section-content">${pvhtml}</div>`);
  }

  // Examples
  const ex = rule.examples || {};
  if (ex.violating && ex.violating.length > 0) {
    html += section('Violating Example',
      `<div class="section-content code violation">${esc(ex.violating[0])}</div>`);
  }
  if (ex.compliant && ex.compliant.length > 0) {
    html += section('Compliant Example',
      `<div class="section-content code">${esc(ex.compliant[0])}</div>`);
  }

  // When/Unless
  if (rule.when && rule.when.length > 0) {
    html += section('When (predicates)',
      `<div class="section-content" style="font-family:var(--font);font-size:12px">${esc(JSON.stringify(rule.when, null, 2))}</div>`);
  }

  // Applies To
  if (rule.applies_to) {
    html += section('Applies To',
      `<div class="section-content" style="font-family:var(--font);font-size:12px">${esc(JSON.stringify(rule.applies_to, null, 2))}</div>`);
  }

  // Functional
  if (rule.functional) {
    html += section('Functional Check',
      `<div class="section-content" style="font-family:var(--font);font-size:12px">${esc(JSON.stringify(rule.functional, null, 2))}</div>`);
  }

  // History
  if (rule.history && rule.history.length > 0) {
    let hhtml = '';
    for (const h of rule.history.slice(-10).reverse()) {
      hhtml += `<div class="history-entry">
        <span class="event">${esc(h.event || '')}</span>
        ${h.at ? `<span> ${esc(h.at.substring(0,19))}</span>` : ''}
        ${h.feedback ? `<span> -- ${esc(h.feedback)}</span>` : ''}
        ${h.notes ? `<span> -- ${esc(h.notes)}</span>` : ''}
        ${h.reason ? `<span> -- ${esc(h.reason)}</span>` : ''}
      </div>`;
    }
    html += section('History (recent)', `<div class="section-content">${hhtml}</div>`);
  }

  document.getElementById('ruleBody').innerHTML = html;
}

function section(title, content) {
  return `<div class="section"><div class="section-title">${title}</div>${content}</div>`;
}

function quickFeedback(text) {
  const input = document.getElementById('feedbackInput');
  input.value = text;
  input.focus();
}

async function submitFeedback() {
  const input = document.getElementById('feedbackInput');
  const feedback = input.value.trim();
  if (!feedback || !selectedId) return;

  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Thinking...';

  try {
    const res = await fetch(`/api/rules/${selectedId}/feedback`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({feedback})
    });
    const data = await res.json();
    if (data.error) { toast(data.error, true); return; }

    pendingUpdate = data.updated_rule;
    document.getElementById('diffSummary').textContent = data.change_summary;
    document.getElementById('diffContent').innerHTML = renderDiff(data.changes);
    document.getElementById('diffPanel').classList.add('visible');
  } catch(e) {
    toast('Failed: ' + e.message, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Apply';
  }
}

function renderDiff(changes) {
  if (!changes || changes.length === 0) return '<span style="color:var(--text-dim)">No changes detected</span>';
  let html = '';
  for (const c of changes) {
    const color = c.type === 'added' ? 'var(--green)' :
                  c.type === 'removed' ? 'var(--red)' :
                  c.type === 'changed' ? 'var(--orange)' : 'var(--text)';
    const prefix = c.type === 'added' ? '+' : c.type === 'removed' ? '-' : '~';
    html += `<div style="color:${color};font-family:var(--font);font-size:12px;padding:2px 0">
      ${prefix} <strong>${esc(c.field)}</strong>: ${esc(c.detail)}
    </div>`;
  }
  return html;
}

async function acceptChanges() {
  if (!pendingUpdate || !selectedId) return;
  const res = await fetch(`/api/rules/${selectedId}/accept`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({updated_rule: pendingUpdate})
  });
  const data = await res.json();
  if (data.error) { toast(data.error, true); return; }
  pendingUpdate = null;
  document.getElementById('diffPanel').classList.remove('visible');
  document.getElementById('feedbackInput').value = '';
  toast('Rule updated and saved');
  loadRules();
  selectRule(selectedId);
}

function rejectChanges() {
  pendingUpdate = null;
  document.getElementById('diffPanel').classList.remove('visible');
  toast('Changes rejected');
}

function continueEditing() {
  document.getElementById('diffPanel').classList.remove('visible');
  document.getElementById('feedbackInput').value = '';
  document.getElementById('feedbackInput').focus();
}

async function retireRule() {
  if (!selectedId) return;
  if (!confirm(`Retire rule ${selectedId}?`)) return;
  const res = await fetch(`/api/rules/${selectedId}/feedback`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({feedback: 'Retire this rule. Set status to retired.'})
  });
  const data = await res.json();
  if (data.updated_rule) {
    pendingUpdate = data.updated_rule;
    pendingUpdate.status = 'retired';
    await acceptChanges();
  }
}

function refreshRule() { if (selectedId) selectRule(selectedId); }

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toast(msg, isError) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isError ? ' error' : '');
  setTimeout(() => t.className = 'toast', 2500);
}

loadRules();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/rules")
def api_rules():
    _load_pack()
    rules_out = []
    for r in PACK.get("rules", []):
        rules_out.append({
            "id": r["id"],
            "statement": (r.get("statement") or "")[:120],
            "kind": r.get("kind", ""),
            "strength": r.get("strength", ""),
            "status": r.get("status", "active"),
            "status_class": _rule_status_class(r),
        })
    active = sum(1 for r in rules_out if r["status"] != "retired")
    gaps = len(find_gaps(PACK))
    return jsonify({"rules": rules_out, "total": len(rules_out),
                    "active": active, "gaps": gaps})


@app.route("/api/rules/<rule_id>")
def api_rule_detail(rule_id):
    _load_pack()
    rule = next((r for r in PACK["rules"] if r["id"] == rule_id), None)
    if not rule:
        return jsonify({"error": f"Rule {rule_id} not found"}), 404
    problems = validate_rule(rule, PACK.get("key_index", {}))
    return jsonify({"rule": rule, "problems": problems})


@app.route("/api/rules/<rule_id>/feedback", methods=["POST"])
def api_rule_feedback(rule_id):
    _load_pack()
    rule = next((r for r in PACK["rules"] if r["id"] == rule_id), None)
    if not rule:
        return jsonify({"error": f"Rule {rule_id} not found"}), 404

    data = request.get_json()
    feedback = (data or {}).get("feedback", "").strip()
    if not feedback:
        return jsonify({"error": "No feedback provided"}), 400

    old_rule = copy.deepcopy(rule)
    result = _run_feedback(rule, feedback)
    if not result:
        return jsonify({"error": "LLM call failed"}), 500

    updated = result.get("updated_rule", {})
    summary = result.get("change_summary", "")

    # Compute diff
    changes = _compute_changes(old_rule, updated)

    return jsonify({
        "updated_rule": updated,
        "change_summary": summary,
        "confidence": result.get("confidence", 5),
        "changes": changes,
    })


@app.route("/api/rules/<rule_id>/accept", methods=["POST"])
def api_rule_accept(rule_id):
    _load_pack()
    data = request.get_json()
    updated_rule = (data or {}).get("updated_rule")
    if not updated_rule:
        return jsonify({"error": "No updated_rule in request"}), 400

    # Ensure the id matches
    updated_rule["id"] = rule_id

    # Replace the rule in the pack
    for i, r in enumerate(PACK["rules"]):
        if r["id"] == rule_id:
            PACK["rules"][i] = updated_rule
            break
    else:
        return jsonify({"error": f"Rule {rule_id} not found in pack"}), 404

    _save_pack()
    return jsonify({"ok": True, "message": f"Rule {rule_id} updated and saved"})


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------

def _compute_changes(old: dict, new: dict) -> list[dict]:
    """Compare old and new rule dicts and return a list of changes."""
    changes = []
    all_keys = set(list(old.keys()) + list(new.keys()))
    skip = {"history"}  # history always changes

    for key in sorted(all_keys):
        if key in skip:
            continue
        old_val = old.get(key)
        new_val = new.get(key)

        if old_val == new_val:
            continue
        if old_val is None and new_val is not None:
            changes.append({
                "field": key,
                "type": "added",
                "detail": _summarize_value(new_val),
            })
        elif old_val is not None and new_val is None:
            changes.append({
                "field": key,
                "type": "removed",
                "detail": _summarize_value(old_val),
            })
        else:
            changes.append({
                "field": key,
                "type": "changed",
                "detail": f"{_summarize_value(old_val)} -> {_summarize_value(new_val)}",
            })
    return changes


def _summarize_value(val) -> str:
    if isinstance(val, str):
        return val[:100] + ("..." if len(val) > 100 else "")
    if isinstance(val, list):
        return f"[{len(val)} items]"
    if isinstance(val, dict):
        return json.dumps(val)[:100]
    return str(val)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Cogni Rule Expert UI")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--pack", default="packs/rtl/rules.json")
    ap.add_argument("--ngrok", action="store_true",
                    help="Expose via ngrok tunnel for external access")
    ap.add_argument("--ngrok-token", default=None,
                    help="ngrok auth token (or set NGROK_AUTHTOKEN env var)")
    args = ap.parse_args()

    PACK_PATH = args.pack
    os.makedirs(RUN_DIR, exist_ok=True)
    _load_pack()

    active = len([r for r in PACK["rules"] if r.get("status") != "retired"])
    print(f"\n  Cogni Rule Expert UI")
    print(f"  Pack: {PACK_PATH} ({active} active rules)")

    if args.ngrok:
        try:
            from pyngrok import ngrok, conf

            token = args.ngrok_token or os.environ.get("NGROK_AUTHTOKEN")
            if token:
                ngrok.set_auth_token(token)

            public_url = ngrok.connect(args.port, "http",
                                       bind_tls=True).public_url
            print(f"  Local:  http://localhost:{args.port}")
            print(f"  Public: {public_url}")
            print(f"\n  Share this URL with your team.\n")
        except ImportError:
            print("  [warn] pyngrok not installed. Run: pip install pyngrok")
            print(f"  Open: http://localhost:{args.port}\n")
        except Exception as e:
            print(f"  [warn] ngrok failed: {e}")
            print(f"  Open: http://localhost:{args.port}\n")
    else:
        print(f"  Open: http://localhost:{args.port}")
        print(f"  Tip:  add --ngrok to expose externally\n")

    app.run(host="0.0.0.0", port=args.port, debug=False)

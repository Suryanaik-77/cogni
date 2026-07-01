"""
Cogni Lint Dashboard — UI for RTL editing and pure-Python analysis.

Left panel:   RTL code editor with scenario selector
Right panel:  Cogni Analyzer results — lint findings + synthesis predictions
Run button:   Runs the Cogni RTL analyzer on the current code and shows results.

Usage:
    python lint_ui.py [--port 5001] [--ngrok] [--ngrok-token TOKEN]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile

from flask import Flask, request, jsonify, Response

_REPO = os.path.dirname(os.path.abspath(__file__))
_SCENARIOS_DIR = os.path.join(_REPO, "scenarios")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _list_scenarios():
    """Return list of scenario names that have rtl/ subdirs."""
    out = []
    for name in sorted(os.listdir(_SCENARIOS_DIR)):
        rtl_dir = os.path.join(_SCENARIOS_DIR, name, "rtl")
        if os.path.isdir(rtl_dir):
            files = [f for f in os.listdir(rtl_dir)
                     if f.endswith((".sv", ".v", ".svh"))]
            if files:
                out.append({"name": name, "files": sorted(files)})
    return out


def _read_rtl_file(scenario, filename):
    path = os.path.join(_SCENARIOS_DIR, scenario, "rtl", filename)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _check_rules_against(measurements):
    """Check RTL rules against a measurements dict. Returns list of rule verdicts."""
    pack_path = os.path.join(_REPO, "packs", "rtl", "rules.json")
    with open(pack_path) as f:
        pack = json.load(f)

    results = []
    for rule in pack.get("rules", []):
        if rule.get("status") == "retired":
            continue
        stages = (rule.get("applies_to") or {}).get("stage", [])
        if isinstance(stages, str):
            stages = [stages]
        if "rtl" not in stages:
            continue

        preds = rule.get("predicts", [])
        if not preds:
            results.append({
                "id": rule["id"],
                "statement": rule.get("statement", "")[:100],
                "status": "skipped",
                "details": "no predictions",
            })
            continue

        violations = []
        clean = []
        na = []
        for pred in preds:
            mkey = pred.get("measurement_key", "")
            val = pred.get("value", {})
            measured = measurements.get(mkey)
            if measured is None:
                na.append(mkey)
                continue
            if isinstance(val, dict):
                lo = val.get("min")
                hi = val.get("max")
                if lo is not None and measured < lo:
                    violations.append(f"{mkey}: {measured} below [{lo},{hi}]")
                elif hi is not None and measured > hi:
                    violations.append(f"{mkey}: {measured} above [{lo},{hi}]")
                else:
                    clean.append(mkey)
            else:
                clean.append(mkey)

        if violations:
            results.append({
                "id": rule["id"],
                "statement": rule.get("statement", "")[:100],
                "strength": rule.get("strength", "?"),
                "status": "violation",
                "details": "; ".join(violations),
            })
        elif clean:
            results.append({
                "id": rule["id"],
                "statement": rule.get("statement", "")[:100],
                "strength": rule.get("strength", "?"),
                "status": "clean",
                "details": ", ".join(clean),
            })
        elif na:
            results.append({
                "id": rule["id"],
                "statement": rule.get("statement", "")[:100],
                "status": "n/a",
                "details": "not measured by this tool",
            })

    return results


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/api/scenarios")
def api_scenarios():
    return jsonify(_list_scenarios())


@app.route("/api/rtl/<scenario>/<filename>")
def api_read_rtl(scenario, filename):
    content = _read_rtl_file(scenario, filename)
    return jsonify({"content": content, "scenario": scenario, "file": filename})


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.json or {}
    code = data.get("code", "")
    filename = data.get("filename", "design.sv")
    scenario = data.get("scenario", "")

    # Write code to a temp file for tool execution
    tmp_dir = tempfile.mkdtemp(prefix="cogni_lint_run_")
    tmp_file = os.path.join(tmp_dir, filename)
    with open(tmp_file, "w", encoding="utf-8") as f:
        f.write(code)

    # If scenario has multiple files, copy them too
    if scenario:
        rtl_dir = os.path.join(_SCENARIOS_DIR, scenario, "rtl")
        if os.path.isdir(rtl_dir):
            for fn in os.listdir(rtl_dir):
                if fn != filename and fn.endswith((".sv", ".v", ".svh")):
                    src = os.path.join(rtl_dir, fn)
                    dst = os.path.join(tmp_dir, fn)
                    shutil.copy2(src, dst)

    all_files = [os.path.join(tmp_dir, f)
                 for f in sorted(os.listdir(tmp_dir))
                 if f.endswith((".sv", ".v", ".svh"))]

    # Handle SDC constraint files
    sdc_code = data.get("sdc_code", "")
    sdc_files = []
    if sdc_code:
        sdc_path = os.path.join(tmp_dir, "constraints.sdc")
        with open(sdc_path, "w", encoding="utf-8") as f:
            f.write(sdc_code)
        sdc_files.append(sdc_path)
    if scenario:
        sdc_dir = os.path.join(_SCENARIOS_DIR, scenario, "constraints")
        if os.path.isdir(sdc_dir):
            for fn in os.listdir(sdc_dir):
                if fn.endswith(".sdc"):
                    sdc_files.append(os.path.join(sdc_dir, fn))

    # Handle UPF power-intent files
    upf_code = data.get("upf_code", "")
    upf_files = []
    if upf_code:
        upf_path = os.path.join(tmp_dir, "power.upf")
        with open(upf_path, "w", encoding="utf-8") as f:
            f.write(upf_code)
        upf_files.append(upf_path)
    if scenario:
        upf_dir = os.path.join(_SCENARIOS_DIR, scenario, "upf")
        if os.path.isdir(upf_dir):
            for fn in os.listdir(upf_dir):
                if fn.endswith(".upf"):
                    upf_files.append(os.path.join(upf_dir, fn))

    # Cogni RTL Analyzer (pure Python, no commercial tool)
    from agent.rtl_analyzer import analyze_design
    cogni_result = analyze_design(all_files, sdc_files=sdc_files or None,
                                  upf_files=upf_files or None)
    cogni_data = {
        "findings": [
            {"rule": f.rule, "severity": f.severity, "file": f.file,
             "line": f.line, "message": f.message,
             "synth_impact": f.synth_impact,
             "confidence": f.confidence}
            for f in cogni_result.findings
        ],
        "predictions": [
            {"category": p.category, "prediction": p.prediction,
             "confidence": p.confidence, "detail": p.detail}
            for p in cogni_result.predictions
        ],
        "measurements": cogni_result.measurements,
        "total": len(cogni_result.findings),
    }
    cogni_rules = _check_rules_against(cogni_result.measurements)

    shutil.rmtree(tmp_dir, ignore_errors=True)

    return jsonify({
        "cogni": cogni_data,
        "cogni_rules": cogni_rules,
    })


@app.route("/api/agent_review", methods=["POST"])
def api_agent_review():
    data = request.json or {}
    all_files = data.get("files", {})
    scenario = data.get("scenario", "")

    if not all_files:
        code = data.get("code", "")
        filename = data.get("filename", "design.sv")
        all_files = {filename: code}

    if scenario:
        rtl_dir = os.path.join(_SCENARIOS_DIR, scenario, "rtl")
        if os.path.isdir(rtl_dir):
            for fn in os.listdir(rtl_dir):
                if fn.endswith((".sv", ".v", ".svh")) and fn not in all_files:
                    with open(os.path.join(rtl_dir, fn), encoding="utf-8") as fh:
                        all_files[fn] = fh.read()

    tmp_dir = tempfile.mkdtemp(prefix="cogni_agent_")
    tmp_paths = []
    for fn, code in all_files.items():
        p = os.path.join(tmp_dir, fn)
        with open(p, "w", encoding="utf-8") as f:
            f.write(code)
        tmp_paths.append(p)

    from agent.rtl_analyzer import agent_review_loop, analyze_design

    try:
        static_result = analyze_design(tmp_paths)
        static_findings = [
            {"rule": f.rule, "severity": f.severity,
             "file": f.file, "line": f.line,
             "message": f.message, "synth_impact": f.synth_impact or "",
             "confidence": f.confidence}
            for f in static_result.findings
        ]

        loop_result = agent_review_loop(
            tmp_paths, rtl_sources=all_files, max_iterations=3)
        reviewed = loop_result["result"]
        iters = loop_result["iterations"]

        suggested = reviewed.measurements.get("cogni.agent.suggested_rules", [])
        agent_findings = [
            {"rule": f.rule, "severity": f.severity,
             "file": f.file, "line": f.line,
             "message": f.message, "synth_impact": f.synth_impact or "",
             "confidence": f.confidence}
            for f in reviewed.findings
        ]

        total_fp = sum(it["fp_removed"] for it in iters)
        total_missing = sum(it["missing_found"] for it in iters)
        total_fsm = sum(it["fsm_consequences"] for it in iters)

        waived_rules = {(f["rule"], f["line"]) for f in static_findings} - \
                       {(f["rule"], f["line"]) for f in agent_findings}

        resp = {
            "false_positives": total_fp,
            "missing_bugs": total_missing,
            "fsm_consequences": total_fsm,
            "suggested_rules": suggested,
            "static_findings": static_findings,
            "agent_findings": agent_findings,
            "waived_findings": [f for f in static_findings
                                if (f["rule"], f["line"]) in waived_rules],
            "waivers_total": iters[-1]["total_waivers"] if iters else 0,
            "learned_rules_total": iters[-1]["total_rules"] if iters else 0,
            "iterations": iters,
            "converged": loop_result["converged"],
            "total_iterations": loop_result["total_iterations"],
        }
    except Exception as e:
        resp = {"error": str(e), "agent_findings": [], "static_findings": []}

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return jsonify(resp)


@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.json or {}
    code = data.get("code", "")
    scenario = data.get("scenario", "")
    filename = data.get("filename", "")
    if not scenario or not filename:
        return jsonify({"error": "missing scenario or filename"}), 400
    path = os.path.join(_SCENARIOS_DIR, scenario, "rtl", filename)
    if not os.path.exists(path):
        return jsonify({"error": "file not found"}), 404
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    return jsonify({"saved": True, "path": path})


# ---------------------------------------------------------------------------
# Main HTML
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return Response(_HTML, content_type="text/html")


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cogni Lint Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  background: #0d1117; color: #c9d1d9;
  height: 100vh; display: flex; flex-direction: column;
}

/* Top bar */
.topbar {
  background: #161b22; border-bottom: 1px solid #30363d;
  padding: 8px 16px; display: flex; align-items: center; gap: 12px;
  flex-shrink: 0;
}
.topbar h1 { font-size: 15px; color: #58a6ff; font-weight: 600; }
.topbar select, .topbar button {
  background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
  border-radius: 6px; padding: 6px 12px; font-size: 13px;
  font-family: inherit; cursor: pointer;
}
.topbar select:hover, .topbar button:hover { border-color: #58a6ff; }
.topbar .run-btn {
  background: #238636; color: #fff; font-weight: 600;
  padding: 6px 20px;
}
.topbar .run-btn:hover { background: #2ea043; }
.topbar .run-btn:disabled { opacity: 0.5; cursor: wait; }
.topbar .save-btn { background: #1f6feb; color: #fff; }
.topbar .save-btn:hover { background: #388bfd; }
.topbar .status { font-size: 12px; color: #8b949e; margin-left: auto; }

/* Three panels */
.panels {
  display: flex; flex: 1; min-height: 0; overflow: hidden;
}
.panel {
  flex: 1; display: flex; flex-direction: column;
  border-right: 1px solid #30363d; min-width: 0;
}
.panel:last-child { border-right: none; }
.panel-header {
  background: #161b22; padding: 8px 12px;
  border-bottom: 1px solid #30363d;
  font-size: 13px; font-weight: 600;
  display: flex; align-items: center; gap: 8px;
  flex-shrink: 0;
}
.panel-header .tool-icon {
  width: 8px; height: 8px; border-radius: 50%; display: inline-block;
}
.panel-header .badge {
  background: #30363d; padding: 2px 8px; border-radius: 10px;
  font-size: 11px; font-weight: 400; color: #8b949e;
}
.badge-error { background: #da3633 !important; color: #fff !important; }
.badge-clean { background: #238636 !important; color: #fff !important; }
.panel-body { flex: 1; overflow-y: auto; min-height: 0; }

/* Code editor with line numbers */
.editor-wrap {
  display: flex; height: 100%; position: relative;
  overflow: hidden; background: #0d1117;
}
.line-gutter {
  flex-shrink: 0; padding: 12px 0; text-align: right;
  color: #484f58; font-size: 13px; line-height: 1.5;
  user-select: none; background: #0d1117;
  border-right: 1px solid #21262d; min-width: 48px;
  overflow: hidden;
}
.line-gutter .line-num {
  display: block; padding: 0 10px 0 10px;
}
.line-gutter .line-num.line-error {
  background: rgba(218,54,51,0.25); color: #ff7b72;
  font-weight: 700; cursor: pointer; position: relative;
}
.line-gutter .line-num.line-warning {
  background: rgba(210,153,34,0.20); color: #d29922;
  font-weight: 700; cursor: pointer;
}
.line-gutter .line-num.line-info {
  background: rgba(56,139,253,0.15); color: #58a6ff;
  cursor: pointer;
}
.line-gutter .line-num.line-highlight {
  background: rgba(88,166,255,0.25) !important;
  color: #fff !important;
}
.code-editor {
  flex: 1; min-height: 100%; background: #0d1117;
  color: #c9d1d9; border: none; outline: none; resize: none;
  padding: 12px 12px 12px 12px;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px; line-height: 1.5; tab-size: 4;
  white-space: pre; overflow-y: auto; overflow-x: auto;
}

/* File tabs */
.file-tabs {
  display: flex; background: #161b22;
  border-bottom: 1px solid #30363d; flex-shrink: 0;
  overflow-x: auto;
}
.file-tab {
  padding: 6px 14px; font-size: 12px; cursor: pointer;
  border-right: 1px solid #30363d; color: #8b949e;
  white-space: nowrap;
}
.file-tab:hover { color: #c9d1d9; background: #1c2128; }
.file-tab.active {
  color: #c9d1d9; background: #0d1117;
  border-bottom: 2px solid #58a6ff;
}

/* Results */
.result-section {
  padding: 10px 12px; border-bottom: 1px solid #21262d;
}
.result-section h3 {
  font-size: 12px; color: #8b949e; margin-bottom: 6px;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.warning-item {
  padding: 6px 8px; margin: 3px 0; border-radius: 4px;
  font-size: 12px; line-height: 1.4;
  background: #1c1e26;
}
.warning-item .cat {
  color: #d29922; font-weight: 600;
}
.warning-item .loc {
  color: #8b949e; font-size: 11px;
}
.warning-item .msg { color: #c9d1d9; }
.warning-item.finding-item:hover {
  background: #22272e; outline: 1px solid #30363d;
}
.warning-item .loc strong { color: #ff7b72; }

/* Rule results */
.rule-item {
  padding: 6px 8px; margin: 3px 0; border-radius: 4px;
  font-size: 12px; line-height: 1.4;
  display: flex; align-items: flex-start; gap: 8px;
}
.rule-item.violation {
  background: #2d1215; border-left: 3px solid #da3633;
}
.rule-item.clean {
  background: #122117; border-left: 3px solid #238636;
}
.rule-item.skipped {
  background: #1c1e26; border-left: 3px solid #484f58;
}
.rule-status {
  font-size: 10px; font-weight: 700; padding: 1px 6px;
  border-radius: 3px; flex-shrink: 0; min-width: 50px;
  text-align: center; text-transform: uppercase;
}
.violation .rule-status { background: #da3633; color: #fff; }
.clean .rule-status { background: #238636; color: #fff; }
.skipped .rule-status { background: #484f58; color: #c9d1d9; }
.rule-info { flex: 1; min-width: 0; }
.rule-id { color: #58a6ff; font-weight: 600; font-size: 11px; }
.rule-stmt { color: #8b949e; font-size: 11px; margin-top: 2px; }
.rule-detail { color: #d29922; font-size: 11px; margin-top: 2px; }

/* Raw output toggle */
.raw-toggle {
  background: none; border: 1px solid #30363d; color: #8b949e;
  padding: 3px 10px; border-radius: 4px; font-size: 11px;
  cursor: pointer; margin-top: 6px;
}
.raw-toggle:hover { color: #c9d1d9; border-color: #58a6ff; }
.raw-output {
  display: none; margin-top: 6px; padding: 8px;
  background: #161b22; border-radius: 4px;
  font-size: 11px; white-space: pre-wrap; word-break: break-all;
  max-height: 300px; overflow-y: auto; color: #8b949e;
}

/* Summary bar */
.summary-bar {
  display: flex; gap: 12px; padding: 8px 12px;
  background: #161b22; border-bottom: 1px solid #30363d;
  flex-shrink: 0;
}
.summary-stat {
  font-size: 12px; display: flex; align-items: center; gap: 4px;
}
.summary-stat .dot {
  width: 8px; height: 8px; border-radius: 50%; display: inline-block;
}
.dot-red { background: #da3633; }
.dot-green { background: #238636; }
.dot-yellow { background: #d29922; }
.dot-gray { background: #484f58; }

/* Synthesis predictions */
.prediction-item {
  padding: 8px 10px; margin: 3px 0; border-radius: 4px;
  font-size: 12px; line-height: 1.4;
  background: #1c1e26; border-left: 3px solid #a371f7;
}
.prediction-item.pred-high { border-left-color: #da3633; }
.prediction-item.pred-medium { border-left-color: #d29922; }
.prediction-item.pred-clean { border-left-color: #238636; }
.pred-category {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  background: #30363d; color: #c9d1d9; margin-right: 6px;
}
.pred-category.cat-latch { background: #da3633; color: #fff; }
.pred-category.cat-functional { background: #d29922; color: #fff; }
.pred-category.cat-timing { background: #e3b341; color: #000; }
.pred-category.cat-area { background: #388bfd; color: #fff; }
.pred-category.cat-power { background: #a371f7; color: #fff; }
.pred-category.cat-optimization { background: #39d353; color: #000; }
.pred-category.cat-clean { background: #238636; color: #fff; }
.pred-text { color: #c9d1d9; margin-top: 3px; }
.pred-detail { color: #8b949e; font-size: 11px; margin-top: 2px; }

/* Agent Review */
.agent-btn {
  width: 100%; padding: 10px 16px; margin: 12px 0 8px;
  background: linear-gradient(135deg, #238636, #1a7f37); color: #fff;
  border: 1px solid #2ea043; border-radius: 6px; cursor: pointer;
  font-size: 13px; font-weight: 600; letter-spacing: 0.3px;
  transition: all 0.2s;
}
.agent-btn:hover { background: linear-gradient(135deg, #2ea043, #238636); }
.agent-btn:disabled { opacity: 0.6; cursor: not-allowed; }
.loading { padding: 12px; color: #8b949e; font-size: 12px; display: flex; align-items: center; gap: 8px; }
.spinner {
  width: 14px; height: 14px; border: 2px solid #30363d;
  border-top-color: #58a6ff; border-radius: 50%;
  animation: spin 0.8s linear infinite; display: inline-block;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Synthesis Scorecard */
.synth-scorecard {
  background: #161b22; border: 1px solid #30363d; border-radius: 6px;
  padding: 12px 16px; margin: 8px 0;
}
.scorecard-title {
  font-size: 13px; font-weight: 700; color: #58a6ff;
  margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.5px;
}
.scorecard-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 6px 20px;
}
.scorecard-item {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 12px; padding: 3px 0;
  border-bottom: 1px solid #21262d;
}
.scorecard-label { color: #8b949e; }
.scorecard-value { color: #c9d1d9; font-weight: 600; font-family: monospace; }
.scorecard-value.val-warn { color: #d29922; }
.scorecard-value.val-good { color: #238636; }
.scorecard-value.val-bad { color: #da3633; }

/* Prediction group headers */
.pred-group-header {
  font-size: 11px; font-weight: 700; color: #8b949e;
  text-transform: uppercase; letter-spacing: 0.5px;
  margin: 10px 0 4px 0; padding-bottom: 3px;
  border-bottom: 1px solid #21262d;
}

/* Finding severity badges */
.sev-badge {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  margin-right: 6px;
}
.sev-error { background: #da3633; color: #fff; }
.sev-warning { background: #d29922; color: #fff; }
.sev-info { background: #388bfd; color: #fff; }
.conf-badge {
  display: inline-block; padding: 1px 4px; border-radius: 3px;
  font-size: 9px; font-weight: 600; margin-right: 4px;
}
.conf-high { background: #238636; color: #fff; }
.conf-med { background: #6e7681; color: #fff; }
.conf-low { background: #484f58; color: #c9d1d9; }

/* Empty state */
.empty-state {
  padding: 40px 20px; text-align: center; color: #484f58;
  font-size: 13px;
}
.empty-state .icon { font-size: 32px; margin-bottom: 8px; }

/* Loading */
.loading {
  padding: 30px; text-align: center; color: #58a6ff; font-size: 13px;
}
.spinner {
  display: inline-block; width: 16px; height: 16px;
  border: 2px solid #30363d; border-top-color: #58a6ff;
  border-radius: 50%; animation: spin 0.8s linear infinite;
  margin-right: 6px; vertical-align: middle;
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<div class="topbar">
  <h1>COGNI LINT</h1>
  <select id="scenarioSelect">
    <option value="">-- select design --</option>
    <option value="__new__">+ Write New RTL</option>
  </select>
  <input type="text" id="customFilename" placeholder="filename.sv" value="design.sv"
    style="display:none; background:#21262d; color:#c9d1d9; border:1px solid #30363d;
    border-radius:6px; padding:6px 10px; font-size:13px; font-family:inherit; width:140px;">
  <button class="run-btn" id="runBtn" disabled>Run</button>
  <button class="save-btn" id="saveBtn" disabled>Save</button>
  <span class="status" id="statusText">Select a design or write new RTL</span>
</div>

<div class="panels">
  <!-- LEFT: RTL Code Editor -->
  <div class="panel" style="flex: 1.2">
    <div class="panel-header">
      <span class="tool-icon" style="background:#58a6ff"></span>
      RTL Code
      <span class="badge" id="fileBadge">no file</span>
    </div>
    <div class="file-tabs" id="fileTabs"></div>
    <div class="panel-body">
      <div class="editor-wrap" id="editorWrap">
        <div class="line-gutter" id="lineGutter"></div>
        <textarea class="code-editor" id="codeEditor"
          placeholder="Select a design or choose 'Write New RTL' to start coding..."
          spellcheck="false"></textarea>
      </div>
    </div>
  </div>

  <!-- PANEL 2: Cogni Analyzer (our own) -->
  <div class="panel">
    <div class="panel-header">
      <span class="tool-icon" style="background:#a371f7"></span>
      Cogni Analyzer
      <span class="badge" id="cogniBadge">--</span>
    </div>
    <div class="panel-body" id="cogniResults">
      <div class="empty-state">
        <div class="icon">&#9881;</div>
        Run to see Cogni analysis + synthesis predictions
      </div>
    </div>
  </div>

</div>

<script>
const state = {
  scenarios: [],
  scenario: '',
  files: [],
  activeFile: '',
  fileContents: {},
  running: false,
  customMode: false,
};

// ---- Line numbers & bug markers ----
function updateLineNumbers() {
  const editor = document.getElementById('codeEditor');
  const gutter = document.getElementById('lineGutter');
  const text = editor.value;
  const lineCount = text.split('\n').length;
  const markers = state.lineMarkers || {};
  let html = '';
  for (let i = 1; i <= lineCount; i++) {
    const marker = markers[i];
    const cls = marker ? ' line-' + marker.severity : '';
    const title = marker ? marker.messages.join('\n') : '';
    html += `<span class="line-num${cls}" data-line="${i}" title="${escAttr(title)}">${i}</span>`;
  }
  gutter.innerHTML = html;
}

function escAttr(s) {
  return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function syncScroll() {
  const editor = document.getElementById('codeEditor');
  const gutter = document.getElementById('lineGutter');
  gutter.scrollTop = editor.scrollTop;
}

function buildLineMarkers(findings) {
  const markers = {};
  (findings || []).forEach(f => {
    const line = f.line;
    if (!line || line < 1) return;
    if (!markers[line]) {
      markers[line] = { severity: f.severity, messages: [] };
    }
    // Promote severity: error > warning > info
    const order = { error: 0, warning: 1, info: 2 };
    if ((order[f.severity] || 2) < (order[markers[line].severity] || 2)) {
      markers[line].severity = f.severity;
    }
    markers[line].messages.push('[' + f.rule + '] ' + f.message);
  });
  return markers;
}

function scrollToLine(lineNum) {
  const editor = document.getElementById('codeEditor');
  const lineHeight = 19.5; // 13px * 1.5 line-height
  const scrollTo = (lineNum - 1) * lineHeight - editor.clientHeight / 3;
  editor.scrollTop = Math.max(0, scrollTo);
  // Flash highlight on gutter
  const gutter = document.getElementById('lineGutter');
  const spans = gutter.querySelectorAll('.line-num');
  spans.forEach(s => s.classList.remove('line-highlight'));
  if (spans[lineNum - 1]) {
    spans[lineNum - 1].classList.add('line-highlight');
    setTimeout(() => spans[lineNum - 1].classList.remove('line-highlight'), 1500);
  }
}

// ---- Init ----
async function init() {
  const res = await fetch('/api/scenarios');
  state.scenarios = await res.json();
  const sel = document.getElementById('scenarioSelect');
  state.scenarios.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.name;
    opt.textContent = s.name;
    sel.appendChild(opt);
  });
  sel.addEventListener('change', onScenarioChange);
  document.getElementById('runBtn').addEventListener('click', onRun);
  document.getElementById('saveBtn').addEventListener('click', onSave);
  const editorEl = document.getElementById('codeEditor');
  editorEl.addEventListener('input', () => {
    if (state.activeFile) {
      state.fileContents[state.activeFile] = editorEl.value;
    }
    updateLineNumbers();
  });
  editorEl.addEventListener('scroll', syncScroll);
  // Click on gutter line number scrolls to that line
  document.getElementById('lineGutter').addEventListener('click', (e) => {
    const span = e.target.closest('.line-num');
    if (span) scrollToLine(parseInt(span.dataset.line));
  });
  updateLineNumbers();
}

async function onScenarioChange() {
  const sel = document.getElementById('scenarioSelect');
  state.scenario = sel.value;
  const fnInput = document.getElementById('customFilename');

  if (state.scenario === '__new__') {
    // Custom RTL writing mode
    state.customMode = true;
    state.files = ['design.sv'];
    state.activeFile = 'design.sv';
    state.fileContents = {'design.sv': RTL_TEMPLATE};
    fnInput.style.display = '';
    document.getElementById('codeEditor').value = RTL_TEMPLATE;
    document.getElementById('fileBadge').textContent = 'design.sv';
    document.getElementById('runBtn').disabled = false;
    document.getElementById('saveBtn').disabled = true;
    document.getElementById('statusText').textContent = 'Write your RTL code and click Run';
    renderFileTabs();
    clearResults();
    updateLineNumbers();
    return;
  }

  state.customMode = false;
  fnInput.style.display = 'none';
  if (!state.scenario) return;

  const scen = state.scenarios.find(s => s.name === state.scenario);
  state.files = scen ? scen.files : [];
  state.fileContents = {};

  for (const fn of state.files) {
    const res = await fetch(`/api/rtl/${state.scenario}/${fn}`);
    const data = await res.json();
    state.fileContents[fn] = data.content;
  }

  renderFileTabs();
  if (state.files.length > 0) {
    selectFile(state.files[0]);
  }
  document.getElementById('runBtn').disabled = false;
  document.getElementById('saveBtn').disabled = false;
  document.getElementById('statusText').textContent = `Loaded ${state.scenario}`;
  clearResults();
}

function clearResults() {
  ['cogniResults'].forEach(id => {
    document.getElementById(id).innerHTML =
      '<div class="empty-state"><div class="icon">&#9881;</div>Click Run to analyze</div>';
  });
  ['cogniBadge'].forEach(id => {
    document.getElementById(id).textContent = '--';
    document.getElementById(id).className = 'badge';
  });
  state.lineMarkers = {};
  updateLineNumbers();
}

const RTL_TEMPLATE = `module my_design (
    input  logic        clk,
    input  logic        rst_n,
    input  logic [7:0]  data_in,
    output logic [7:0]  data_out
);

    // Write your RTL code here

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            data_out <= 8'd0;
        else
            data_out <= data_in;
    end

endmodule
`;

function renderFileTabs() {
  const tabs = document.getElementById('fileTabs');
  tabs.innerHTML = '';
  state.files.forEach(fn => {
    const tab = document.createElement('div');
    tab.className = 'file-tab' + (fn === state.activeFile ? ' active' : '');
    tab.textContent = fn;
    tab.onclick = () => selectFile(fn);
    tabs.appendChild(tab);
  });
}

function selectFile(fn) {
  // Save current
  if (state.activeFile) {
    state.fileContents[state.activeFile] = document.getElementById('codeEditor').value;
  }
  state.activeFile = fn;
  document.getElementById('codeEditor').value = state.fileContents[fn] || '';
  document.getElementById('fileBadge').textContent = fn;
  renderFileTabs();
  updateLineNumbers();
}

// ---- Run ----
async function onRun() {
  if (state.running) return;
  if (!state.scenario && !state.customMode) return;
  state.running = true;

  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  btn.textContent = 'Running...';
  document.getElementById('statusText').textContent = 'Running Cogni Analyzer...';

  // Save current editor state
  if (state.activeFile) {
    state.fileContents[state.activeFile] = document.getElementById('codeEditor').value;
  }

  // In custom mode, update filename from input
  let filename = state.activeFile;
  if (state.customMode) {
    const fnInput = document.getElementById('customFilename');
    filename = fnInput.value.trim() || 'design.sv';
    if (!filename.endsWith('.sv') && !filename.endsWith('.v')) {
      filename += '.sv';
    }
    state.activeFile = filename;
    document.getElementById('fileBadge').textContent = filename;
  }

  // Show loading
  document.getElementById('cogniResults').innerHTML =
    '<div class="loading"><span class="spinner"></span>Running Cogni Analyzer...</div>';

  const code = state.fileContents[state.activeFile] || document.getElementById('codeEditor').value;
  const scenario = state.customMode ? '' : state.scenario;
  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        code: code,
        filename: filename,
        scenario: scenario,
      }),
    });
    const data = await res.json();
    renderCogniResults(data.cogni, data.cogni_rules);
    const cogniTotal = (data.cogni || {}).total || 0;
    document.getElementById('statusText').textContent =
      `Cogni=${cogniTotal} findings`;
  } catch (e) {
    document.getElementById('statusText').textContent = 'Error: ' + e.message;
  }

  btn.disabled = false;
  btn.textContent = 'Run';
  state.running = false;
}

// ---- Save ----
async function onSave() {
  if (!state.scenario || !state.activeFile) return;
  if (state.activeFile) {
    state.fileContents[state.activeFile] = document.getElementById('codeEditor').value;
  }
  const res = await fetch('/api/save', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      code: state.fileContents[state.activeFile],
      scenario: state.scenario,
      filename: state.activeFile,
    }),
  });
  const data = await res.json();
  if (data.saved) {
    document.getElementById('statusText').textContent = `Saved ${state.activeFile}`;
  } else {
    document.getElementById('statusText').textContent = 'Save failed: ' + (data.error || '');
  }
}

// ---- Render Results ----
function renderCogniResults(result, rules) {
  const container = document.getElementById('cogniResults');
  const badge = document.getElementById('cogniBadge');

  if (!result || result.total === undefined) {
    container.innerHTML = '<div class="empty-state">No data</div>';
    return;
  }

  const errors = (result.findings || []).filter(f => f.severity === 'error').length;
  const warns = (result.findings || []).filter(f => f.severity === 'warning').length;
  badge.textContent = result.total + ' findings';
  badge.className = errors > 0 ? 'badge badge-error' : (warns > 0 ? 'badge' : 'badge badge-clean');

  let html = '';

  // ---- RTL FINDINGS FIRST (grouped by severity) ----
  const allFindings = result.findings || [];
  state.lineMarkers = buildLineMarkers(allFindings);
  updateLineNumbers();

  // Classify each finding by domain so the synthesis view stays focused.
  // CDC/RDC are clock/reset-domain risks (reported separately below); STYLE_*
  // are cosmetic. Everything else is a synthesis/functional finding.
  // Readability/quality rules belong with style, not the synthesis view.
  const qualityRules = new Set([
    'STRUCT_deep_nesting', 'STRUCT_nested_ternary', 'FUNC_magic_numbers',
    'W456_many_ports', 'STARC_many_regs', 'STARC_if_else_chain']);
  const domainOf = (r) =>
    (/^CDC_/.test(r) || /^RDC_/.test(r)) ? 'risk' :
    (/^STYLE_/.test(r) || qualityRules.has(r)) ? 'style' : 'synth';
  const findings   = allFindings.filter(f => domainOf(f.rule) === 'synth');
  const riskFindings  = allFindings.filter(f => domainOf(f.rule) === 'risk');
  const styleFindings = allFindings.filter(f => domainOf(f.rule) === 'style');

  // Render a plain list of findings into a section.
  const renderFindingList = (items) => {
    let s = '';
    items.forEach(f => {
      const sevCls = 'sev-' + f.severity;
      const synthTip = f.synth_impact ? `<div class="rule-detail">Synth: ${escHtml(f.synth_impact)}</div>` : '';
      const conf = f.confidence || 0;
      const confCls = conf >= 90 ? 'conf-high' : conf >= 60 ? 'conf-med' : 'conf-low';
      const confTag = conf > 0 ? `<span class="conf-badge ${confCls}">${conf}%</span>` : '';
      s += `<div class="warning-item finding-item" data-line="${f.line}" style="cursor:pointer"
        onclick="scrollToLine(${f.line})">
        <span class="sev-badge ${sevCls}">${f.severity}</span>
        ${confTag}
        <span class="cat">[${f.rule}]</span>
        <span class="loc">${f.file}:<strong>${f.line}</strong></span>
        <div class="msg">${escHtml(f.message)}</div>
        ${synthTip}
      </div>`;
    });
    return s;
  };

  if (findings.length > 0) {
    // --- Signal-grouped view: cluster related synthesis findings per signal ---
    const sigMap = {};
    findings.forEach(f => {
      const m = f.message.match(/'([a-zA-Z_]\w*)'/);
      const sig = m ? m[1] : null;
      if (sig) {
        if (!sigMap[sig]) sigMap[sig] = [];
        sigMap[sig].push(f);
      }
    });
    const multiSigFindings = Object.entries(sigMap).filter(([,v]) => v.length >= 2);
    if (multiSigFindings.length > 0) {
      html += `<div class="result-section"><h3>SIGNAL CLUSTERS (${multiSigFindings.length} signals with multiple findings)</h3>`;
      multiSigFindings.sort((a,b) => {
        const sevOrder = {error:0, warning:1, info:2};
        const aMax = Math.min(...a[1].map(f => sevOrder[f.severity] || 2));
        const bMax = Math.min(...b[1].map(f => sevOrder[f.severity] || 2));
        return aMax - bMax || b[1].length - a[1].length;
      });
      multiSigFindings.forEach(([sig, items]) => {
        const errs = items.filter(f => f.severity === 'error').length;
        const warns = items.filter(f => f.severity === 'warning').length;
        const sigCls = errs > 0 ? 'color:#da3633' : warns > 0 ? 'color:#d29922' : 'color:#8b949e';
        const badge = errs > 0 ? '<span class="sev-badge sev-error" style="font-size:9px">E</span>' :
                      warns > 0 ? '<span class="sev-badge sev-warning" style="font-size:9px">W</span>' : '';
        html += `<div style="margin:4px 0;padding:6px 8px;background:#161b22;border-radius:4px;border-left:3px solid ${errs?'#da3633':warns?'#d29922':'#30363d'}">
          <div style="font-weight:600;font-size:12px;color:#c9d1d9">
            ${badge} <span style="${sigCls}">${sig}</span>
            <span style="color:#8b949e;font-weight:400;font-size:10px;margin-left:6px">${items.length} findings</span>
          </div>
          <div style="margin-top:3px;font-size:10px;color:#8b949e">`;
        items.forEach(f => {
          const sc = f.severity === 'error' ? '#da3633' : f.severity === 'warning' ? '#d29922' : '#8b949e';
          html += `<div style="margin:1px 0;cursor:pointer" onclick="scrollToLine(${f.line})">
            <span style="color:${sc}">${f.severity[0].toUpperCase()}</span>
            <span style="color:#58a6ff">${f.rule}</span> L${f.line}
          </div>`;
        });
        html += '</div></div>';
      });
      html += '</div>';
    }

    // --- Standard severity-grouped view (synthesis/functional only) ---
    const groups = {error:[], warning:[], info:[]};
    findings.forEach(f => (groups[f.severity] || groups.info).push(f));
    const sectionNames = {error:'ERRORS', warning:'WARNINGS', info:'INFO'};
    for (const sev of ['error','warning','info']) {
      const items = groups[sev];
      if (!items.length) continue;
      html += `<div class="result-section"><h3>${sectionNames[sev]} (${items.length})</h3>`;
      html += renderFindingList(items);
      html += '</div>';
    }
  } else if (allFindings.length === 0) {
    html += '<div class="result-section"><h3>RTL Findings</h3>';
    html += '<div style="padding:8px;color:#238636;font-size:12px">No issues found</div>';
    html += '</div>';
  }

  // ---- SYNTHESIS READINESS ----
  const synth = (result.measurements || {})['cogni.synth'];
  if (synth) {
    const t = synth.timing || {};
    const pwr = synth.power || {};
    const fpga = synth.fpga_resources || {};
    const fsm = synth.fsm || {};
    const mem = synth.memory || {};
    const ops = synth.operators || {};
    const opt = synth.optimization || {};

    const findings = result.findings || [];
    const synthBlockerRules = [
      'FUNC_comb_loop', 'W_multi_driver',
      'SIM_force_release', 'SYNTH_5006_while',
      'SYNTH_5008_for_bound', 'SYNTH_5007_event_in_comb',
      'SYNTH_recursive_func'
    ];
    const synthWarnRules = [
      'SYNTH_5001_delay', 'SYNTH_5000_initial',
      'SYNTH_5003_integer', 'SYNTH_5004_sys_task',
      'CLK_gated_clock', 'CLK_data_as_clock'
    ];
    // Multi-write-port RAM is a *portable* fail but vendor tools can infer it
    // from a dual-port template — treat it as vendor-dependent, not a hard blocker.
    const isRamMultiWrite = f => f.rule === 'W_multi_driver' &&
      /write port|RAM array/i.test(f.message || '');
    const ramMultiWrite = findings.filter(isRamMultiWrite);
    const hardBlockers = findings.filter(f =>
      synthBlockerRules.includes(f.rule) && !isRamMultiWrite(f));
    const hasBlockers = hardBlockers.length > 0;
    const hasSynthWarns = findings.some(f => synthWarnRules.includes(f.rule));
    const hasLatches = synth.latch_bits > 0 ||
      findings.some(f => f.rule === 'W402_latch_inferred');
    const hasDeadLogic = findings.some(f =>
      f.rule === 'FUNC_cmp_out_of_range' || f.rule === 'FUNC_counter_overflow');
    const hasRamMultiWrite = ramMultiWrite.length > 0;
    const readyStatus = hasBlockers ? 'FAIL' :
                        (hasRamMultiWrite || hasLatches || hasDeadLogic || hasSynthWarns) ? 'WARN' : 'PASS';
    const readyCls = readyStatus === 'PASS' ? 'val-good' : readyStatus === 'WARN' ? 'val-warn' : 'val-bad';
    let readyIcon = 'Synthesizable';
    if (readyStatus === 'FAIL') {
      const reasons = [...new Set(hardBlockers.map(f => f.rule.replace(/^(SIM_|SYNTH_\d+_)/, '')))];
      readyIcon = 'Synthesis will FAIL (' + reasons.slice(0,3).join(', ') + ')';
    } else if (readyStatus === 'WARN') {
      const risks = [];
      if (hasRamMultiWrite) risks.push('multi-write RAM (needs vendor dual-port template)');
      if (hasLatches) risks.push('inferred latches');
      if (hasDeadLogic) risks.push('dead logic');
      if (hasSynthWarns) {
        const warnFindings = findings.filter(f => synthWarnRules.includes(f.rule));
        const warnReasons = [...new Set(warnFindings.map(f => f.rule.replace(/^(SYNTH_\d+_|CLK_)/, '')))];
        risks.push(...warnReasons.slice(0,2));
      }
      // Multi-write RAM is portable-fail but vendor-inferrable — say so plainly.
      if (hasRamMultiWrite) {
        readyIcon = 'Portable synth: FAIL · Vendor: template-dependent (' + risks.join(', ') + ')';
      } else {
        readyIcon = 'Synthesizable (risks: ' + risks.join(', ') + ')';
      }
    }

    html += `<div class="result-section">
      <h3>SYNTHESIS READINESS</h3>
      <div class="synth-scorecard">
        <div class="scorecard-grid">
          <div class="scorecard-item">
            <span class="scorecard-label">Status</span>
            <span class="scorecard-value ${readyCls}">${readyIcon}</span>
          </div>
        </div>
      </div>

      <h4 style="color:#8b949e;margin:10px 0 6px;font-size:11px">HARDWARE INFERENCE</h4>
      <table style="width:100%;font-size:11px;border-collapse:collapse">
        <tr style="color:#8b949e;text-align:left;border-bottom:1px solid #30363d">
          <th style="padding:4px">Resource</th><th>Inferred?</th><th>Detail</th>
        </tr>`;

    // Flip-Flops
    const ffBits = synth.ff_bits || 0;
    const ffSigs = Object.keys(synth.ff_signals || {});
    html += `<tr style="border-bottom:1px solid #21262d">
      <td style="padding:3px 4px;color:#58a6ff">Flip-Flops</td>
      <td style="color:#c9d1d9">${ffBits > 0 ? 'Yes' : 'No'} (${ffBits} bits, ${ffSigs.length} registers)</td>
      <td style="color:#8b949e">${ffSigs.slice(0,5).join(', ')}${ffSigs.length > 5 ? '...' : ''}</td>
    </tr>`;

    // Latches
    const latchSigNames = synth.latch_signal_names || [];
    const latchCount = latchSigNames.length || synth.latch_bits || 0;
    html += `<tr style="border-bottom:1px solid #21262d">
      <td style="padding:3px 4px;${hasLatches ? 'color:#da3633' : 'color:#58a6ff'}">Latch</td>
      <td style="${hasLatches ? 'color:#da3633;font-weight:600' : 'color:#238636'}">${hasLatches ? 'Yes (' + latchCount + ' signal' + (latchCount !== 1 ? 's' : '') + ') — UNINTENDED' : 'No'}</td>
      <td style="color:#8b949e">${latchSigNames.length > 0 ? latchSigNames.slice(0,5).join(', ') : (hasLatches ? 'Missing default/else in always_comb' : 'Clean')}</td>
    </tr>`;

    // Clock Enable
    const ceSigs = synth.ce_signals || [];
    const nonCeSigs = synth.non_ce_signals || [];
    const enBlocks = ceSigs.length;
    const totalBlocks = enBlocks + nonCeSigs.length;
    const ceDetail = enBlocks > 0
      ? 'CE: ' + ceSigs.slice(0,4).join(', ') + (nonCeSigs.length > 0 ? ' | No CE: ' + nonCeSigs.slice(0,4).join(', ') : '')
      : 'Conditional data selection (mux), not dedicated CE';
    html += `<tr style="border-bottom:1px solid #21262d">
      <td style="padding:3px 4px;color:#58a6ff">Clock Enable</td>
      <td style="color:#c9d1d9">${enBlocks > 0 ? 'Yes (' + enBlocks + ' of ' + totalBlocks + ' blocks)' : 'No'}</td>
      <td style="color:#8b949e">${ceDetail}</td>
    </tr>`;

    // DSP (Multiplier)
    html += `<tr style="border-bottom:1px solid #21262d">
      <td style="padding:3px 4px;color:#58a6ff">DSP</td>
      <td style="color:#c9d1d9">${ops.multipliers > 0 ? 'Yes (' + ops.multipliers + ' multiplier' + (ops.multipliers > 1 ? 's)' : ')') : 'No'}</td>
      <td style="color:#8b949e">${ops.multipliers > 0 ? 'FPGA: maps to DSP48' : 'No multipliers'}</td>
    </tr>`;

    // BRAM / Memory
    html += `<tr style="border-bottom:1px solid #21262d">
      <td style="padding:3px 4px;color:#58a6ff">BRAM</td>
      <td style="color:#c9d1d9">${mem.arrays > 0 ? 'Yes (' + mem.arrays + ' array' + (mem.arrays > 1 ? 's' : '') + ', ' + (mem.total_bits || 0).toLocaleString() + ' bits)' : 'No'}</td>
      <td style="color:#8b949e">${(fpga.bram_blocks || 0) > 0 ? fpga.bram_blocks + ' BRAM block(s)' : mem.arrays > 0 ? 'LUTRAM or FF-mapped' : 'No memory arrays'}</td>
    </tr>`;

    // Adder / Carry Chain
    const adderExprs = (ops.adder_details || []).map(d => d.expr).slice(0,4).join(', ');
    html += `<tr style="border-bottom:1px solid #21262d">
      <td style="padding:3px 4px;color:#58a6ff">Adder / Carry Chain</td>
      <td style="color:#c9d1d9">${ops.adders > 0 ? 'Yes (' + ops.adders + ')' : 'No'}</td>
      <td style="color:#8b949e">${ops.adders > 0 ? adderExprs : 'No arithmetic'}</td>
    </tr>`;

    // Comparator
    const cmpExprs = (ops.comparator_details || []).map(d => d.expr).slice(0,5).join(', ');
    html += `<tr style="border-bottom:1px solid #21262d">
      <td style="padding:3px 4px;color:#58a6ff">Comparator</td>
      <td style="color:#c9d1d9">${ops.comparators > 0 ? 'Yes (' + ops.comparators + ')' : 'No'}</td>
      <td style="color:#8b949e">${ops.comparators > 0 ? cmpExprs : 'No magnitude comparisons'}</td>
    </tr>`;

    // State Decode
    const sdCount = ops.state_decodes || 0;
    const sdExprs = (ops.state_decode_details || []).map(d => d.expr).slice(0,5).join(', ');
    if (sdCount > 0) {
      html += `<tr style="border-bottom:1px solid #21262d">
        <td style="padding:3px 4px;color:#58a6ff">State Decode</td>
        <td style="color:#c9d1d9">Yes (${sdCount})</td>
        <td style="color:#8b949e">${sdExprs}</td>
      </tr>`;
    }

    // SRL (shift registers)
    html += `<tr style="border-bottom:1px solid #21262d">
      <td style="padding:3px 4px;color:#58a6ff">SRL</td>
      <td style="color:#c9d1d9">${ops.shifters > 0 ? 'Possible (' + ops.shifters + ' shift op' + (ops.shifters > 1 ? 's)' : ')') : 'No'}</td>
      <td style="color:#8b949e">${ops.shifters > 0 ? 'FPGA: may infer SRL16/SRL32' : 'No shift registers'}</td>
    </tr>`;

    // FSM
    if (fsm.states > 0) {
      html += `<tr style="border-bottom:1px solid #21262d">
        <td style="padding:3px 4px;color:#58a6ff">FSM</td>
        <td style="color:#c9d1d9">Yes (${fsm.states} states, ${fsm.encoding_bits}-bit)</td>
        <td style="color:#8b949e">One-hot: ${fsm.states} FFs | Binary: ${fsm.min_bits} FFs</td>
      </tr>`;
    }

    // Constant propagation / dead code
    const constCount = (opt.const_prop_count || 0) + (opt.dead_branches || 0);
    if (constCount > 0) {
      html += `<tr style="border-bottom:1px solid #21262d">
        <td style="padding:3px 4px;color:#d2a8ff">Dead Code</td>
        <td style="color:#d2a8ff">${constCount} branch(es) removed</td>
        <td style="color:#8b949e">Synthesis will optimize away</td>
      </tr>`;
    }

    // Combinational loop
    const hasLoops = findings.some(f => f.rule === 'FUNC_comb_loop');
    if (hasLoops) {
      html += `<tr style="border-bottom:1px solid #21262d">
        <td style="padding:3px 4px;color:#da3633">Combo Loop</td>
        <td style="color:#da3633;font-weight:600">SYNTHESIS BLOCKER</td>
        <td style="color:#8b949e">Most tools abort on combinational loops</td>
      </tr>`;
    }

    // Multiple drivers
    const hasMultiDrv = findings.some(f => f.rule === 'W_multi_driver');
    if (hasMultiDrv) {
      html += `<tr style="border-bottom:1px solid #21262d">
        <td style="padding:3px 4px;color:#da3633">Multi-Driver</td>
        <td style="color:#da3633;font-weight:600">SYNTHESIS BLOCKER</td>
        <td style="color:#8b949e">Bus contention — synthesis may fail</td>
      </tr>`;
    }

    // Synthesis blockers from findings
    const blockerFindings = findings.filter(f => synthBlockerRules.includes(f.rule) && f.rule !== 'FUNC_comb_loop' && f.rule !== 'W_multi_driver');
    if (blockerFindings.length > 0) {
      const blockerNames = [...new Set(blockerFindings.map(f => f.rule))];
      for (const rule of blockerNames) {
        const count = blockerFindings.filter(f => f.rule === rule).length;
        const label = rule.replace(/^(SIM_|SYNTH_\d+_|SYNTH_)/, '');
        html += `<tr style="border-bottom:1px solid #21262d">
          <td style="padding:3px 4px;color:#da3633">${label}</td>
          <td style="color:#da3633;font-weight:600">SYNTHESIS BLOCKER (${count})</td>
          <td style="color:#8b949e">${blockerFindings.find(f => f.rule === rule).message.slice(0,60)}</td>
        </tr>`;
      }
    }

    html += '</table>';

    // Inferred Netlist Summary (Phase 4)
    const netlistData = (result.measurements || {})['cogni.netlist'];
    if (netlistData && netlistData.total_cells > 0) {
      html += `<h4 style="color:#8b949e;margin:10px 0 6px;font-size:11px">INFERRED NETLIST</h4>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(80px,1fr));gap:6px;font-size:11px">`;
      const typeLabels = {FF:'Flip-Flops', LATCH:'Latches', MUX:'Muxes', ADDER:'Adders',
        MULT:'Multipliers', COMP:'Comparators', SHIFT:'Shifters', BRAM:'BRAMs', LUT:'LUT-RAMs'};
      for (const [type, count] of Object.entries(netlistData.cell_types || {})) {
        const label = typeLabels[type] || type;
        const color = type === 'LATCH' ? '#da3633' : '#58a6ff';
        html += `<div style="text-align:center;padding:4px;background:#161b22;border:1px solid #30363d;border-radius:4px">
          <div style="font-size:16px;font-weight:600;color:${color}">${count}</div>
          <div style="color:#8b949e;font-size:9px">${label}</div>
        </div>`;
      }
      html += `<div style="text-align:center;padding:4px;background:#161b22;border:1px solid #30363d;border-radius:4px">
        <div style="font-size:16px;font-weight:600;color:#c9d1d9">${netlistData.total_cells}</div>
        <div style="color:#8b949e;font-size:9px">Total Cells</div>
      </div>
      <div style="text-align:center;padding:4px;background:#161b22;border:1px solid #30363d;border-radius:4px">
        <div style="font-size:16px;font-weight:600;color:#c9d1d9">${netlistData.total_nets}</div>
        <div style="color:#8b949e;font-size:9px">Nets</div>
      </div>`;
      html += '</div>';
    }

    html += '</div>';
  }

  // ---- CDC CROSSING REPORT ----
  const cdcData = (result.measurements || {})['cogni.cdc'];
  if (cdcData && cdcData.total_crossings > 0) {
    const safeCls = cdcData.violations === 0 ? 'val-good' : 'val-bad';
    html += `<div class="result-section">
      <h3>CDC CROSSING REPORT</h3>
      <div class="synth-scorecard">
        <div class="scorecard-grid">
          <div class="scorecard-item">
            <span class="scorecard-label">Total Crossings</span>
            <span class="scorecard-value">${cdcData.total_crossings}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Safe (Synchronized)</span>
            <span class="scorecard-value val-good">${cdcData.safe}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Violations</span>
            <span class="scorecard-value ${safeCls}">${cdcData.violations}</span>
          </div>
        </div>
      </div>
      <table style="width:100%;font-size:11px;border-collapse:collapse;margin-top:6px">
        <tr style="color:#8b949e;text-align:left;border-bottom:1px solid #30363d">
          <th style="padding:4px">ID</th><th>Signal</th><th>Width</th>
          <th>Source</th><th>Dest</th><th>Sync Type</th><th>Status</th>
        </tr>`;
    (cdcData.crossings || []).forEach(c => {
      const stCls = c.status === 'PASS' ? 'color:#238636' : 'color:#da3633;font-weight:600';
      const syncCls = c.sync_type.startsWith('NONE') ? 'color:#da3633' : 'color:#238636';
      html += `<tr style="border-bottom:1px solid #21262d">
        <td style="padding:3px 4px;color:#58a6ff">${c.id}</td>
        <td style="color:#c9d1d9">${c.signal}</td>
        <td>${c.width}b</td>
        <td style="color:#d2a8ff">${c.src_domain}</td>
        <td style="color:#79c0ff">${c.dst_domain}</td>
        <td style="${syncCls}">${c.sync_type}</td>
        <td style="${stCls}">${c.status}</td>
      </tr>`;
    });
    html += '</table></div>';
  }

  // ---- RDC RESET DOMAIN REPORT ----
  const rdcData = (result.measurements || {})['cogni.rdc'];
  if (rdcData && rdcData.total_issues > 0) {
    const rdcCls = rdcData.errors === 0 ? 'val-good' : 'val-bad';
    html += `<div class="result-section">
      <h3>RDC RESET DOMAIN REPORT</h3>
      <div class="synth-scorecard">
        <div class="scorecard-grid">
          <div class="scorecard-item">
            <span class="scorecard-label">Total Issues</span>
            <span class="scorecard-value ${rdcCls}">${rdcData.total_issues}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Errors</span>
            <span class="scorecard-value val-bad">${rdcData.errors}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Warnings</span>
            <span class="scorecard-value" style="color:#d29922">${rdcData.warnings}</span>
          </div>
        </div>
      </div>
      <table style="width:100%;font-size:11px;border-collapse:collapse;margin-top:6px">
        <tr style="color:#8b949e;text-align:left;border-bottom:1px solid #30363d">
          <th style="padding:4px">Rule</th><th>Signal</th><th>Line</th>
          <th>Severity</th><th>Detail</th>
        </tr>`;
    (rdcData.items || []).forEach(r => {
      const sevCls = r.severity === 'error' ? 'color:#da3633;font-weight:600' : 'color:#d29922';
      html += `<tr style="border-bottom:1px solid #21262d">
        <td style="padding:3px 4px;color:#58a6ff">${r.rule}</td>
        <td style="color:#c9d1d9">${r.signal}</td>
        <td>${r.line}</td>
        <td style="${sevCls}">${r.severity.toUpperCase()}</td>
        <td style="color:#8b949e;font-size:10px">${r.message.substring(0,80)}...</td>
      </tr>`;
    });
    html += '</table></div>';
  }

  // ---- DFT TESTABILITY REPORT ----
  const dftData = (result.measurements || {})['cogni.dft'];
  if (dftData && dftData.total_issues > 0) {
    const dftCls = dftData.errors === 0 ? 'val-good' : 'val-bad';
    html += `<div class="result-section">
      <h3>DFT TESTABILITY REPORT</h3>
      <div class="synth-scorecard">
        <div class="scorecard-grid">
          <div class="scorecard-item">
            <span class="scorecard-label">Total Issues</span>
            <span class="scorecard-value ${dftCls}">${dftData.total_issues}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Errors</span>
            <span class="scorecard-value val-bad">${dftData.errors}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Warnings</span>
            <span class="scorecard-value" style="color:#d29922">${dftData.warnings}</span>
          </div>
        </div>
      </div>
      <table style="width:100%;font-size:11px;border-collapse:collapse;margin-top:6px">
        <tr style="color:#8b949e;text-align:left;border-bottom:1px solid #30363d">
          <th style="padding:4px">Rule</th><th>Signal</th><th>Line</th>
          <th>Severity</th><th>Detail</th>
        </tr>`;
    (dftData.items || []).forEach(r => {
      const sevCls = r.severity === 'error' ? 'color:#da3633;font-weight:600' : 'color:#d29922';
      html += `<tr style="border-bottom:1px solid #21262d">
        <td style="padding:3px 4px;color:#58a6ff">${r.rule}</td>
        <td style="color:#c9d1d9">${r.signal}</td>
        <td>${r.line}</td>
        <td style="${sevCls}">${r.severity.toUpperCase()}</td>
        <td style="color:#8b949e;font-size:10px">${r.message.substring(0,80)}...</td>
      </tr>`;
    });
    html += '</table></div>';
  }

  // ---- SVA FORMAL INTENT REPORT ----
  const svaData = (result.measurements || {})['cogni.sva'];
  if (svaData && svaData.total_issues > 0) {
    const svaCls = svaData.errors === 0 ? 'val-good' : 'val-bad';
    html += `<div class="result-section">
      <h3>SVA FORMAL INTENT REPORT</h3>
      <div class="synth-scorecard">
        <div class="scorecard-grid">
          <div class="scorecard-item">
            <span class="scorecard-label">Total Issues</span>
            <span class="scorecard-value ${svaCls}">${svaData.total_issues}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Errors</span>
            <span class="scorecard-value val-bad">${svaData.errors}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Warnings</span>
            <span class="scorecard-value" style="color:#d29922">${svaData.warnings}</span>
          </div>
        </div>
      </div>
      <table style="width:100%;font-size:11px;border-collapse:collapse;margin-top:6px">
        <tr style="color:#8b949e;text-align:left;border-bottom:1px solid #30363d">
          <th style="padding:4px">Rule</th><th>Signal</th><th>Line</th>
          <th>Severity</th><th>Detail</th>
        </tr>`;
    (svaData.items || []).forEach(r => {
      const sevCls = r.severity === 'error' ? 'color:#da3633;font-weight:600' : 'color:#d29922';
      html += `<tr style="border-bottom:1px solid #21262d">
        <td style="padding:3px 4px;color:#58a6ff">${r.rule}</td>
        <td style="color:#c9d1d9">${r.signal}</td>
        <td>${r.line}</td>
        <td style="${sevCls}">${r.severity.toUpperCase()}</td>
        <td style="color:#8b949e;font-size:10px">${r.message.substring(0,80)}...</td>
      </tr>`;
    });
    html += '</table></div>';
  }

  // ---- HIERARCHICAL ANALYSIS REPORT ----
  const hierData = (result.measurements || {})['cogni.hier'];
  if (hierData && hierData.total_issues > 0) {
    const hierCls = hierData.errors === 0 ? 'val-good' : 'val-bad';
    html += `<div class="result-section">
      <h3>HIERARCHICAL ANALYSIS REPORT</h3>
      <div class="synth-scorecard">
        <div class="scorecard-grid">
          <div class="scorecard-item">
            <span class="scorecard-label">Total Issues</span>
            <span class="scorecard-value ${hierCls}">${hierData.total_issues}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Errors</span>
            <span class="scorecard-value val-bad">${hierData.errors}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Warnings</span>
            <span class="scorecard-value" style="color:#d29922">${hierData.warnings}</span>
          </div>
        </div>
      </div>
      <table style="width:100%;font-size:11px;border-collapse:collapse;margin-top:6px">
        <tr style="color:#8b949e;text-align:left;border-bottom:1px solid #30363d">
          <th style="padding:4px">Rule</th><th>Signal</th><th>Line</th>
          <th>Severity</th><th>Detail</th>
        </tr>`;
    (hierData.items || []).forEach(r => {
      const sevCls = r.severity === 'error' ? 'color:#da3633;font-weight:600' : 'color:#d29922';
      html += `<tr style="border-bottom:1px solid #21262d">
        <td style="padding:3px 4px;color:#58a6ff">${r.rule}</td>
        <td style="color:#c9d1d9">${r.signal}</td>
        <td>${r.line}</td>
        <td style="${sevCls}">${r.severity.toUpperCase()}</td>
        <td style="color:#8b949e;font-size:10px">${r.message.substring(0,80)}...</td>
      </tr>`;
    });
    html += '</table></div>';
  }

  // ---- TIMING PREDICTION (back-annotated from inferred netlist) ----
  const timing = (result.measurements || {})['cogni.timing'];
  const sdcTiming = (result.measurements || {})['cogni.sdc.timing'];
  if (timing && (timing.critical_paths || []).length) {
    const fmax = timing.max_freq_mhz || 0;
    // Relative band, not an absolute number — logic delay is process-dependent.
    const band = timing.worst_depth <= 2 ? 'Very short (timing-friendly)' :
                 timing.worst_depth <= 4 ? 'Short' :
                 timing.worst_depth <= 8 ? 'Moderate' : 'Long (watch closely)';
    const fpgaBand = fmax >= 500 ? '>500 MHz' :
                     fmax >= 250 ? '250-500 MHz' :
                     fmax >= 100 ? '100-250 MHz' : '<100 MHz';
    html += '<div class="result-section"><h3>TIMING PREDICTION</h3>';
    html += `<div style="color:#8b949e;font-size:10px;margin:0 0 6px">Logic-only estimate — wire-load, placement &amp; routing ignored. Relative ranking, not sign-off.</div>`;
    html += `<div class="synth-scorecard"><div class="scorecard-grid">
      <div class="scorecard-item"><span class="scorecard-label">Critical path</span>
        <span class="scorecard-value val-warn">${band}</span></div>
      <div class="scorecard-item"><span class="scorecard-label">Logic depth</span>
        <span class="scorecard-value">${timing.worst_depth} level(s)</span></div>
      <div class="scorecard-item"><span class="scorecard-label">Endpoint</span>
        <span class="scorecard-value">${escHtml(timing.worst_endpoint || '')}</span></div>
    </div></div>`;
    html += `<div style="color:#8b949e;font-size:10px;margin:6px 0">Est. logic delay ~${timing.worst_path_ns} ns → FPGA Fmax ${fpgaBand}, ASIC higher. Actual timing depends on technology library, placement, and routing.</div>`;

    // Friendly names for the inferred cell types on a path.
    const cellName = (c) => {
      const expr = (c.expr || '');
      if (c.type === 'ADDER') return /-/.test(expr) && !/\+/.test(expr) ? 'Decrementer' :
                                     /\+\s*1/.test(expr) ? 'Incrementer' : 'Adder';
      return {COMP:'Comparator', MUX:'Mux', SHIFT:'Shifter', MULT:'Multiplier',
              LUT:'Logic', FF:'Register', LATCH:'Latch', BRAM:'Memory'}[c.type] || c.type;
    };
    html += `<h4 style="color:#8b949e;margin:10px 0 6px;font-size:11px">CRITICAL PATHS (longest logic chains)</h4>`;
    (timing.critical_paths || []).slice(0, 5).forEach(p => {
      const launch = p.launched_by || 'input';
      const logic = (p.path || [])
        .filter(c => c.type !== 'FF' && c.type !== 'LATCH')
        .map(c => cellName(c) + (c.line ? ' <span style="color:#6e7681">(L' + c.line + ')</span>' : ''));
      const endpoint = (p.endpoint || '') + (p.endpoint_kind === 'reg' ? ' register' : '');
      const flow = [`<span style="color:#58a6ff">${escHtml(launch)}</span>`]
        .concat(logic.map(l => `<span style="color:#d29922">${l}</span>`))
        .concat([`<span style="color:#3fb950">${escHtml(endpoint)}</span>`])
        .join(' <span style="color:#6e7681">&rarr;</span> ');
      html += `<div style="margin:5px 0;padding:5px 8px;background:#161b22;border-radius:4px;border-left:3px solid #d29922">
        <div style="font-size:11px">${flow}</div>
        <div style="color:#8b949e;font-size:10px;margin-top:2px">Logic depth ${p.depth} &middot; est. ${p.total_ns} ns</div>
      </div>`;
    });

    // Per-clock slack when an SDC was supplied.
    if (sdcTiming && Object.keys(sdcTiming).length) {
      html += `<h4 style="color:#8b949e;margin:10px 0 6px;font-size:11px">SDC TIMING SLACK</h4>
        <table style="width:100%;font-size:11px;border-collapse:collapse">
        <tr style="color:#8b949e;text-align:left;border-bottom:1px solid #30363d">
          <th style="padding:3px">Clock</th><th>Period</th><th>Critical</th><th>Slack</th><th>Status</th></tr>`;
      for (const [clk, si] of Object.entries(sdcTiming)) {
        const ok = si.met;
        html += `<tr style="border-bottom:1px solid #21262d">
          <td style="padding:3px;color:#c9d1d9">${escHtml(clk)}</td>
          <td style="color:#8b949e">${si.period_ns} ns (${si.freq_mhz} MHz)</td>
          <td style="color:#8b949e">${si.critical_path_ns} ns</td>
          <td style="color:${ok ? '#238636' : '#da3633'}">${si.slack_ns >= 0 ? '+' : ''}${si.slack_ns} ns</td>
          <td style="color:${ok ? '#238636' : '#da3633'}">${ok ? 'MET' : 'VIOLATED'}</td></tr>`;
      }
      html += '</table>';
    }
    html += '</div>';
  }

  // ---- SYNTHESIS PREDICTIONS (grouped by category) ----
  const preds = result.predictions || [];
  if (preds.length > 0) {
    html += '<div class="result-section"><h3>SYNTHESIS PREDICTIONS</h3>';

    const catOrder = ['functional', 'inference', 'optimization', 'clean'];
    const catLabels = {
      inference: 'Hardware Inference (what synthesis will create)',
      optimization: 'Optimization (what synthesis will remove)',
      functional: 'Synthesis Blockers & Risks',
      clean: 'Status'
    };
    const grouped = {};
    preds.forEach(p => {
      if (!grouped[p.category]) grouped[p.category] = [];
      grouped[p.category].push(p);
    });

    catOrder.forEach(cat => {
      const items = grouped[cat];
      if (!items) return;
      html += `<div class="pred-group-header">${catLabels[cat] || cat}</div>`;
      items.forEach(p => {
        const cls = 'pred-' + p.confidence;
        const catCls = 'cat-' + p.category;
        html += `<div class="prediction-item ${cls}">
          <span class="pred-category ${catCls}">${p.category}</span>
          <div class="pred-text">${escHtml(p.prediction)}</div>
          <div class="pred-detail">${escHtml(p.detail)}</div>
        </div>`;
      });
    });
    html += '</div>';
  }

  // ---- OPTIMIZATION OPPORTUNITIES (what synthesis will do) ----
  const synthOpt = (result.measurements || {})['cogni.synth'];
  if (synthOpt) {
    const o = synthOpt.optimization || {};
    const op = synthOpt.operators || {};
    const ce = synthOpt.ce_signals || [];
    const nce = synthOpt.non_ce_signals || [];
    const memo = synthOpt.memory || {};
    const cmpDet = op.comparator_details || [];
    const eqCmp = cmpDet.filter(d => /==|!=/.test(d.expr || '')).length;
    const items = [];
    if (op.adders > 0) items.push(['Carry-chain inference', `${op.adders} adder/subtractor(s) map to dedicated carry logic`]);
    if (ce.length > 0) items.push(['Clock-enable inference', `${ce.length} of ${ce.length + nce.length} register blocks gate on an enable`]);
    if (eqCmp > 0) items.push(['Comparator simplification', `${eqCmp} equality compare(s) reduce to XNOR/AND trees`]);
    if ((o.const_prop_count || 0) > 0) items.push(['Constant propagation', `${o.const_prop_count} constant expression(s) folded`]);
    if ((o.dead_branches || 0) > 0) items.push(['Dead-branch elimination', `${o.dead_branches} unreachable branch(es) removed`]);
    if ((o.constant_outputs || []).length > 0) items.push(['Constant output pruning', `${o.constant_outputs.length} tied-off output(s)`]);
    if (memo.arrays > 0) items.push(['Memory inference', `${memo.arrays} array(s), ${memo.total_bits} bits`]);
    // Analyses we do NOT perform — state plainly so the absence isn't misread.
    const notDetected = ['Retiming (register balancing)', 'Resource sharing (mux-folding of operators)'];
    if (items.length || notDetected.length) {
      html += '<div class="result-section"><h3>OPTIMIZATION OPPORTUNITIES</h3>';
      html += `<div style="color:#8b949e;font-size:10px;margin:0 0 6px">What synthesis is expected to do with this RTL (inferred from structure).</div>`;
      items.forEach(([name, detail]) => {
        html += `<div style="margin:3px 0;font-size:11px">
          <span style="color:#3fb950">&#10003;</span>
          <strong style="color:#c9d1d9">${name}</strong>
          <span style="color:#8b949e"> &mdash; ${escHtml(detail)}</span></div>`;
      });
      notDetected.forEach(name => {
        html += `<div style="margin:3px 0;font-size:11px">
          <span style="color:#6e7681">&#9675;</span>
          <span style="color:#6e7681">${name} &mdash; not analyzed</span></div>`;
      });
      html += '</div>';
    }
  }

  // ---- DESIGN RISKS (CDC / RDC) — separate from synthesis ----
  if (riskFindings.length > 0) {
    const cdc = riskFindings.filter(f => /^CDC_/.test(f.rule));
    const rdc = riskFindings.filter(f => /^RDC_/.test(f.rule));
    html += `<div class="result-section"><h3>DESIGN RISKS &mdash; CLOCK / RESET DOMAIN (${riskFindings.length})</h3>`;
    html += `<div style="color:#8b949e;font-size:10px;margin:0 0 6px">Clock- and reset-domain-crossing risks &mdash; not synthesis blockers; verified structurally (see CDC/RDC reports for detail).</div>`;
    if (cdc.length) { html += `<h4 style="color:#8b949e;margin:8px 0 4px;font-size:11px">CDC (${cdc.length})</h4>`; html += renderFindingList(cdc); }
    if (rdc.length) { html += `<h4 style="color:#8b949e;margin:8px 0 4px;font-size:11px">RDC (${rdc.length})</h4>`; html += renderFindingList(rdc); }
    html += '</div>';
  }

  // ---- CODING STYLE & QUALITY — separate from synthesis ----
  if (styleFindings.length > 0) {
    html += `<div class="result-section"><h3>CODING STYLE &amp; QUALITY (${styleFindings.length})</h3>`;
    html += `<div style="color:#8b949e;font-size:10px;margin:0 0 6px">Cosmetic / convention / readability suggestions &mdash; no synthesis impact.</div>`;
    html += renderFindingList(styleFindings);
    html += '</div>';
  }

  // Rules checked against Cogni measurements
  if (rules && rules.length) {
    const violations = rules.filter(r => r.status === 'violation');
    const clean = rules.filter(r => r.status === 'clean');
    if (violations.length) {
      html += '<div class="result-section"><h3>Rule Violations (' + violations.length + ')</h3>';
      violations.forEach(r => {
        html += `<div class="rule-item violation">
          <span class="rule-status">FAIL</span>
          <div class="rule-info">
            <div class="rule-id">${r.id}</div>
            <div class="rule-stmt">${escHtml(r.statement)}</div>
            <div class="rule-detail">${escHtml(r.details)}</div>
          </div>
        </div>`;
      });
      html += '</div>';
    }
    if (clean.length) {
      html += '<div class="result-section"><h3>Rules Passed (' + clean.length + ')</h3>';
      clean.forEach(r => {
        html += `<div class="rule-item clean">
          <span class="rule-status">PASS</span>
          <div class="rule-info">
            <div class="rule-id">${r.id}</div>
            <div class="rule-stmt">${escHtml(r.statement)}</div>
          </div>
        </div>`;
      });
      html += '</div>';
    }
  }

  // Agent Review button
  html += `<div class="result-section">
    <button class="agent-btn" id="agentBtn" onclick="runAgentReview()">
      Run Cogni Agent Loop (LLM)
    </button>
    <div id="agentResults"></div>
  </div>`;

  container.innerHTML = html;
}

async function runAgentReview() {
  const btn = document.getElementById('agentBtn');
  const out = document.getElementById('agentResults');
  btn.disabled = true;
  btn.textContent = 'Agent Loop Running...';
  out.innerHTML = '<div class="loading"><span class="spinner"></span>Looping: analyze → review → learn → re-analyze until converged...</div>';

  if (state.activeFile) {
    state.fileContents[state.activeFile] = document.getElementById('codeEditor').value;
  }
  const files = Object.assign({}, state.fileContents);
  const scenario = state.scenario || '';
  try {
    const res = await fetch('/api/agent_review', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({files, scenario}),
    });
    const data = await res.json();
    let html = '';

    if (data.error) {
      html = `<div style="color:#da3633;padding:8px">Error: ${escHtml(data.error)}</div>`;
    } else {
      const fp = data.false_positives || 0;
      const mb = data.missing_bugs || 0;
      const fc = data.fsm_consequences || 0;
      const sr = data.suggested_rules || [];

      const wt = data.waivers_total || 0;
      const lr = data.learned_rules_total || 0;
      const iters = data.iterations || [];
      const nIter = data.total_iterations || 1;
      const conv = data.converged ? 'Converged' : 'Max iterations reached';

      html += `<div style="padding:8px;font-size:13px;font-weight:600;color:${data.converged ? '#39d353' : '#d29922'}">
        ${conv} after ${nIter} iteration${nIter > 1 ? 's' : ''}
      </div>`;
      html += `<div style="padding:4px 8px;font-size:12px;color:#8b949e">
        Total FPs removed: <strong>${fp}</strong> |
        Missing bugs found: <strong>${mb}</strong> |
        FSM consequences: <strong>${fc}</strong>
      </div>`;
      html += `<div style="padding:2px 8px;font-size:11px;color:#6e7681">
        Persisted waivers: <strong>${wt}</strong> |
        Learned rules: <strong>${lr}</strong>
        <span style="color:#39d353"> Saved</span>
      </div>`;

      if (iters.length > 1) {
        html += '<div style="padding:4px 8px;margin-top:4px">';
        html += '<div style="font-size:11px;color:#58a6ff;font-weight:600;margin-bottom:4px">Iteration Log</div>';
        html += '<table style="width:100%;font-size:11px;color:#8b949e;border-collapse:collapse">';
        html += '<tr style="border-bottom:1px solid #21262d"><th style="text-align:left;padding:2px 6px">#</th><th>In</th><th>Out</th><th>FP</th><th>New Bugs</th><th>+Waivers</th><th>+Rules</th></tr>';
        iters.forEach(it => {
          html += `<tr style="border-bottom:1px solid #161b22">
            <td style="padding:2px 6px">${it.iteration}</td>
            <td style="text-align:center">${it.findings_in}</td>
            <td style="text-align:center">${it.findings_out}</td>
            <td style="text-align:center">${it.fp_removed}</td>
            <td style="text-align:center">${it.missing_found}</td>
            <td style="text-align:center;color:${it.new_waivers ? '#d29922' : '#484f58'}">${it.new_waivers}</td>
            <td style="text-align:center;color:${it.new_rules ? '#39d353' : '#484f58'}">${it.new_rules}</td>
          </tr>`;
        });
        html += '</table></div>';
      }

      // RTL Findings (static analysis — with waived ones struck through)
      const staticFindings = data.static_findings || [];
      const waived = data.waived_findings || [];
      const waivedKeys = new Set(waived.map(f => f.rule + ':' + f.line));

      if (staticFindings.length > 0) {
        const sGroups = {error:[], warning:[], info:[]};
        staticFindings.forEach(f => (sGroups[f.severity] || sGroups.info).push(f));
        for (const sev of ['error','warning','info']) {
          const items = sGroups[sev];
          if (!items.length) continue;
          const label = {error:'ERRORS',warning:'WARNINGS',info:'INFO'}[sev];
          html += `<div class="result-section"><h3>${label} (${items.length})</h3>`;
          items.forEach(f => {
            const sevCls = 'sev-' + f.severity;
            const isWaived = waivedKeys.has(f.rule + ':' + f.line);
            const wTag = isWaived ? '<span style="color:#d29922;font-size:10px;margin-left:6px">WAIVED</span>' : '';
            const wStyle = isWaived ? 'opacity:0.5;text-decoration:line-through;' : '';
            const conf = f.confidence || 0;
            const confCls = conf >= 90 ? 'conf-high' : conf >= 60 ? 'conf-med' : 'conf-low';
            const confTag = conf > 0 ? `<span class="conf-badge ${confCls}">${conf}%</span>` : '';
            html += `<div class="warning-item finding-item" style="cursor:pointer;${wStyle}"
              onclick="scrollToLine(${f.line})">
              <span class="sev-badge ${sevCls}">${f.severity}</span>
              ${confTag}
              <span class="cat">[${f.rule}]</span>${wTag}
              <span class="loc">${f.file}:<strong>${f.line}</strong></span>
              <div class="msg">${escHtml(f.message)}</div>
              ${f.synth_impact ? '<div class="rule-detail">Synth: ' + escHtml(f.synth_impact) + '</div>' : ''}
            </div>`;
          });
          html += '</div>';
        }
      }

      // Agent-discovered issues (missing bugs, FSM consequences)
      const agentOnly = (data.agent_findings || []).filter(f => f.rule.startsWith('AGENT_'));
      if (agentOnly.length > 0) {
        html += '<div class="result-section"><h3>AGENT-DISCOVERED ('+agentOnly.length+')</h3>';
        agentOnly.forEach(f => {
          const sevCls = 'sev-' + f.severity;
          html += `<div class="warning-item finding-item" style="cursor:pointer"
            onclick="scrollToLine(${f.line})">
            <span class="sev-badge ${sevCls}">${f.severity}</span>
            <span class="cat">[${f.rule}]</span>
            <span class="loc">${f.file}:<strong>${f.line}</strong></span>
            <div class="msg">${escHtml(f.message)}</div>
            ${f.synth_impact ? '<div class="rule-detail">Fix: ' + escHtml(f.synth_impact) + '</div>' : ''}
          </div>`;
        });
        html += '</div>';
      }

      if (sr.length > 0) {
        html += '<h3 style="margin:8px 0 4px">Suggested New Rules</h3>';
        sr.forEach(r => {
          html += `<div style="padding:4px 8px;font-size:12px;color:#c9d1d9">${escHtml(r)}</div>`;
        });
      }
    }
    out.innerHTML = html;
  } catch(e) {
    out.innerHTML = `<div style="color:#da3633;padding:8px">Request failed: ${e.message}</div>`;
  }
  btn.disabled = false;
  btn.textContent = 'Run Cogni Agent Loop (LLM)';
}

function toggleRaw(id) {
  const el = document.getElementById(id);
  el.style.display = el.style.display === 'block' ? 'none' : 'block';
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

init();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Cogni Lint Dashboard")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--ngrok", action="store_true",
                        help="Expose via ngrok tunnel")
    parser.add_argument("--ngrok-token", default=None,
                        help="ngrok auth token (or set NGROK_AUTHTOKEN)")
    args = parser.parse_args()

    if args.ngrok:
        try:
            from pyngrok import ngrok, conf
            token = args.ngrok_token or os.environ.get("NGROK_AUTHTOKEN")
            if token:
                conf.get_default().auth_token = token
            tunnel = ngrok.connect(args.port)
            print(f"\n  ngrok tunnel: {tunnel.public_url}\n")
        except ImportError:
            print("  [warn] pyngrok not installed, skipping ngrok")

    print(f"  Lint Dashboard: http://localhost:{args.port}")
    print(f"  Scenarios: {[s['name'] for s in _list_scenarios()]}")
    print()
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()

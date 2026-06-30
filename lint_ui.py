"""
Cogni Lint Dashboard — three-panel UI for RTL editing and cross-tool validation.

Left panel:   RTL code editor with scenario selector
Middle panel:  VCS lint results (warnings, rule pass/fail)
Right panel:   Verilator lint results (warnings, rule pass/fail)
Run button:    Executes both tools on the current code and shows results.

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


def _run_verilator(rtl_files, top=None):
    """Run verilator --lint-only and return structured results."""
    import subprocess, re
    cmd = ["verilator", "--lint-only", "-Wall", "-Wno-fatal"]
    if top:
        cmd.extend(["--top-module", top])
    cmd.extend(rtl_files)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = result.stdout + result.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"error": "verilator not found or timed out", "warnings": [], "raw": ""}

    warnings = []
    pattern = re.compile(
        r"%(?:Warning|Error)-(\w+):\s*(.*?):(\d+):\d+:\s*(.*?)(?:\n|$)")
    for m in pattern.finditer(output):
        warnings.append({
            "category": m.group(1),
            "file": os.path.basename(m.group(2)),
            "line": int(m.group(3)),
            "message": m.group(4).strip(),
        })

    categories = {}
    for w in warnings:
        cat = w["category"]
        categories[cat] = categories.get(cat, 0) + 1

    return {
        "warnings": warnings,
        "categories": categories,
        "total": len(warnings),
        "raw": output[-3000:] if len(output) > 3000 else output,
    }


def _run_vcs(rtl_files, top=None):
    """Run VCS lint and return structured results."""
    import subprocess, re
    work_dir = tempfile.mkdtemp(prefix="cogni_lint_ui_vcs_")
    abs_files = [os.path.abspath(f) for f in rtl_files]

    warnings = []
    raw_parts = []
    lint_pattern = re.compile(r"Lint-\[(\w+)\]\s*(.*?)(?:\n(?:\S)|\Z)", re.DOTALL)
    detail_pattern = re.compile(
        r"Lint-\[(\w+)\][^\n]*\n([^\n]*?),\s*(\d+)\n(.*?)(?=\n\n|\nLint-|\nParsing|\nTop Level|\nStarting|\nCPU|\Z)",
        re.DOTALL)

    # Phase 1: vlogan
    vlogan_cmd = ["vlogan", "-sverilog", "-full64", "+warn=all"] + abs_files
    try:
        result = subprocess.run(
            vlogan_cmd, capture_output=True, text=True,
            timeout=60, cwd=work_dir)
        output = result.stdout + result.stderr
        raw_parts.append(output)
        for m in detail_pattern.finditer(output):
            warnings.append({
                "category": m.group(1),
                "file": os.path.basename(m.group(2).strip()),
                "line": int(m.group(3)),
                "message": m.group(4).strip().replace("\n", " "),
            })
        # Fallback: simple pattern
        if not warnings:
            for m in lint_pattern.finditer(output):
                warnings.append({
                    "category": m.group(1),
                    "file": "",
                    "line": 0,
                    "message": m.group(2).strip().replace("\n", " "),
                })
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"error": "vlogan not found or timed out", "warnings": [], "raw": ""}

    # Phase 2: vcs elaboration
    vcs_cmd = ["vcs", "-sverilog", "-full64", "+lint=all",
               "+warn=all", "-notice"] + abs_files
    if top:
        vcs_cmd.extend(["-top", top])
    try:
        result = subprocess.run(
            vcs_cmd, capture_output=True, text=True,
            timeout=60, cwd=work_dir)
        output = result.stdout + result.stderr
        raw_parts.append(output)
        for m in detail_pattern.finditer(output):
            warnings.append({
                "category": m.group(1),
                "file": os.path.basename(m.group(2).strip()),
                "line": int(m.group(3)),
                "message": m.group(4).strip().replace("\n", " "),
            })
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Clean up
    shutil.rmtree(work_dir, ignore_errors=True)

    categories = {}
    for w in warnings:
        cat = w["category"]
        categories[cat] = categories.get(cat, 0) + 1

    raw = "\n---\n".join(raw_parts)
    return {
        "warnings": warnings,
        "categories": categories,
        "total": len(warnings),
        "raw": raw[-3000:] if len(raw) > 3000 else raw,
    }


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


def _get_verilator_measurements(rtl_files, top=None):
    """Run Verilator lint and return measurements dict."""
    from agent.fix_verify import lint_counts
    from adapters.rtl.verilator.oracle import _CLASS_TO_KEY, _VERILATOR_COVERED_KEYS
    classes = lint_counts(rtl_files, top=top,
                          extra_args=["-Wall", "-Wno-fatal"])
    measurements = {k: 0 for k in _VERILATOR_COVERED_KEYS}
    for cls, n in classes.items():
        key = _CLASS_TO_KEY.get(cls)
        if key:
            measurements[key] = measurements.get(key, 0) + n
    return measurements


def _get_vcs_measurements(rtl_files, top=None):
    """Run VCS lint and return measurements dict."""
    from adapters.rtl.vcs.oracle import _run_vcs_lint, _VCS_CLASS_TO_RTL_KEY, _VCS_COVERED_KEYS
    classes = _run_vcs_lint(rtl_files, top=top)
    measurements = {k: 0 for k in _VCS_COVERED_KEYS}
    for cls, n in classes.items():
        key = _VCS_CLASS_TO_RTL_KEY.get(cls)
        if key:
            measurements[key] = measurements.get(key, 0) + n
    return measurements


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

    verilator_result = _run_verilator(all_files)
    vcs_result = _run_vcs(all_files)

    verilator_meas = _get_verilator_measurements(all_files)
    vcs_meas = _get_vcs_measurements(all_files)

    verilator_rules = _check_rules_against(verilator_meas)
    vcs_rules = _check_rules_against(vcs_meas)

    # Cogni RTL Analyzer (pure Python, no commercial tool)
    from agent.rtl_analyzer import analyze_design
    cogni_result = analyze_design(all_files)
    cogni_data = {
        "findings": [
            {"rule": f.rule, "severity": f.severity, "file": f.file,
             "line": f.line, "message": f.message,
             "synth_impact": f.synth_impact}
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
        "verilator": verilator_result,
        "vcs": vcs_result,
        "cogni": cogni_data,
        "verilator_rules": verilator_rules,
        "vcs_rules": vcs_rules,
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

    from agent.rtl_analyzer import agent_review_loop

    try:
        loop_result = agent_review_loop(
            tmp_paths, rtl_sources=all_files, max_iterations=3)
        reviewed = loop_result["result"]
        iters = loop_result["iterations"]

        suggested = reviewed.measurements.get("cogni.agent.suggested_rules", [])
        agent_findings = [
            {"rule": f.rule, "severity": f.severity,
             "file": f.file, "line": f.line,
             "message": f.message, "synth_impact": f.synth_impact or ""}
            for f in reviewed.findings
        ]

        total_fp = sum(it["fp_removed"] for it in iters)
        total_missing = sum(it["missing_found"] for it in iters)
        total_fsm = sum(it["fsm_consequences"] for it in iters)

        resp = {
            "false_positives": total_fp,
            "missing_bugs": total_missing,
            "fsm_consequences": total_fsm,
            "suggested_rules": suggested,
            "agent_findings": agent_findings,
            "waivers_total": iters[-1]["total_waivers"] if iters else 0,
            "learned_rules_total": iters[-1]["total_rules"] if iters else 0,
            "iterations": iters,
            "converged": loop_result["converged"],
            "total_iterations": loop_result["total_iterations"],
        }
    except Exception as e:
        resp = {"error": str(e), "agent_findings": []}

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

  <!-- PANEL 3: VCS Results -->
  <div class="panel">
    <div class="panel-header">
      <span class="tool-icon" style="background:#d29922"></span>
      VCS
      <span class="badge" id="vcsBadge">--</span>
    </div>
    <div class="panel-body" id="vcsResults">
      <div class="empty-state">
        <div class="icon">&#9881;</div>
        Run to see VCS lint results
      </div>
    </div>
  </div>

  <!-- PANEL 4: Verilator Results -->
  <div class="panel">
    <div class="panel-header">
      <span class="tool-icon" style="background:#238636"></span>
      Verilator
      <span class="badge" id="verilatorBadge">--</span>
    </div>
    <div class="panel-body" id="verilatorResults">
      <div class="empty-state">
        <div class="icon">&#9881;</div>
        Run to see Verilator lint results
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
  ['cogniResults', 'vcsResults', 'verilatorResults'].forEach(id => {
    document.getElementById(id).innerHTML =
      '<div class="empty-state"><div class="icon">&#9881;</div>Click Run to analyze</div>';
  });
  ['cogniBadge', 'vcsBadge', 'verilatorBadge'].forEach(id => {
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
  document.getElementById('statusText').textContent = 'Running Cogni + Verilator + VCS...';

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
  document.getElementById('vcsResults').innerHTML =
    '<div class="loading"><span class="spinner"></span>Running VCS lint...</div>';
  document.getElementById('verilatorResults').innerHTML =
    '<div class="loading"><span class="spinner"></span>Running Verilator lint...</div>';

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
    renderVCSResults(data.vcs, data.vcs_rules);
    renderVerilatorResults(data.verilator, data.verilator_rules);
    const cogniTotal = (data.cogni || {}).total || 0;
    const vcsViol = (data.vcs_rules || []).filter(r => r.status === 'violation').length;
    const verViol = (data.verilator_rules || []).filter(r => r.status === 'violation').length;
    document.getElementById('statusText').textContent =
      `Cogni=${cogniTotal} findings, ` +
      `VCS=${data.vcs.total} warnings (${vcsViol} rule violations), ` +
      `Verilator=${data.verilator.total} warnings (${verViol} rule violations)`;
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
function renderToolResults(containerId, badgeId, result, toolName, rules) {
  const container = document.getElementById(containerId);
  const badge = document.getElementById(badgeId);

  if (result.error) {
    container.innerHTML = `<div class="empty-state">${result.error}</div>`;
    badge.textContent = 'error';
    badge.className = 'badge badge-error';
    return;
  }

  const ruleViolations = (rules || []).filter(r => r.status === 'violation');
  const totalIssues = result.total + ruleViolations.length;
  badge.textContent = result.total + ' warnings';
  badge.className = totalIssues > 0 ? 'badge badge-error' : 'badge badge-clean';

  let html = '';

  // Summary
  if (Object.keys(result.categories).length > 0 || rules) {
    html += '<div class="result-section"><h3>Summary</h3><div class="summary-bar">';
    for (const [cat, count] of Object.entries(result.categories || {})) {
      html += `<span class="summary-stat">
        <span class="dot dot-yellow"></span>${cat}: ${count}
      </span>`;
    }
    if (rules) {
      const v = rules.filter(r => r.status === 'violation').length;
      const c = rules.filter(r => r.status === 'clean').length;
      const na = rules.filter(r => r.status === 'n/a').length;
      html += `<span class="summary-stat"><span class="dot dot-red"></span>Rules FAIL: ${v}</span>`;
      html += `<span class="summary-stat"><span class="dot dot-green"></span>Rules PASS: ${c}</span>`;
      if (na > 0) html += `<span class="summary-stat"><span class="dot dot-gray"></span>N/A: ${na}</span>`;
    }
    html += '</div></div>';
  }

  // Warnings
  html += '<div class="result-section"><h3>Lint Warnings (' + result.total + ')</h3>';
  if (result.warnings.length === 0) {
    html += '<div style="padding:8px;color:#238636;font-size:12px">No warnings found</div>';
  }
  result.warnings.forEach(w => {
    html += `<div class="warning-item">
      <span class="cat">[${w.category}]</span>
      ${w.file ? `<span class="loc">${w.file}:${w.line}</span>` : ''}
      <div class="msg">${escHtml(w.message)}</div>
    </div>`;
  });
  html += '</div>';

  // Rule checks
  if (rules && rules.length) {
    const violations = rules.filter(r => r.status === 'violation');
    const clean = rules.filter(r => r.status === 'clean');
    const na = rules.filter(r => r.status === 'n/a');

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

    if (na.length) {
      html += '<div class="result-section"><h3>Not Measured (' + na.length + ')</h3>';
      na.forEach(r => {
        html += `<div class="rule-item skipped">
          <span class="rule-status">N/A</span>
          <div class="rule-info">
            <div class="rule-id">${r.id}</div>
            <div class="rule-stmt">${escHtml(r.statement)}</div>
          </div>
        </div>`;
      });
      html += '</div>';
    }
  }

  // Raw output
  if (result.raw) {
    const rawId = toolName + '_raw';
    html += `<div class="result-section">
      <button class="raw-toggle" onclick="toggleRaw('${rawId}')">Show raw output</button>
      <div class="raw-output" id="${rawId}">${escHtml(result.raw)}</div>
    </div>`;
  }

  container.innerHTML = html;
}

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
  const findings = result.findings || [];
  state.lineMarkers = buildLineMarkers(findings);
  updateLineNumbers();

  if (findings.length > 0) {
    const groups = {error:[], warning:[], info:[]};
    findings.forEach(f => (groups[f.severity] || groups.info).push(f));
    const sectionNames = {error:'ERRORS', warning:'WARNINGS', info:'INFO'};
    for (const sev of ['error','warning','info']) {
      const items = groups[sev];
      if (!items.length) continue;
      const label = sectionNames[sev];
      html += `<div class="result-section"><h3>${label} (${items.length})</h3>`;
      items.forEach(f => {
        const sevCls = 'sev-' + f.severity;
        const synthTip = f.synth_impact ? `<div class="rule-detail">Synth: ${escHtml(f.synth_impact)}</div>` : '';
        html += `<div class="warning-item finding-item" data-line="${f.line}" style="cursor:pointer"
          onclick="scrollToLine(${f.line})">
          <span class="sev-badge ${sevCls}">${f.severity}</span>
          <span class="cat">[${f.rule}]</span>
          <span class="loc">${f.file}:<strong>${f.line}</strong></span>
          <div class="msg">${escHtml(f.message)}</div>
          ${synthTip}
        </div>`;
      });
      html += '</div>';
    }
  } else {
    html += '<div class="result-section"><h3>RTL Findings</h3>';
    html += '<div style="padding:8px;color:#238636;font-size:12px">No issues found</div>';
    html += '</div>';
  }

  // ---- SYNTHESIS SCORECARD ----
  const synth = (result.measurements || {})['cogni.synth'];
  if (synth) {
    const t = synth.timing || {};
    const pwr = synth.power || {};
    const fpga = synth.fpga_resources || {};
    const fsm = synth.fsm || {};
    const mem = synth.memory || {};
    const gb = synth.gate_breakdown || {};

    const gateVal = (synth.total_gates || 0).toLocaleString();
    const freqVal = (t.max_freq_mhz || 0).toFixed(0);
    const pathVal = (t.critical_path_ns || 0).toFixed(2);
    const gatingPct = (pwr.clock_gating_pct || 0).toFixed(0);
    const gatingCls = gatingPct >= 80 ? 'val-good' : (gatingPct >= 40 ? 'val-warn' : 'val-bad');

    html += `<div class="result-section">
      <h3>SYNTHESIS SCORECARD</h3>
      <div class="synth-scorecard">
        <div class="scorecard-grid">
          <div class="scorecard-item">
            <span class="scorecard-label">Gate Equivalents</span>
            <span class="scorecard-value">${gateVal}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Flip-Flop Bits</span>
            <span class="scorecard-value">${synth.ff_bits || 0}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Critical Path</span>
            <span class="scorecard-value">${pathVal} ns</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Max Frequency</span>
            <span class="scorecard-value">~${freqVal} MHz</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Clock Gating</span>
            <span class="scorecard-value ${gatingCls}">${gatingPct}%</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Comb Depth</span>
            <span class="scorecard-value">${t.max_comb_depth || 0} levels</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Sequential</span>
            <span class="scorecard-value">${(gb.sequential || 0).toLocaleString()} gates</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Combinational</span>
            <span class="scorecard-value">${(gb.combinational || 0).toLocaleString()} gates</span>
          </div>`;

    if ((mem.total_bits || 0) > 0) {
      html += `
          <div class="scorecard-item">
            <span class="scorecard-label">Memory</span>
            <span class="scorecard-value">${(mem.total_bits || 0).toLocaleString()} bits</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">Memory Gates</span>
            <span class="scorecard-value">${(gb.memory || 0).toLocaleString()}</span>
          </div>`;
    }

    if (fsm.states > 0) {
      html += `
          <div class="scorecard-item">
            <span class="scorecard-label">FSM States</span>
            <span class="scorecard-value">${fsm.states} (${fsm.encoding_bits}-bit)</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">FSM Min Bits</span>
            <span class="scorecard-value">${fsm.min_bits}</span>
          </div>`;
    }

    html += `
          <div class="scorecard-item">
            <span class="scorecard-label">FPGA LUTs</span>
            <span class="scorecard-value">~${(fpga.lut_count || 0).toLocaleString()}</span>
          </div>
          <div class="scorecard-item">
            <span class="scorecard-label">FPGA FFs</span>
            <span class="scorecard-value">${fpga.ff_count || 0}</span>
          </div>`;

    if ((fpga.dsp_blocks || 0) > 0) {
      html += `
          <div class="scorecard-item">
            <span class="scorecard-label">FPGA DSPs</span>
            <span class="scorecard-value">${fpga.dsp_blocks}</span>
          </div>`;
    }
    if ((fpga.bram_blocks || 0) > 0) {
      html += `
          <div class="scorecard-item">
            <span class="scorecard-label">FPGA BRAMs</span>
            <span class="scorecard-value">${fpga.bram_blocks}</span>
          </div>`;
    }

    html += `
        </div>
      </div>
    </div>`;
  }

  // ---- SYNTHESIS PREDICTIONS (grouped by category) ----
  const preds = result.predictions || [];
  if (preds.length > 0) {
    html += '<div class="result-section"><h3>SYNTHESIS PREDICTIONS</h3>';

    const catOrder = ['optimization', 'area', 'timing', 'power', 'functional', 'latch', 'clean'];
    const catLabels = {
      optimization: 'Optimization (Const Prop / Dead Code)',
      area: 'Area & Resources', timing: 'Timing', power: 'Power',
      functional: 'Functional Risks', latch: 'Latch Inference', clean: 'Status'
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

      const findings = data.agent_findings || [];
      if (findings.length > 0) {
        html += '<h3 style="margin:8px 0 4px">Agent Findings</h3>';
        findings.forEach(f => {
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

function renderVCSResults(result, rules) {
  renderToolResults('vcsResults', 'vcsBadge', result, 'vcs', rules);
}

function renderVerilatorResults(result, rules) {
  renderToolResults('verilatorResults', 'verilatorBadge', result, 'verilator', rules);
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

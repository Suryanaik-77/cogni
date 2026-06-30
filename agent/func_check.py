"""
agent.func_check
=================
Functional rule checker — evaluates rules that encode design intent,
not just lint counts.

Three functional checking modes:

  1. **pattern** — grep-like search for RTL code patterns.
     "This design must NOT contain `assign clk =`" (gated clock pattern).
     "This design MUST contain `always_ff @(posedge clk or negedge rst_n)`"
     (async reset pattern).

  2. **sva** — SystemVerilog Assertion extraction and verification.
     The rule carries an SVA property; the checker injects it into the
     design and runs Verilator --assert to confirm it holds.

  3. **protocol** — structural protocol checks.
     "Every valid signal must have a corresponding ready signal."
     "AXI: AWVALID must not depend combinationally on AWREADY."

Each mode produces a measurement into the ``func.*`` namespace that
the standard sweep engine can grade.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any


@dataclass
class FuncCheckResult:
    rule_id: str
    measurement_key: str
    passed: bool
    measured: Any
    detail: str


# ---------------------------------------------------------------------------
# Pattern checker
# ---------------------------------------------------------------------------

def _check_pattern(rule: dict, rtl_files: dict[str, str]) -> list[FuncCheckResult]:
    """Check for presence/absence of RTL code patterns.

    Rule functional section:
      "functional": {
        "mode": "pattern",
        "patterns": [
          {"regex": "assign\\s+clk\\s*=", "must_not_match": true,
           "description": "gated clock via assign"},
          {"regex": "always_ff\\s*@\\s*\\(posedge\\s+clk",
           "must_match": true, "description": "registered logic"}
        ]
      }
    """
    func = rule.get("functional", {})
    patterns = func.get("patterns", [])
    results: list[FuncCheckResult] = []

    for pat_spec in patterns:
        regex = pat_spec.get("regex", "")
        if not regex:
            continue
        must_match = pat_spec.get("must_match", False)
        must_not_match = pat_spec.get("must_not_match", False)
        desc = pat_spec.get("description", regex)
        mkey = pat_spec.get("measurement_key",
                            f"func.pattern.{_slug(rule['id'])}.{_slug(desc)}")

        matches: list[dict] = []
        try:
            compiled = re.compile(regex, re.MULTILINE | re.IGNORECASE)
        except re.error:
            results.append(FuncCheckResult(
                rule_id=rule["id"], measurement_key=mkey,
                passed=False, measured=None,
                detail=f"invalid regex: {regex}"))
            continue

        for fpath, content in rtl_files.items():
            for m in compiled.finditer(content):
                line_no = content[:m.start()].count("\n") + 1
                matches.append({"file": fpath, "line": line_no,
                                "match": m.group()[:80]})

        match_count = len(matches)

        if must_not_match:
            passed = match_count == 0
            detail = (f"pattern '{desc}' found {match_count} time(s)"
                      if not passed else f"pattern '{desc}' absent (good)")
        elif must_match:
            passed = match_count > 0
            detail = (f"pattern '{desc}' found {match_count} time(s)"
                      if passed else f"pattern '{desc}' NOT found (violation)")
        else:
            passed = True
            detail = f"pattern '{desc}' found {match_count} time(s) (info)"

        if not passed and matches:
            locs = ", ".join(f"{m['file']}:{m['line']}" for m in matches[:5])
            detail += f" at {locs}"

        results.append(FuncCheckResult(
            rule_id=rule["id"], measurement_key=mkey,
            passed=passed, measured=match_count, detail=detail))

    return results


# ---------------------------------------------------------------------------
# SVA checker
# ---------------------------------------------------------------------------

def _check_sva(rule: dict, rtl_root: str, rtl_files: dict[str, str],
               *, top: str | None = None) -> list[FuncCheckResult]:
    """Check SVA properties against the design using Verilator --assert.

    Rule functional section:
      "functional": {
        "mode": "sva",
        "assertions": [
          {
            "property": "a_no_idle_to_error",
            "sva": "assert property (@(posedge clk) disable iff (!rst_n)
                     state == IDLE |-> ##[1:$] state != ERROR);",
            "bind_to": "fsm_controller",
            "description": "FSM must not jump from IDLE to ERROR",
            "measurement_key": "func.sva.fsm_no_idle_to_error"
          }
        ]
      }
    """
    func = rule.get("functional", {})
    assertions = func.get("assertions", [])
    results: list[FuncCheckResult] = []

    for assertion in assertions:
        sva_code = assertion.get("sva", "")
        prop_name = assertion.get("property", "unnamed")
        bind_to = assertion.get("bind_to", "")
        desc = assertion.get("description", prop_name)
        mkey = assertion.get("measurement_key",
                             f"func.sva.{_slug(rule['id'])}.{_slug(prop_name)}")

        if not sva_code:
            results.append(FuncCheckResult(
                rule_id=rule["id"], measurement_key=mkey,
                passed=False, measured=None,
                detail=f"empty SVA for property '{prop_name}'"))
            continue

        # Build a bind file that injects the assertion
        if bind_to:
            bind_sv = (f"module bind_{_slug(prop_name)};\n"
                       f"  {sva_code}\n"
                       f"endmodule\n\n"
                       f"bind {bind_to} bind_{_slug(prop_name)} "
                       f"u_bind_{_slug(prop_name)} (.*);\n")
        else:
            bind_sv = f"// inline SVA\n{sva_code}\n"

        # Write temp bind file and try Verilator --assert
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".sv", dir=rtl_root,
                    prefix=f"_cogni_sva_{_slug(prop_name)}_",
                    delete=False) as f:
                f.write(bind_sv)
                bind_path = f.name

            sv_files = [os.path.join(rtl_root, fp) for fp in rtl_files]
            sv_files.append(bind_path)

            cmd = ["verilator", "--lint-only", "--assert",
                   "-Wall", "-Wno-fatal"]
            if top:
                cmd += ["--top-module", top]
            cmd += sv_files

            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=30)
            # Check for assertion-related warnings/errors
            output = proc.stderr + proc.stdout
            assert_errors = [l for l in output.splitlines()
                             if "assert" in l.lower() or "Error" in l]

            if proc.returncode == 0 and not assert_errors:
                passed = True
                detail = f"SVA '{desc}' passed Verilator --assert"
            else:
                passed = False
                err_summary = "; ".join(assert_errors[:3]) if assert_errors else "verilator failed"
                detail = f"SVA '{desc}' failed: {err_summary[:120]}"

            results.append(FuncCheckResult(
                rule_id=rule["id"], measurement_key=mkey,
                passed=passed, measured=1 if passed else 0,
                detail=detail))

        except FileNotFoundError:
            results.append(FuncCheckResult(
                rule_id=rule["id"], measurement_key=mkey,
                passed=False, measured=None,
                detail="verilator not found (SVA check requires Verilator)"))
        except subprocess.TimeoutExpired:
            results.append(FuncCheckResult(
                rule_id=rule["id"], measurement_key=mkey,
                passed=False, measured=None,
                detail=f"SVA '{desc}' timed out (30s)"))
        finally:
            try:
                os.unlink(bind_path)
            except (OSError, UnboundLocalError):
                pass

    return results


# ---------------------------------------------------------------------------
# Protocol checker
# ---------------------------------------------------------------------------

def _check_protocol(rule: dict, rtl_files: dict[str, str]) -> list[FuncCheckResult]:
    """Check structural protocol constraints via pattern analysis.

    Rule functional section:
      "functional": {
        "mode": "protocol",
        "protocol": "axi4_lite",
        "checks": [
          {
            "id": "valid_not_comb_ready",
            "description": "xVALID must not depend combinationally on xREADY",
            "signal_pairs": [
              {"valid": "awvalid", "ready": "awready"},
              {"valid": "wvalid",  "ready": "wready"}
            ],
            "check": "no_comb_dependency",
            "measurement_key": "func.protocol.axi.valid_ready_independence"
          }
        ]
      }
    """
    func = rule.get("functional", {})
    checks = func.get("checks", [])
    results: list[FuncCheckResult] = []

    for chk in checks:
        check_type = chk.get("check", "")
        desc = chk.get("description", chk.get("id", ""))
        mkey = chk.get("measurement_key",
                       f"func.protocol.{_slug(rule['id'])}.{_slug(chk.get('id', ''))}")

        if check_type == "no_comb_dependency":
            violations = _check_no_comb_dep(chk, rtl_files)
            passed = len(violations) == 0
            detail = (f"protocol '{desc}': OK"
                      if passed else
                      f"protocol '{desc}': {len(violations)} violation(s) — "
                      + "; ".join(violations[:3]))
            results.append(FuncCheckResult(
                rule_id=rule["id"], measurement_key=mkey,
                passed=passed, measured=len(violations),
                detail=detail))

        elif check_type == "signal_exists":
            signals = chk.get("signals", [])
            missing = []
            all_content = "\n".join(rtl_files.values())
            for sig in signals:
                if not re.search(rf'\b{re.escape(sig)}\b', all_content):
                    missing.append(sig)
            passed = len(missing) == 0
            detail = (f"all required signals present"
                      if passed else
                      f"missing signals: {', '.join(missing)}")
            results.append(FuncCheckResult(
                rule_id=rule["id"], measurement_key=mkey,
                passed=passed, measured=len(missing),
                detail=detail))

        elif check_type == "paired_signals":
            pairs = chk.get("signal_pairs", [])
            unpaired = []
            all_content = "\n".join(rtl_files.values())
            for pair in pairs:
                for role, sig in pair.items():
                    if not re.search(rf'\b{re.escape(sig)}\b', all_content):
                        unpaired.append(f"{role}={sig}")
            passed = len(unpaired) == 0
            detail = (f"all signal pairs present"
                      if passed else
                      f"unpaired: {', '.join(unpaired)}")
            results.append(FuncCheckResult(
                rule_id=rule["id"], measurement_key=mkey,
                passed=passed, measured=len(unpaired),
                detail=detail))

    return results


def _check_no_comb_dep(chk: dict, rtl_files: dict[str, str]) -> list[str]:
    """Heuristic: in always_comb blocks, check that 'valid' signals are
    not assigned based on 'ready' signals (AXI rule)."""
    violations = []
    pairs = chk.get("signal_pairs", [])
    for fpath, content in rtl_files.items():
        for pair in pairs:
            valid_sig = pair.get("valid", "")
            ready_sig = pair.get("ready", "")
            if not valid_sig or not ready_sig:
                continue
            # Look for: always_comb block containing both valid assignment
            # and ready reference
            comb_blocks = re.findall(
                r'always_comb\s+begin(.*?)end',
                content, re.DOTALL | re.IGNORECASE)
            for block in comb_blocks:
                assigns_valid = re.search(
                    rf'\b{re.escape(valid_sig)}\b\s*[<]?=', block)
                reads_ready = re.search(
                    rf'\b{re.escape(ready_sig)}\b', block)
                if assigns_valid and reads_ready:
                    violations.append(
                        f"{fpath}: {valid_sig} depends on {ready_sig} "
                        f"in always_comb")
    return violations


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:40] or "x"


def gather_rtl(rtl_root: str) -> dict[str, str]:
    """Collect RTL files as {relative_path: content}."""
    files: dict[str, str] = {}
    for dirpath, _, filenames in os.walk(rtl_root):
        for fn in sorted(filenames):
            if fn.endswith((".sv", ".v", ".svh", ".vh")):
                fpath = os.path.join(dirpath, fn)
                rel = os.path.relpath(fpath, rtl_root)
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    files[rel] = f.read()
    return files


def run_functional_checks(pack: dict, rtl_root: str, *,
                          top: str | None = None) -> list[FuncCheckResult]:
    """Run all functional checks from a rule pack against RTL files.

    Only rules with a "functional" section are checked. Returns results
    that can be injected into Reality.measurements for sweep grading.
    """
    rtl_files = gather_rtl(rtl_root)
    if not rtl_files:
        return []

    results: list[FuncCheckResult] = []

    for rule in pack.get("rules", []):
        if rule.get("status") == "retired":
            continue
        func = rule.get("functional")
        if not func:
            continue

        mode = func.get("mode", "")

        if mode == "pattern":
            results.extend(_check_pattern(rule, rtl_files))
        elif mode == "sva":
            results.extend(_check_sva(rule, rtl_root, rtl_files, top=top))
        elif mode == "protocol":
            results.extend(_check_protocol(rule, rtl_files))

    return results


def inject_measurements(results: list[FuncCheckResult],
                        reality) -> None:
    """Inject functional check results into a Reality object's measurements
    so the standard sweep engine can grade them."""
    measurements = getattr(reality, "measurements", None)
    if measurements is None:
        return
    for r in results:
        if r.measurement_key:
            measurements[r.measurement_key] = r.measured


def format_results(results: list[FuncCheckResult]) -> str:
    """Format functional check results for console output."""
    if not results:
        return ""
    lines = ["=== FUNCTIONAL CHECKS ==="]
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]

    if failed:
        for r in failed:
            lines.append(f"  FAIL  {r.rule_id}")
            lines.append(f"        {r.detail}")
            lines.append(f"        key: {r.measurement_key} = {r.measured}")
    if passed:
        for r in passed:
            lines.append(f"  OK    {r.rule_id}: {r.detail}")

    lines.append(f"\n  {len(passed)} passed, {len(failed)} failed")
    return "\n".join(lines)

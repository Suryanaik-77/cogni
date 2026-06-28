"""
agent.optimizer
===============
RTL optimization for synthesis readiness.

After the RTL is lint-clean (no Verilator violations), this module asks
an LLM to propose synthesis-friendly optimizations — things Verilator
can't flag but that matter for area, timing, and power at gate-level:

  * Redundant or dead logic that synthesis will discard anyway
  * Inefficient mux trees / priority chains that synthesize to long paths
  * Missing reset values that cause unpredictable gate-level behaviour
  * Combinational loops that lint won't flag but synth tools hate
  * Constants that could be parameters for better synthesis elaboration

The optimizer MUST NOT introduce new lint warnings. After optimization,
the caller re-runs Verilator to confirm. If new warnings appear, the
optimized file is rejected and the pre-optimization version is kept.

Architecture mirrors agent/fixer.py: LLM proposes, reality (Verilator)
confirms. No optimized code is accepted without re-verification.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from agent.llm import LLMCall
from agent import llm as _llm
from agent.llm.transports import run_briefs_concurrently


@dataclass
class OptimizationProposal:
    target_file: str
    original_content: str
    optimized_content: str
    changes_summary: list[str]
    rationale: str
    confidence: str


OPTIMIZE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["optimized_file", "changes_summary", "rationale", "confidence"],
    "properties": {
        "optimized_file":   {"type": "string"},
        "changes_summary":  {"type": "array", "items": {"type": "string"}},
        "rationale":        {"type": "string"},
        "confidence":       {"type": "string",
                             "enum": ["high", "medium", "low"]},
        "decision":         {"type": "string",
                             "enum": ["optimize", "no_change"]},
    },
}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:60] or "x"


def _optimize_call(rel_path: str, content: str, lint_clean: dict,
                    memory_context: str = "") -> LLMCall:
    prompt = """# Role: RTL Optimizer for Synthesis

You are an RTL optimization expert preparing code for gate-level synthesis.
The RTL has already passed Verilator lint (zero warnings). Your job is to
propose optimizations that improve synthesis quality WITHOUT introducing
any new lint warnings.

## What to optimize

Focus on changes that improve the SYNTHESIZED result:

1. **Dead / redundant logic**: assignments that can never be reached,
   signals assigned but never read, conditions that are always true/false.
2. **Synthesis-unfriendly patterns**: priority-encoded if-else chains that
   should be parallel case statements, overly wide operations that could
   use narrower types.
3. **Missing reset values**: flip-flops without explicit reset produce
   undefined gate-level state; add reset assignments where missing.
4. **Inefficient mux structures**: nested ternaries that synthesize to
   cascaded muxes; rewrite as case or parallel if for better area/timing.
5. **Constant propagation opportunities**: magic numbers that should be
   localparam for better synthesis elaboration.
6. **Clock-domain and timing**: unnecessary register stages, combinational
   paths that could be pipelined.

## Discipline

- Return the COMPLETE optimized file in `optimized_file`. Every byte must
  be there — this replaces the original.
- If no optimization is warranted, set `decision: "no_change"` and return
  the original file unchanged in `optimized_file`.
- Do NOT fix lint issues (there are none). Do NOT refactor for style.
  Only make changes that improve synthesis outcome.
- Keep the module interface (ports, parameters) IDENTICAL.
- Each item in `changes_summary` should be one sentence describing one
  specific change and WHY it helps synthesis.
- `confidence`: "high" = these are textbook improvements; "medium" = likely
  beneficial but design-dependent; "low" = speculative.

## Inputs

Read `inputs.json`. It contains:
  - `file_path`    : relative path of the RTL file
  - `file_content` : the lint-clean source to optimize
  - `lint_status`  : current Verilator lint counts (all zero)
  - `memory`       : (optional) prior agent knowledge — past optimization
                     attempts, what was accepted/rejected before.
"""
    inputs: dict[str, Any] = {
        "file_path": rel_path,
        "file_content": content,
        "lint_status": lint_clean,
    }
    if memory_context:
        inputs["memory"] = memory_context
    return LLMCall(
        name=f"optimize.{_slug(rel_path)}",
        model=_llm.MODEL_OPUS,
        role="rtl_optimizer",
        prompt=prompt,
        schema=OPTIMIZE_SCHEMA,
        inputs=inputs,
    )


def _write_brief(call: LLMCall, run_dir: str) -> dict:
    paths = call.write_brief(run_dir)
    return {
        "name": call.name, "model": call.model, "role": call.role,
        "prompt": paths["prompt"], "schema": paths["schema"],
        "inputs": paths["inputs"], "output": paths["output"],
    }


def _read_output(call: LLMCall, run_dir: str) -> dict | None:
    out = os.path.join(run_dir, "llm_calls", call.name, "output.json")
    if not os.path.exists(out):
        return None
    with open(out) as f:
        return json.load(f)


async def optimize_rtl(rtl_root: str, run_dir: str, *,
                       lint_counts: dict,
                       memory_context: str = "",
                       concurrency: int = 4) -> list[OptimizationProposal]:
    """Propose synthesis-friendly optimizations for each RTL file.

    Only called when RTL is already lint-clean. Returns proposals for
    files where the optimizer found improvements. The caller must
    re-verify with Verilator before accepting any optimized file.
    """
    os.makedirs(run_dir, exist_ok=True)

    files: dict[str, str] = {}
    for dirpath, _, filenames in os.walk(rtl_root):
        for fn in sorted(filenames):
            if fn.endswith((".sv", ".v")):
                fpath = os.path.join(dirpath, fn)
                rel = os.path.relpath(fpath, rtl_root)
                with open(fpath, encoding="utf-8") as f:
                    files[rel] = f.read()

    if not files:
        return []

    calls = [_optimize_call(rel, content, lint_counts, memory_context)
             for rel, content in files.items()]
    briefs = [_write_brief(c, run_dir) for c in calls]
    print(f"[optimize] running {len(briefs)} optimization call(s)...", flush=True)
    await run_briefs_concurrently(briefs, concurrency=concurrency)

    proposals = []
    for call, (rel, original) in zip(calls, files.items()):
        out = _read_output(call, run_dir)
        if not out:
            continue
        if (out.get("decision") or "").lower() == "no_change":
            print(f"[optimize] {rel}: no changes needed")
            continue
        optimized = (out.get("optimized_file") or "").strip()
        if not optimized:
            continue
        if not optimized.endswith("\n"):
            optimized += "\n"
        if optimized.strip() == original.strip():
            print(f"[optimize] {rel}: output identical to input, skipping")
            continue
        proposals.append(OptimizationProposal(
            target_file=rel,
            original_content=original,
            optimized_content=optimized,
            changes_summary=out.get("changes_summary", []),
            rationale=out.get("rationale", ""),
            confidence=out.get("confidence", "low"),
        ))
        n_changes = len(out.get("changes_summary", []))
        print(f"[optimize] {rel}: {n_changes} optimization(s) proposed "
              f"(confidence={out.get('confidence', '?')})")

    return proposals


def optimize_rtl_sync(*args, **kwargs) -> list[OptimizationProposal]:
    return asyncio.run(optimize_rtl(*args, **kwargs))

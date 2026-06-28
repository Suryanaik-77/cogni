"""
cogni.agent.fixer
=================
Cognitive fix proposer.

For each violation produced by `agent.sweep`, run the same predict →
verify → revise → reflect chain as the verdict layer, but with the goal
of producing a code patch that resolves the violation.

Stages per violation:
  1. propose   (Claude)   → {patch_unified_diff, rationale, confidence}
  2. verify    (GPT + Gemini, parallel)
                          → {agrees, concerns, suggested_revisions}
  3. revise    (Claude, only if any verifier dissents)
                          → final patch
  4. reflect   (Claude)   → {rule_attribution, surprise?, kb_edits[*]}

Output is a list of FixProposal records ready for the report writer.
The fixer never touches source files. Patches are unified diffs that
the operator applies with `git apply` or `patch -p1`.

This module mirrors the architecture of agent/organs.py and agent/orchestrator.py
but is self-contained — it does not modify the verdict layer.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any

from agent.llm import LLMCall
from agent import llm as _llm        # for late-bound MODEL_* (test-mode swap)
from agent.llm.transports import run_briefs_concurrently
from agent.sweep import RuleCheck


# -----------------------------------------------------------------------------
# Output records
# -----------------------------------------------------------------------------

@dataclass
class VerifierOpinion:
    verifier: str        # "gpt" | "gemini"
    agrees: bool
    concerns: list[str] = field(default_factory=list)
    suggested_revisions: list[str] = field(default_factory=list)


@dataclass
class FixProposal:
    rule_id: str
    rule_statement: str
    rule_kind: str
    rule_strength: str
    measurement_key: str
    measured: Any
    expected: Any
    target_file: str
    patch_unified_diff: str
    fixed_file: str
    rationale: str
    confidence: str       # likely | uncertain | unlikely
    verifier_opinions: list[VerifierOpinion] = field(default_factory=list)
    revisions: int = 0
    reflection: dict = field(default_factory=dict)
    citations: list[dict] = field(default_factory=list)


# -----------------------------------------------------------------------------
# JSON Schemas (closed, mirror organs.py style)
# -----------------------------------------------------------------------------

PROPOSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    # Only the rationale + confidence are universally required. The patch
    # PAYLOAD can be EITHER a full corrected file (`fixed_file`, most reliable —
    # no line-number/context matching) OR a `patch_unified_diff`; `target_file`
    # is optional because it's derivable from the diff header or a single-file
    # context. Requiring all four made weak models fail the whole call when
    # they emitted a diff but omitted the redundant target_file field.
    "required": ["rationale", "confidence"],
    "properties": {
        "target_file":         {"type": "string"},
        "fixed_file":          {"type": "string"},
        "patch_unified_diff":  {"type": "string"},
        "rationale":           {"type": "string"},
        "confidence":          {"type": "string", "enum": ["likely", "uncertain", "unlikely", "high", "medium", "low"]},
        "decision":            {"type": "string", "enum": ["propose", "refuse"]},
        "missing_evidence":    {"type": "array", "items": {"type": "string"}},
    },
}

VERIFIER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["agrees", "concerns", "suggested_revisions"],
    "properties": {
        "agrees":               {"type": "boolean"},
        "concerns":             {"type": "array", "items": {"type": "string"}},
        "suggested_revisions":  {"type": "array", "items": {"type": "string"}},
        "alternative_patch":    {"type": "string"},
    },
}

REFLECT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rule_attribution"],
    "properties": {
        "rule_attribution": {
            "type": "object",
            "additionalProperties": False,
            "required": ["rule_id", "outcome"],
            "properties": {
                "rule_id":   {"type": "string"},
                "outcome":   {"type": "string", "enum": ["supported", "failed", "neutral"]},
                "note":      {"type": "string"},
            },
        },
        "surprise":  {"type": "string"},
        "kb_edits":  {"type": "array"},   # left freeform; KB.apply handles validation
    },
}


# -----------------------------------------------------------------------------
# Prompt builders
# -----------------------------------------------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:60] or "x"


_DIFF_TARGET_RE = re.compile(r"^\+\+\+\s+(?:b/)?(\S+)", re.MULTILINE)


def _has_payload(prop: dict) -> bool:
    """A proposal carries a real fix if it has a full file or a diff and is
    not an explicit refusal."""
    if (prop.get("decision") or "").lower() == "refuse":
        return False
    return bool((prop.get("fixed_file") or "").strip()
                or (prop.get("patch_unified_diff") or "").strip())


def _derive_target_file(prop: dict, source_files: dict[str, str]) -> str:
    """Best-effort target path: explicit field -> diff `+++ b/<path>` header ->
    the sole source file. Keeps weak models from failing just because they
    omitted the (redundant) target_file."""
    tf = (prop.get("target_file") or "").strip()
    if tf:
        return tf
    m = _DIFF_TARGET_RE.search(prop.get("patch_unified_diff") or "")
    if m and m.group(1) not in ("/dev/null",):
        return m.group(1)
    if len(source_files) == 1:
        return next(iter(source_files))
    return ""


def _rule_view(rc: RuleCheck) -> dict:
    return {
        "id":        rc.rule_id,
        "statement": rc.statement,
        "kind":      rc.kind,
        "strength":  rc.strength,
        "examples":  rc.examples,
        "prevents":  rc.prevents,
        "citations": rc.citations,
        "checks":    [asdict(c) for c in rc.checks],
    }


def _propose_call(rc: RuleCheck, source_files: dict[str, str],
                   memory_context: str = "") -> LLMCall:
    """source_files: { relative_path: file_content } — the snippets the patch
    will edit. Caller decides which files to expose.
    """
    prompt = """# Role: Fix Proposer (primary cognitive model)

A deterministic sweep found that the user's design violates a rule from
the knowledge pack. Your job is to propose a minimal, surgical patch to
the source that resolves the violation, plus a one-paragraph rationale.

## How to return the fix

PREFER returning the **whole corrected file** in `fixed_file` — the full text
of the one file after your edit. It is the most reliable form: no line numbers
or context to drift. Set `target_file` to that file's path (a key in
`source_files`). Only change what the rule requires; keep everything else
byte-for-byte.

If you'd rather, you may instead return a `patch_unified_diff` (the kind
`patch -p1` accepts) whose `+++ b/<path>` header names `target_file`. Use one
file per fix unless the violation truly spans multiple files.

## Discipline

- Do not rewrite or refactor unrelated code. Smallest change that satisfies
  the rule is best.
- If the rule's `examples.compliant` is present, prefer the pattern shown
  there.
- If the violation cannot be fixed without information you don't have
  (missing PDK details, unclear intent, ambiguity about which signal is
  authoritative), set `decision: "refuse"` with `missing_evidence` listing
  what you'd need. A clean refusal is better than a guess.

## Confidence levels

- `likely`     : you'd commit this patch yourself
- `uncertain`  : the fix is plausible but you'd want a peer review
- `unlikely`   : you can produce a patch but you suspect it won't survive

## Inputs

Read `inputs.json`. It contains:
  - `rule`        : the violated rule with its `examples` and `predicts[*]`
  - `violation`   : measured value, expected band, channel
  - `source_files`: { path: content } — the relevant source snippets
  - `memory`      : (optional) prior agent knowledge about this design —
                     past fix attempts, finding trends, accepted/reverted
                     history. Use this to avoid repeating failed approaches.

## Output

Return JSON matching the schema. `target_file` must be one of the keys in
`source_files`. If you use a diff, its `---` / `+++` lines must use that path.
"""
    inputs: dict[str, Any] = {
        "rule": _rule_view(rc),
        "violation": {
            "measurement_key": rc.checks[0].measurement_key if rc.checks else "",
            "measured":        rc.checks[0].measured if rc.checks else None,
            "expected":        rc.checks[0].expected if rc.checks else None,
            "channel":         rc.checks[0].channel if rc.checks else "",
            "reason":          rc.checks[0].reason if rc.checks else "",
        },
        "source_files": source_files,
    }
    if memory_context:
        inputs["memory"] = memory_context
    return LLMCall(
        name=f"fix_propose.{_slug(rc.rule_id)}",
        model=_llm.MODEL_OPUS,
        role="fix_proposer",
        prompt=prompt,
        schema=PROPOSE_SCHEMA,
        inputs=inputs,
    )


def _verifier_calls(rc: RuleCheck, proposal: dict, source_files: dict) -> list[LLMCall]:
    prompt = """# Role: Patch Verifier (independent critic)

A primary fix proposer produced a patch for a rule violation. Critique it.
You are not here to agree.

## What to check
1. Does the patch actually resolve the violation per the rule's `predicts`?
2. Does it introduce new violations (latches, width, blkseq, ...)?
3. Is it minimal? Or does it refactor unrelated code?
4. Does the unified diff format apply cleanly to `target_file`?
5. Is the `confidence` honest?

## Output

- `agrees`: true only if you would commit this patch yourself.
- `concerns`: specific, actionable. No generic statements.
- `suggested_revisions`: what should change. Refusal is OK.
- `alternative_patch`: optional — a sharper diff if you'd replace it.

Be a peer engineer. If the patch looks right, say so cleanly.
"""
    inputs = {
        "rule": _rule_view(rc),
        "proposal": proposal,
        "source_files": source_files,
    }
    base = _slug(rc.rule_id)
    return [
        LLMCall(name=f"fix_verify_gpt.{base}",    model=_llm.MODEL_GPT,
                role="fix_verifier_gpt", prompt=prompt,
                schema=VERIFIER_SCHEMA, inputs=inputs),
        LLMCall(name=f"fix_verify_gemini.{base}", model=_llm.MODEL_GEMINI,
                role="fix_verifier_gemini", prompt=prompt,
                schema=VERIFIER_SCHEMA, inputs=inputs),
    ]


def _revise_call(rc: RuleCheck, prior: dict, verifier_feedback: list[dict],
                 source_files: dict) -> LLMCall:
    prompt = """# Role: Fix Reviser

Two verifiers reviewed your patch. At least one disagreed. Read their
concerns and suggested revisions, then produce a final patch that
addresses them — or refuse cleanly if the concerns reveal you're missing
necessary evidence.

Use the same JSON shape as the original proposal.
"""
    inputs = {
        "rule": _rule_view(rc),
        "prior_proposal": prior,
        "verifier_feedback": verifier_feedback,
        "source_files": source_files,
    }
    return LLMCall(
        name=f"fix_revise.{_slug(rc.rule_id)}",
        model=_llm.MODEL_OPUS,
        role="fix_reviser",
        prompt=prompt,
        schema=PROPOSE_SCHEMA,
        inputs=inputs,
    )


def _reflect_call(rc: RuleCheck, final_proposal: dict,
                  verifier_opinions: list[dict]) -> LLMCall:
    prompt = """# Role: Reflector

A patch was proposed for a rule violation. Verifiers reviewed it. Look at
the rule, the violation, the final patch, and the verifier opinions —
then attribute outcome to the rule.

Optionally propose KB edits if you saw a clear weakness in the rule itself
(e.g. its `examples.compliant` was misleading, or its `predicts` band was
off). Use the standard kb_edits format: add_rule | weaken_rule |
strengthen_rule | append_unless | retire_rule. Most fixes will need no
edits — empty `kb_edits` is the common case.
"""
    inputs = {
        "rule": _rule_view(rc),
        "final_proposal": final_proposal,
        "verifier_opinions": verifier_opinions,
    }
    return LLMCall(
        name=f"fix_reflect.{_slug(rc.rule_id)}",
        model=_llm.MODEL_OPUS,
        role="fix_reflector",
        prompt=prompt,
        schema=REFLECT_SCHEMA,
        inputs=inputs,
    )


# -----------------------------------------------------------------------------
# Source-file resolver
# -----------------------------------------------------------------------------

def _gather_source(rtl_root: str | None,
                   netlist_path: str | None,
                   max_bytes: int = 40_000) -> dict[str, str]:
    """Best-effort: read all .sv/.v under rtl_root, or the netlist file.
    Trims each file to max_bytes for prompt budget control."""
    out: dict[str, str] = {}
    paths: list[str] = []
    if rtl_root and os.path.isdir(rtl_root):
        for dp, _, fs in os.walk(rtl_root):
            for fn in sorted(fs):
                if fn.endswith((".sv", ".v")):
                    paths.append(os.path.join(dp, fn))
    if netlist_path and os.path.exists(netlist_path):
        paths.append(netlist_path)
    for p in paths:
        try:
            with open(p) as f:
                txt = f.read(max_bytes + 1)
            if len(txt) > max_bytes:
                txt = txt[:max_bytes] + f"\n// ... truncated at {max_bytes} bytes ...\n"
            # Use path relative to rtl_root if possible — keeps diffs portable.
            key = os.path.relpath(p, rtl_root) if rtl_root else os.path.basename(p)
            out[key] = txt
        except Exception:
            continue
    return out


# -----------------------------------------------------------------------------
# Brief writer + dispatcher
# -----------------------------------------------------------------------------

def _write_brief(call: LLMCall, run_dir: str) -> dict:
    paths = call.write_brief(run_dir)
    return {
        "name":   call.name, "model": call.model, "role": call.role,
        "prompt": paths["prompt"], "schema": paths["schema"],
        "inputs": paths["inputs"], "output": paths["output"],
    }


def _read_output(call: LLMCall, run_dir: str) -> dict | None:
    out = os.path.join(run_dir, "llm_calls", call.name, "output.json")
    if not os.path.exists(out):
        return None
    with open(out) as f:
        return json.load(f)


# -----------------------------------------------------------------------------
# Public entrypoint
# -----------------------------------------------------------------------------

async def propose_fixes(violations: list[RuleCheck],
                        run_dir: str,
                        *,
                        rtl_root: str | None = None,
                        netlist_path: str | None = None,
                        concurrency: int = 4,
                        verify: bool = False,
                        memory_context: str = "",
                        on_progress=None) -> list[FixProposal]:
    """Run the propose → (verify → revise) → reflect cognition chain over every
    violation. Returns one FixProposal per violation.

    `verify` (default False) controls the cross-LLM patch panel (verifier 1 +
    verifier 2). It is OFF by default: the real check on a patch is re-running
    the tool (Verilator/Yosys) on the patched copy, so the LLM panel is
    redundant. Enable it only when you want a second opinion before applying.

    `memory_context` (optional) is a text block summarising the agent's prior
    knowledge about this design — past fix attempts, finding trends, etc.
    It is injected into each proposer's inputs so the LLM can avoid repeating
    failed approaches.
    """
    os.makedirs(run_dir, exist_ok=True)
    source_files = _gather_source(rtl_root, netlist_path)

    if not violations:
        return []

    # ---- Stage 1: propose, fanned out across violations ----
    propose_calls = [_propose_call(rc, source_files, memory_context)
                     for rc in violations]
    propose_briefs = [_write_brief(c, run_dir) for c in propose_calls]
    if on_progress: on_progress("propose", len(propose_briefs), 0)
    await run_briefs_concurrently(propose_briefs, concurrency=concurrency)
    proposals = [_read_output(c, run_dir) or {} for c in propose_calls]

    # Verifier-panel state (stays empty when verify=False -> no verifier 1/2 calls).
    per_violation_verifiers: list[list[dict]] = [[] for _ in violations]
    per_violation_verifier_kinds: list[list[str]] = [[] for _ in violations]
    revise_calls: list[LLMCall | None] = [None] * len(violations)
    revised: list[dict | None] = [None] * len(violations)

    if verify:
        # ---- Stage 2: verify, panel × violations, all in parallel ----
        all_verify_calls: list[LLMCall] = []
        verify_index: list[tuple[int, str]] = []   # (violation_idx, "gpt"/"gemini")
        for i, (rc, prop) in enumerate(zip(violations, proposals)):
            if not _has_payload(prop):
                continue
            vcs = _verifier_calls(rc, prop, source_files)
            for vc in vcs:
                all_verify_calls.append(vc)
                verify_index.append((i, "gpt" if "gpt" in vc.name else "gemini"))
        verify_briefs = [_write_brief(c, run_dir) for c in all_verify_calls]
        if on_progress: on_progress("verify", len(verify_briefs), 0)
        await run_briefs_concurrently(verify_briefs, concurrency=concurrency)
        verify_outputs = [_read_output(c, run_dir) or {} for c in all_verify_calls]
        for (vi, kind), out in zip(verify_index, verify_outputs):
            per_violation_verifiers[vi].append(out)
            per_violation_verifier_kinds[vi].append(kind)

        # ---- Stage 3: revise (only where any verifier disagrees) ----
        for i, opinions in enumerate(per_violation_verifiers):
            if opinions and any(not o.get("agrees", False) for o in opinions):
                revise_calls[i] = _revise_call(violations[i], proposals[i],
                                                opinions, source_files)
        revise_briefs = [_write_brief(c, run_dir) for c in revise_calls if c]
        if revise_briefs:
            if on_progress: on_progress("revise", len(revise_briefs), 0)
            await run_briefs_concurrently(revise_briefs, concurrency=concurrency)
        revised = [(_read_output(c, run_dir) or {}) if c else None for c in revise_calls]

    # ---- Stage 4: reflect, one call per violation that produced a patch ----
    final_proposals = [revised[i] or proposals[i] for i in range(len(violations))]
    reflect_calls: list[LLMCall | None] = [None] * len(violations)
    for i, fp in enumerate(final_proposals):
        if _has_payload(fp):
            reflect_calls[i] = _reflect_call(violations[i], fp,
                                              per_violation_verifiers[i])
    reflect_briefs = [_write_brief(c, run_dir) for c in reflect_calls if c]
    if reflect_briefs:
        if on_progress: on_progress("reflect", len(reflect_briefs), 0)
        await run_briefs_concurrently(reflect_briefs, concurrency=concurrency)
    reflections = [(_read_output(c, run_dir) or {}) if c else {}
                   for c in reflect_calls]

    # ---- Build FixProposal records ----
    out: list[FixProposal] = []
    for i, rc in enumerate(violations):
        fp = final_proposals[i] or {}
        v_opinions = []
        for kind, opinion in zip(per_violation_verifier_kinds[i],
                                  per_violation_verifiers[i]):
            v_opinions.append(VerifierOpinion(
                verifier=kind,
                agrees=bool(opinion.get("agrees", False)),
                concerns=list(opinion.get("concerns", []) or []),
                suggested_revisions=list(opinion.get("suggested_revisions", []) or []),
            ))
        first = rc.checks[0] if rc.checks else None
        out.append(FixProposal(
            rule_id=rc.rule_id,
            rule_statement=rc.statement,
            rule_kind=rc.kind,
            rule_strength=rc.strength,
            measurement_key=first.measurement_key if first else "",
            measured=first.measured if first else None,
            expected=first.expected if first else None,
            target_file=_derive_target_file(fp, source_files),
            patch_unified_diff=fp.get("patch_unified_diff", ""),
            fixed_file=fp.get("fixed_file", ""),
            rationale=fp.get("rationale", ""),
            confidence=fp.get("confidence", "unlikely"),
            verifier_opinions=v_opinions,
            revisions=1 if revise_calls[i] is not None else 0,
            reflection=reflections[i],
            citations=rc.citations,
        ))
    return out


def propose_fixes_sync(*args, **kwargs) -> list[FixProposal]:
    """Sync wrapper for CLIs that don't want to drive an event loop."""
    return asyncio.run(propose_fixes(*args, **kwargs))

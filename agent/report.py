"""
cogni.agent.report
==================
Report writers for the sweep + fix CLI.

Outputs:
  REPORT.json   — full machine-readable record (sweep + fixes)
  REPORT.md     — human-readable summary
  patches/<rule_id>__<file>.patch  — one unified diff per accepted fix
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from typing import Any

from agent.sweep import SweepReport, RuleCheck
from agent.fixer import FixProposal


_SEV_ORDER = {"constraint": 0, "tendency": 1, "heuristic": 2, "identity": 3}


def _safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "x"


def write_json_report(report_dir: str,
                      sweep_report: SweepReport,
                      fixes: list[FixProposal],
                      *,
                      meta: dict | None = None) -> str:
    out = {
        "meta": meta or {},
        "summary": {
            "stage":               sweep_report.stage,
            "n_rules_total":       sweep_report.n_rules_total,
            "n_rules_applicable":  sweep_report.n_rules_applicable,
            "n_violations":        sweep_report.n_violations,
            "n_clean":             sweep_report.n_clean,
            "n_skipped":           sweep_report.n_skipped,
            "n_na":                sweep_report.n_na,
            "n_fixes_proposed":    sum(1 for f in fixes if f.patch_unified_diff),
            "n_fixes_refused":     sum(1 for f in fixes if not f.patch_unified_diff),
        },
        "rules": [asdict(r) for r in sweep_report.rules],
        "fixes": [asdict(f) for f in fixes],
    }
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, "REPORT.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    return path


def write_patches(report_dir: str, fixes: list[FixProposal]) -> list[str]:
    """Write one .patch per fix that has a non-empty diff. Returns list of paths."""
    pdir = os.path.join(report_dir, "patches")
    os.makedirs(pdir, exist_ok=True)
    written = []
    for f in fixes:
        if not f.patch_unified_diff:
            continue
        fname_part = _safe_filename(f.target_file or "patch")
        path = os.path.join(pdir, f"{_safe_filename(f.rule_id)}__{fname_part}.patch")
        with open(path, "w") as fh:
            fh.write(f.patch_unified_diff.rstrip() + "\n")
        written.append(path)
    return written


def _format_violation_block(rc: RuleCheck) -> str:
    lines = []
    lines.append(f"### {rc.rule_id} — {rc.kind} / {rc.strength}\n")
    lines.append(f"{rc.statement}\n")
    for c in rc.checks:
        if c.status == "violation":
            lines.append(
                f"- **{c.measurement_key}**: {c.reason}  "
                f"(channel: `{c.channel}`)"
            )
    if rc.examples.get("compliant"):
        lines.append("\n**Compliant pattern:**")
        for ex in rc.examples["compliant"][:1]:
            lines.append("```")
            lines.append(ex.strip())
            lines.append("```")
    if rc.citations:
        lines.append("\n**References:**")
        for cit in rc.citations[:3]:
            t = cit.get("title", "ref")
            u = cit.get("url", "")
            lines.append(f"- [{t}]({u})")
    return "\n".join(lines)


def _format_fix_block(fp: FixProposal) -> str:
    lines = []
    lines.append(f"### Fix for `{fp.rule_id}` — confidence: **{fp.confidence}**, "
                  f"target: `{fp.target_file or '?'}`, revisions: {fp.revisions}\n")
    if fp.rationale:
        lines.append(fp.rationale.strip() + "\n")
    if fp.patch_unified_diff:
        lines.append("```diff")
        lines.append(fp.patch_unified_diff.rstrip())
        lines.append("```")
    else:
        lines.append("_No patch produced — see verifier opinions below._")
    if fp.verifier_opinions:
        lines.append("\n**Verifier panel:**")
        for v in fp.verifier_opinions:
            mark = "✓ agrees" if v.agrees else "✗ dissents"
            lines.append(f"- `{v.verifier}` — {mark}")
            for c in v.concerns[:3]:
                lines.append(f"    - concern: {c}")
            for s in v.suggested_revisions[:2]:
                lines.append(f"    - suggests: {s}")
    refl = fp.reflection or {}
    attr = refl.get("rule_attribution") or {}
    if attr:
        lines.append(f"\n**Reflection:** rule outcome = `{attr.get('outcome','?')}`"
                      f"{' — ' + attr['note'] if attr.get('note') else ''}")
    if refl.get("surprise"):
        lines.append(f"**Surprise:** {refl['surprise']}")
    if refl.get("kb_edits"):
        lines.append(f"**Proposed KB edits:** {len(refl['kb_edits'])} (see REPORT.json)")
    return "\n".join(lines)


def write_markdown_report(report_dir: str,
                          sweep_report: SweepReport,
                          fixes: list[FixProposal],
                          *,
                          meta: dict | None = None) -> str:
    meta = meta or {}
    lines: list[str] = []
    lines.append(f"# Cogni — {sweep_report.stage.upper()} stage gate report\n")
    if meta.get("scenario"):
        lines.append(f"_scenario_: `{meta['scenario']}`")
    if meta.get("pack_path"):
        lines.append(f"_pack_: `{meta['pack_path']}`")
    if meta.get("source"):
        lines.append(f"_reality source_: `{meta['source']}`")
    if meta.get("test_mode") is not None:
        lines.append(f"_test mode_: `{meta['test_mode']}`")
    lines.append("")

    s = sweep_report
    lines.append("## Summary\n")
    lines.append(f"- rules in pack: **{s.n_rules_total}**")
    lines.append(f"- applicable to this design: **{s.n_rules_applicable}**")
    lines.append(f"- violations: **{s.n_violations}**, clean: **{s.n_clean}**, "
                  f"n/a: **{s.n_na}**, skipped: **{s.n_skipped}**")
    if fixes:
        n_proposed = sum(1 for f in fixes if f.patch_unified_diff)
        lines.append(f"- patches proposed: **{n_proposed}** "
                      f"(of {len(fixes)} attempted)")
    lines.append("")

    # group violations by kind for severity ordering
    violations = [r for r in s.rules if r.status == "violation"]
    if violations:
        lines.append("## Violations\n")
        violations.sort(key=lambda r: (_SEV_ORDER.get(r.kind, 9), r.rule_id))
        for r in violations:
            lines.append(_format_violation_block(r))
            lines.append("")
    else:
        lines.append("## Violations\n\n_None — design is clean against this pack._\n")

    if fixes:
        lines.append("## Proposed fixes\n")
        for f in fixes:
            lines.append(_format_fix_block(f))
            lines.append("")
        lines.append(
            "Patches are also written to `patches/` as standalone unified "
            "diffs. Apply with `git apply patches/<file>` from the project "
            "root after review.\n"
        )

    skipped = [r for r in s.rules if r.status == "skipped"]
    if skipped:
        lines.append("## Skipped (gating not satisfied)\n")
        for r in skipped[:50]:
            lines.append(f"- `{r.rule_id}` — {r.reason}")
        if len(skipped) > 50:
            lines.append(f"- ... and {len(skipped)-50} more")
        lines.append("")

    na = [r for r in s.rules if r.status == "na"]
    if na:
        lines.append("## Not measurable in this run\n")
        lines.append(f"_{len(na)} rules apply but their measurement keys were "
                      f"not reported by the oracle._\n")

    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, "REPORT.md")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def write_all(report_dir: str,
              sweep_report: SweepReport,
              fixes: list[FixProposal],
              *,
              meta: dict | None = None) -> dict[str, Any]:
    j = write_json_report(report_dir, sweep_report, fixes, meta=meta)
    m = write_markdown_report(report_dir, sweep_report, fixes, meta=meta)
    p = write_patches(report_dir, fixes)
    return {"json": j, "markdown": m, "patches": p}

"""
cogni.agent.fix_verify
======================
Close the fix loop with REALITY, not opinion.

The fixer (agent/fixer.py) proposes unified-diff patches; its "verify" stage
is two LLMs *reviewing* the patch. This module does the harder, honest check:

    1. duplicate the raw source into a throwaway temp tree
    2. apply the proposed patch(es) to the copy
    3. re-run `verilator --lint-only` on the patched copy
    4. report warning counts BEFORE vs AFTER

So the fixer can say "width warnings 1 -> 0, confirmed by Verilator" instead
of "two models think this patch is fine." The original source is never
touched — everything happens on the copy.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

_WARN_RE = re.compile(r"%Warning-([A-Z0-9_]+)")


def gather_rtl_files(root: str) -> list[str]:
    """All .sv/.v files under `root` (sorted, absolute)."""
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith((".sv", ".v")):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


def lint_counts(files: list[str], *, top: str | None = None,
                verilator_bin: str = "verilator",
                extra_args: list[str] | None = None) -> dict[str, int]:
    """Run `verilator --lint-only` and return {WARNING_CLASS: count}.

    Verilator exits non-zero when it finds warnings, so we read its output
    regardless of return code. Returns {} on a clean design.
    """
    if not files:
        return {}
    cmd = [verilator_bin, "--lint-only"]
    if extra_args:
        cmd += extra_args
    if top:
        cmd += ["--top-module", top]
    cmd += files
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise RuntimeError(f"verilator not found ({verilator_bin!r})") from e
    blob = (proc.stdout or "") + (proc.stderr or "")
    counts: dict[str, int] = {}
    for m in _WARN_RE.finditer(blob):
        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def _apply_patch(workdir: str, diff_text: str) -> bool:
    """Apply one unified diff inside `workdir`. Tries git-apply then patch,
    at strip levels 1 and 0 (the proposer may or may not use a/ b/ prefixes).
    Returns True on the first clean apply."""
    if not diff_text.strip():
        return False
    if not diff_text.endswith("\n"):
        diff_text += "\n"
    patch_path = os.path.join(workdir, "_cogni_fix.patch")
    with open(patch_path, "w", encoding="utf-8") as fh:
        fh.write(diff_text)
    attempts = (
        ["git", "apply", "-p1", "_cogni_fix.patch"],
        ["git", "apply", "-p0", "_cogni_fix.patch"],
        ["patch", "-p1", "--no-backup-if-mismatch", "-i", "_cogni_fix.patch"],
        ["patch", "-p0", "--no-backup-if-mismatch", "-i", "_cogni_fix.patch"],
    )
    ok = False
    for cmd in attempts:
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
        if r.returncode == 0:
            ok = True
            break
    try:
        os.remove(patch_path)
    except OSError:
        pass
    return ok


def verify_fixes(patches: list[dict], rtl_root: str, *,
                 top: str | None = None,
                 verilator_bin: str = "verilator",
                 extra_args: list[str] | None = None) -> dict:
    """Duplicate -> apply -> re-lint.

    `patches`: list of {"target_file": str, "patch_unified_diff": str,
                        "rule_id": str (optional)}.
    Returns:
      {
        "before": {CLASS: n}, "after": {CLASS: n},
        "delta":  {CLASS: after-before},
        "applied": [{"target_file","rule_id","applied": bool}],
        "total_before": int, "total_after": int,
        "resolved": bool,           # strictly fewer warnings, none newly added
        "tmp_dir": str,
      }
    """
    before = lint_counts(gather_rtl_files(rtl_root), top=top,
                         verilator_bin=verilator_bin, extra_args=extra_args)

    tmp_root = tempfile.mkdtemp(prefix="cogni_fixchk_")
    work = os.path.join(tmp_root, os.path.basename(os.path.normpath(rtl_root)) or "rtl")
    shutil.copytree(rtl_root, work)

    applied = []
    for p in patches:
        diff = p.get("patch_unified_diff") or ""
        ok = _apply_patch(work, diff) if diff else False
        applied.append({"target_file": p.get("target_file", ""),
                        "rule_id": p.get("rule_id", ""), "applied": ok})

    after = lint_counts(gather_rtl_files(work), top=top,
                        verilator_bin=verilator_bin, extra_args=extra_args)

    classes = set(before) | set(after)
    delta = {c: after.get(c, 0) - before.get(c, 0) for c in classes
             if after.get(c, 0) != before.get(c, 0)}
    total_before, total_after = sum(before.values()), sum(after.values())
    any_applied = any(a["applied"] for a in applied)
    no_new = all(after.get(c, 0) <= before.get(c, 0) for c in classes)

    return {
        "before": before, "after": after, "delta": delta,
        "applied": applied,
        "total_before": total_before, "total_after": total_after,
        "resolved": bool(any_applied and total_after < total_before and no_new),
        "tmp_dir": work,
    }


def format_report(result: dict) -> str:
    """Human-readable before/after block."""
    lines = ["", "=" * 60, "  FIX RE-CHECK (Verilator on patched copy)", "=" * 60]
    na = sum(1 for a in result["applied"] if a["applied"])
    lines.append(f"  patches applied : {na}/{len(result['applied'])}")
    for a in result["applied"]:
        mark = "ok " if a["applied"] else "FAIL"
        lines.append(f"     [{mark}] {a['target_file']}  ({a['rule_id']})")
    lines.append(f"  warnings before : {result['total_before']}  {result['before'] or '{}'}")
    lines.append(f"  warnings after  : {result['total_after']}  {result['after'] or '{}'}")
    if result["delta"]:
        lines.append(f"  change          : {result['delta']}")
    verdict = "RESOLVED (confirmed by Verilator)" if result["resolved"] else \
              "NOT resolved (warnings not reduced / patch failed / new warnings)"
    lines.append(f"  verdict         : {verdict}")
    lines.append("=" * 60)
    return "\n".join(lines)

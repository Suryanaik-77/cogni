"""
cogni_check.py
==============
Stage-gate CLI: run an RTL or synth design through the cogni knowledge pack,
report all violations, and optionally propose fix patches.

Usage:
    # RTL stage, replay from precomputed findings
    python3 cogni_check.py scenarios/rtl_demo --stage rtl

    # Synth stage, replay
    python3 cogni_check.py scenarios/ibex_synth --stage synth

    # RTL stage, live verilator if installed
    python3 cogni_check.py path/to/my_design --stage rtl --rtl-root path/to/rtl

    # With fix proposals (cognitive layer: predict→verify→revise→reflect)
    python3 cogni_check.py scenarios/rtl_demo --stage rtl --propose-fixes

The scenario directory may contain:
  - config.yaml         (optional; pack_path, perceiver/oracle paths, rtl_root)
  - manifest.json       (perceiver facts/tags)
  - findings.json       (precomputed reality)
  - rtl/                (live RTL tree)
  - netlist.v           (gate-level netlist for synth stage)

If no scenario dir is given, paths can be supplied via CLI flags.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.kb import KnowledgeBase
from agent.sweep import sweep, SweepReport
from agent.fixer import propose_fixes
from agent.report import write_all
from agent.core import WorldModel
from agent import llm as _llm


# -----------------------------------------------------------------------------
# Config / scenario layout
# -----------------------------------------------------------------------------

def _read_yaml_or_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    if path.endswith(".json"):
        with open(path) as f:
            return json.load(f)
    # tiny YAML reader (only the subset our configs use): supports flat
    # key: value pairs and one level of nested mapping. Falls back to PyYAML
    # if available.
    try:
        import yaml  # type: ignore
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        out: dict = {}
        cur = out
        cur_key = None
        with open(path) as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                if not line.startswith(" "):
                    if ":" not in line:
                        continue
                    k, _, v = line.partition(":")
                    v = v.strip()
                    if not v:
                        out[k.strip()] = {}
                        cur = out[k.strip()]
                        cur_key = k.strip()
                    else:
                        out[k.strip()] = _coerce(v)
                        cur = out
                        cur_key = None
                else:
                    if ":" not in line:
                        continue
                    k, _, v = line.lstrip().partition(":")
                    if isinstance(cur, dict) and cur_key:
                        cur[k.strip()] = _coerce(v.strip())
                    else:
                        out[k.strip()] = _coerce(v.strip())
        return out


def _coerce(s: str):
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if s.lower() in ("null", "none", "~", ""):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s.strip("\"'")


# -----------------------------------------------------------------------------
# World builder (lightweight perceiver)
# -----------------------------------------------------------------------------

def _world_from_manifest(manifest_path: str, default_stage: str) -> WorldModel:
    """Build a WorldModel from a flat manifest.json shaped as:
       { "facts": { key: value }, "tags": [tag, ...] }
    plus auto-injected `<stage>_stage` tag.
    """
    w = WorldModel(domain="vlsi", facts={}, tags=set())
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path) as f:
            data = json.load(f)
        for k, v in (data.get("facts") or {}).items():
            w.add(k, v, source="manifest")
        for t in (data.get("tags") or []):
            w.tags.add(str(t))
    w.tags.add(f"{default_stage}_stage")
    return w


def _build_world(stage: str, scenario_dir: str | None,
                  rtl_root: str | None,
                  manifest_path: str | None) -> WorldModel:
    """Prefer the registered adapter perceiver (it injects discriminator
    tags). Fall back to manifest-only if the perceiver can't run.
    """
    cfg = _scenario_config(scenario_dir)
    perc_cfg = cfg.get("perceiver") or {}
    if not manifest_path:
        manifest_path = perc_cfg.get("manifest_path") or (
            os.path.join(scenario_dir, "manifest.json") if scenario_dir else None
        )

    world = WorldModel(domain="vlsi", facts={}, tags=set())
    world.tags.add(f"{stage}_stage")

    # Try the registered adapter first.
    try:
        from adapters import make_perceiver
        tool = cfg.get("tool") or ("verilator" if stage == "rtl" else "yosys")
        adapter_cfg = dict(perc_cfg)
        if manifest_path and "manifest_path" not in adapter_cfg:
            adapter_cfg["manifest_path"] = manifest_path
        adapter = make_perceiver(stage, tool, adapter_cfg)
        # raw_input: rtl_root if RTL-stage, else scenario_dir.
        raw = rtl_root or cfg.get("rtl_root") or scenario_dir or ""
        adapter.perceive(world, raw)
        return world
    except Exception as e:
        print(f"[cogni-check] adapter perceiver unavailable ({type(e).__name__}: {e}); "
              f"falling back to manifest-only.")

    # Manifest-only fallback.
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path) as f:
            data = json.load(f)
        for k, v in (data.get("facts") or {}).items():
            world.add(k, v, source="manifest")
        for t in (data.get("tags") or []):
            world.tags.add(str(t))
    return world


# -----------------------------------------------------------------------------
# Stage runners
# -----------------------------------------------------------------------------

def _scenario_config(scenario_dir: str | None) -> dict:
    if not scenario_dir:
        return {}
    for fn in ("config.yaml", "config.yml", "config.json"):
        p = os.path.join(scenario_dir, fn)
        if os.path.exists(p):
            return _read_yaml_or_json(p)
    return {}


def _run_rtl(scenario_dir: str | None,
             rtl_root: str | None,
             findings_path: str | None,
             top_module: str | None):
    from adapters.rtl.verilator.runner import observe as rtl_observe
    cfg = _scenario_config(scenario_dir)
    oracle_cfg = cfg.get("oracle") or {}
    if scenario_dir:
        if not findings_path:
            cand = os.path.join(scenario_dir, "findings.json")
            if os.path.exists(cand):
                findings_path = cand
            elif oracle_cfg.get("findings_path"):
                findings_path = oracle_cfg["findings_path"]
        if not rtl_root:
            cand = os.path.join(scenario_dir, "rtl")
            if os.path.isdir(cand):
                rtl_root = cand
            elif cfg.get("rtl_root"):
                rtl_root = cfg["rtl_root"]
    return rtl_observe(rtl_root=rtl_root, findings_path=findings_path,
                       top_module=top_module), {"rtl_root": rtl_root,
                                                  "findings_path": findings_path}


def _run_synth(scenario_dir: str | None,
               findings_path: str | None,
               reports_dir: str | None,
               netlist_path: str | None,
               top_module: str | None):
    from adapters.synth.yosys.runner import observe as synth_observe
    cfg = _scenario_config(scenario_dir)
    oracle_cfg = cfg.get("oracle") or {}
    if scenario_dir:
        if not findings_path:
            cand = os.path.join(scenario_dir, "findings.json")
            if os.path.exists(cand):
                findings_path = cand
            elif oracle_cfg.get("findings_path"):
                findings_path = oracle_cfg["findings_path"]
        if not reports_dir:
            cand = os.path.join(scenario_dir, "reports")
            if os.path.isdir(cand):
                reports_dir = cand
            elif oracle_cfg.get("reports_dir"):
                reports_dir = oracle_cfg["reports_dir"]
        if not netlist_path:
            for fn in ("netlist.v", "syn_netlist.v", "out.v"):
                cand = os.path.join(scenario_dir, fn)
                if os.path.exists(cand):
                    netlist_path = cand
                    break
    return synth_observe(findings_path=findings_path, reports_dir=reports_dir,
                         netlist_path=netlist_path, top_module=top_module), {
        "findings_path": findings_path, "reports_dir": reports_dir,
        "netlist_path": netlist_path,
    }


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def _resolve_pack(stage: str, override: str | None) -> str:
    if override:
        return override
    return f"packs/{stage}/rules.json"


def main():
    # Force UTF-8 output so a stray non-ASCII char (em-dash etc.) never
    # crashes printing on a latin-1 locale.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("scenario_dir", nargs="?", default=None,
                    help="Optional scenario directory containing manifest/findings/rtl.")
    ap.add_argument("--stage", choices=["rtl", "synth"], required=True)
    ap.add_argument("--pack", default=None,
                    help="Override pack path (default packs/<stage>/rules.json).")
    ap.add_argument("--manifest", default=None,
                    help="Path to manifest.json (defaults to <scenario>/manifest.json).")
    ap.add_argument("--findings", default=None,
                    help="Path to precomputed findings.json (replay path).")
    ap.add_argument("--rtl-root", default=None, help="RTL tree (rtl stage only).")
    ap.add_argument("--reports-dir", default=None,
                    help="Yosys reports dir (synth stage).")
    ap.add_argument("--netlist", default=None,
                    help="Gate-level netlist (.v) for synth stage.")
    ap.add_argument("--top-module", default=None)
    ap.add_argument("--out", default=None,
                    help="Report output dir (default: scenario_dir/report-<ts> "
                         "or ./report-<ts>).")
    ap.add_argument("--propose-fixes", action="store_true",
                    help="Run cognitive fix proposer over violations.")
    ap.add_argument("--verify-fixes", action="store_true",
                    help="After proposing, duplicate the source, apply the "
                         "patches, and re-run Verilator to confirm the fix "
                         "(rtl stage). Implies --propose-fixes.")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="Max parallel LLM calls (default 4).")
    ap.add_argument("--test-mode", action="store_true",
                    help="Use cheaper/faster models for proposer + verifiers.")
    args = ap.parse_args()

    if args.test_mode:
        _llm.enable_test_mode()

    # ---- Pack ----
    pack_path = _resolve_pack(args.stage, args.pack)
    # Load via KB to enforce schema validation, then read raw JSON for the
    # sweep engine (which works on the v1 dict shape directly).
    kb = KnowledgeBase.load(pack_path)
    with open(pack_path) as f:
        pack = json.load(f)
    pack["__path__"] = pack_path
    pack["__rule_count__"] = len(kb.rules)

    # ---- World ----
    world = _build_world(args.stage, args.scenario_dir,
                          args.rtl_root, args.manifest)

    # ---- Reality ----
    if args.stage == "rtl":
        reality, src_meta = _run_rtl(args.scenario_dir, args.rtl_root,
                                      args.findings, args.top_module)
    else:
        reality, src_meta = _run_synth(args.scenario_dir, args.findings,
                                        args.reports_dir, args.netlist,
                                        args.top_module)

    # ---- Sweep ----
    print(f"[cogni-check] stage={args.stage}  pack={pack_path}")
    print(f"[cogni-check] reality source={getattr(reality, 'source', '?')}")
    print(f"[cogni-check] measurements: {len(reality.measurements)}  "
          f"world facts: {len(world.facts)}  tags: {len(world.tags)}")

    rep = sweep(pack, world, reality, stage_filter=args.stage)
    print(f"[cogni-check] {rep.n_rules_total} rules: "
          f"{rep.n_violations} violations, {rep.n_clean} clean, "
          f"{rep.n_skipped} skipped, {rep.n_na} n/a")

    # ---- Output dir ----
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if args.out:
        out_dir = args.out
    elif args.scenario_dir:
        out_dir = os.path.join(args.scenario_dir, f"report-{ts}")
    else:
        out_dir = os.path.abspath(f"./report-{ts}")
    os.makedirs(out_dir, exist_ok=True)
    print(f"[cogni-check] output: {out_dir}")

    # ---- Fixes ----
    fixes = []
    if args.propose_fixes or args.verify_fixes:
        violations = rep.violations()
        if not violations:
            print("[cogni-check] no violations; skipping fix proposer.")
        else:
            print(f"[cogni-check] running fix proposer on {len(violations)} "
                  f"violations (test_mode={_llm.is_test_mode()})...")
            t0 = time.time()
            def _prog(stage, n, _):
                print(f"[cogni-check]   {stage}: {n} calls")
            fixes = asyncio.run(propose_fixes(
                violations, run_dir=out_dir,
                rtl_root=src_meta.get("rtl_root"),
                netlist_path=src_meta.get("netlist_path"),
                concurrency=args.concurrency,
                on_progress=_prog,
            ))
            print(f"[cogni-check] fix proposer done in {time.time()-t0:.1f}s "
                  f"({sum(1 for f in fixes if f.patch_unified_diff)} patches)")

    # ---- Verify fixes against reality (duplicate -> apply -> re-lint) ----
    if args.verify_fixes and fixes:
        if args.stage != "rtl":
            print("[cogni-check] --verify-fixes only supported for --stage rtl; skipping.")
        elif not src_meta.get("rtl_root"):
            print("[cogni-check] --verify-fixes needs an rtl_root; skipping.")
        else:
            from agent.fix_verify import verify_fixes, format_report
            patches = [{"target_file": f.target_file,
                        "patch_unified_diff": f.patch_unified_diff,
                        "rule_id": f.rule_id}
                       for f in fixes if f.patch_unified_diff]
            if patches:
                res = verify_fixes(patches, src_meta["rtl_root"],
                                   top=args.top_module)
                print(format_report(res))
            else:
                print("[cogni-check] no patches to verify.")

    # ---- Write reports ----
    meta = {
        "stage":          args.stage,
        "pack_path":      pack_path,
        "scenario":       args.scenario_dir,
        "source":         getattr(reality, "source", ""),
        "test_mode":      _llm.is_test_mode(),
        "timestamp":      ts,
        **src_meta,
    }
    paths = write_all(out_dir, rep, fixes, meta=meta)
    print(f"[cogni-check] wrote {paths['markdown']}")
    print(f"[cogni-check] wrote {paths['json']}")
    if paths["patches"]:
        print(f"[cogni-check] wrote {len(paths['patches'])} patch files under "
              f"{os.path.dirname(paths['patches'][0])}/")


if __name__ == "__main__":
    main()

"""
cogni.run_scenario
==================
Single entry point: load a scenario config, perceive, run cycles for each
question, observe reality, reflect. Logs everything to runs/<scenario>/<ts>/.

Usage:
    python3 run_scenario.py scenarios/ibex_signoff   [--dispatcher mock|subagent|replay]

Outputs (under runs/<scenario>/<ts>/):
    ledger.jsonl       — committed predictions
    refusals.jsonl     — refusals with reasons
    verdicts.jsonl     — verdict per prediction
    surprises.jsonl    — wrong predictions and the diagnosis
    kb_edits.jsonl     — typed edits applied to the KB
    llm_calls/         — every LLM brief + output (for audit / replay)
    summary.json       — hit rate, refusal rate, edit count
    kb_after.json      — the KB after all reflection
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone

# Make the cogni package importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.core import Prediction, Reality, Verdict, VerdictKind, new_id, jsonl_append, Confidence
from agent.kb import KnowledgeBase
from agent.perceiver import Perceiver
from agent.orchestrator import Orchestrator
from agent.dispatcher import MockDispatcher, SubagentDispatcher, RecordingDispatcher, ReplayDispatcher
from agent import verdict as verdict_engine


# ---------------------------------------------------------------------------
# Adapter + oracle factories (resolved by scenario config)
# ---------------------------------------------------------------------------

# Legacy adapter-name and oracle-type strings map to (stage, tool) pairs
# so old scenario configs keep working during the refactor.
_LEGACY_ADAPTER_TO_PAIR = {
    "rtl_ibex":    ("synth", "yosys"),   # Ibex flow went through Yosys
}
_LEGACY_ORACLE_TO_PAIR = {
    "yosys": ("synth", "yosys"),
}


def _resolve_pair(cfg: dict) -> tuple[str, str]:
    """Extract (stage, tool) from a scenario config, preferring the new
    explicit fields and falling back to legacy adapter/oracle names."""
    stage = cfg.get("stage")
    tool = cfg.get("tool")
    if stage and tool:
        return stage, tool
    # Legacy: adapter name first, oracle.type second.
    legacy_name = cfg.get("adapter")
    if legacy_name and legacy_name in _LEGACY_ADAPTER_TO_PAIR:
        return _LEGACY_ADAPTER_TO_PAIR[legacy_name]
    otype = (cfg.get("oracle") or {}).get("type")
    if otype and otype in _LEGACY_ORACLE_TO_PAIR:
        return _LEGACY_ORACLE_TO_PAIR[otype]
    raise ValueError(
        "scenario config must specify `stage` + `tool` (preferred) or a "
        "recognized legacy `adapter` / `oracle.type`"
    )


def make_adapter(cfg: dict):
    """Build the perceiver for the scenario.

    Accepts either the new schema (`stage`, `tool`, optional `perceiver`
    subdict) or the legacy `adapter: <name>` form.
    """
    from adapters import make_perceiver
    stage, tool = _resolve_pair(cfg)
    perceiver_cfg = cfg.get("perceiver") or {}
    return make_perceiver(stage, tool, perceiver_cfg)


def make_oracle(cfg: dict):
    """Build the oracle for the scenario.

    Accepts either the new schema or the legacy `oracle: {type, ...}` form.
    The oracle sub-config is passed through (minus the `type` key) so
    existing fields like `reports_dir`, `findings_path`,
    `ground_truth_path` continue to work unchanged.
    """
    from adapters import make_oracle as _mk
    stage, tool = _resolve_pair(cfg)
    oracle_cfg = dict(cfg.get("oracle") or {})
    oracle_cfg.pop("type", None)
    return _mk(stage, tool, oracle_cfg)


def make_dispatcher(kind: str, run_dir: str, replay_path: str = ""):
    if kind == "mock":
        inner = MockDispatcher(run_dir)
    elif kind == "subagent":
        inner = SubagentDispatcher(run_dir)
    elif kind == "replay":
        if not replay_path:
            raise ValueError("replay dispatcher needs --replay-trace")
        inner = ReplayDispatcher(replay_path)
    else:
        raise ValueError(f"unknown dispatcher kind: {kind}")
    trace = os.path.join(run_dir, "llm_trace.jsonl")
    return RecordingDispatcher(inner, trace)


# ---------------------------------------------------------------------------
# Verdict dispatch (mechanical)
# ---------------------------------------------------------------------------

def make_verdict(prediction: Prediction, reality: Reality, vspec: dict) -> Verdict:
    """Unified verdict dispatcher.

    If the prediction has a `structured_claim`, route everything through
    the new claim-typed engine. Scenarios still pass `vspec` so the
    summary key hint and any legacy positive_substrings are honored as
    a low-confidence fallback when nothing structured decides.
    """
    t = vspec.get("type")
    key = vspec["measurement_key"]

    has_structured = bool(getattr(prediction, "structured_claim", None))
    has_legacy_quant = bool(prediction.quantitative)

    if has_structured or has_legacy_quant:
        return verdict_engine.verdict_for(
            prediction, reality,
            summary_key_hints=[key],
            legacy_measurement_key=key if t == "contains" else None,
            legacy_positive_substrings=vspec.get("positive_substrings") if t == "contains" else None,
        )

    # No structured claim AND no quantitative — fall back to legacy paths.
    if t == "numeric":
        return verdict_engine.numeric_verdict(prediction, reality, key)
    if t == "contains":
        return verdict_engine.contains_verdict(prediction, reality, key,
                                               vspec.get("positive_substrings", []))
    if t == "categorical":
        return verdict_engine.categorical_verdict(prediction, reality, key,
                                                  lambda a, c: str(a).lower() == c.lower())
    raise ValueError(f"unknown verdict type: {t}")


# ---------------------------------------------------------------------------
# Tiny YAML reader (avoid PyYAML dep)
# ---------------------------------------------------------------------------

def read_config(path: str) -> dict:
    """Tiny YAML subset: top-level scalars and one-level nested mappings."""
    out: dict = {}
    cur = out
    stack = [(0, out)]
    with open(path) as f:
        lines = f.readlines()
    in_block = False
    block_lines = []
    block_key = None
    block_indent = 0
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            if in_block: block_lines.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if in_block:
            if indent > block_indent or not stripped:
                block_lines.append(line)
                continue
            else:
                # close block
                _assign(stack, block_indent, block_key, "\n".join(block_lines).strip())
                in_block = False
                block_lines = []
                block_key = None

        # strip inline comments outside of block scalars
        # (only when '#' is preceded by whitespace, to keep '#' inside strings safe)
        for i, ch in enumerate(stripped):
            if ch == '#' and i > 0 and stripped[i-1].isspace():
                stripped = stripped[:i].rstrip()
                break

        if stripped.endswith("|"):
            block_key = stripped[:-1].strip().rstrip(":").strip()
            block_indent = indent
            in_block = True
            continue

        if ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip(); val = val.strip()
            if not val:
                # nested mapping
                new = {}
                _assign(stack, indent, key, new)
                stack.append((indent + 2, new))
            else:
                # strip quotes
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                _assign(stack, indent, key, val)
    if in_block:
        _assign(stack, block_indent, block_key, "\n".join(block_lines).strip())
    return out


def _assign(stack, indent, key, val):
    while len(stack) > 1 and stack[-1][0] > indent:
        stack.pop()
    stack[-1][1][key] = val


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(scenario_dir: str, dispatcher_kind: str = "mock",
        replay_trace: str = "", limit: int = 0) -> dict:
    cfg = read_config(os.path.join(scenario_dir, "config.yaml"))
    name = cfg.get("name", os.path.basename(scenario_dir))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs", name, ts)
    os.makedirs(run_dir, exist_ok=True)
    print(f"[run] scenario={name} run_dir={run_dir} dispatcher={dispatcher_kind}", flush=True)

    # ---------------- KB ----------------
    kb = KnowledgeBase.load(cfg["pack_path"])
    # mirror the KB into the run dir so the mutations don't clobber the pack
    kb_run_path = os.path.join(run_dir, "kb_active.json")
    kb.save(kb_run_path)
    kb = KnowledgeBase.load(kb_run_path)

    # ---------------- Adapter + Perceiver ----------------
    adapter = make_adapter(cfg)
    perceiver = Perceiver(adapter)

    # ---------------- Oracle ----------------
    oracle = make_oracle(cfg)

    # ---------------- Dispatcher + Orchestrator ----------------
    dispatcher = make_dispatcher(dispatcher_kind, run_dir, replay_trace)
    orch = Orchestrator(kb=kb, dispatcher=dispatcher, run_dir=run_dir,
                        confidence_floor=Confidence(cfg.get("confidence_floor", "uncertain")))

    # ---------------- Question loop ----------------
    summary = {"name": name, "ts": ts, "run_dir": run_dir,
               "n_committed": 0, "n_refused": 0, "n_right": 0, "n_wrong": 0,
               "n_unfalsifiable": 0, "n_kb_edits": 0, "by_question": []}

    if name == "ibex_signoff":
        questions = json.load(open(os.path.join(scenario_dir, "questions.json")))["questions"]
        # one shared world for all questions
        world = perceiver.perceive([cfg["rtl_root"]])
        # one shared reality for all questions (Yosys ran once)
        reality = oracle.from_existing()
        with open(os.path.join(run_dir, "world.json"), "w") as f:
            json.dump(world.to_dict(), f, indent=2)
        with open(os.path.join(run_dir, "reality.json"), "w") as f:
            json.dump(asdict(reality), f, indent=2, default=str)
        question_set = [(q, world, reality) for q in questions]
    else:
        raise ValueError(f"don't know how to drive scenario '{name}'")

    if limit:
        question_set = question_set[:limit]

    for q, world, reality in question_set:
        qid = q["id"]
        question = q["question"]
        stage = q.get("stage")
        print(f"[run] === {qid}: {question}", flush=True)

        # ---- One cognitive cycle ----
        # Pass the verdict spec's measurement_key so the predictor's
        # structured_claim uses the same key reality stores under.
        mkey = (q.get("verdict") or {}).get("measurement_key")
        rec = orch.cycle(world, question, stage=stage, measurement_key_hint=mkey)

        outcome = rec.get("outcome")
        if outcome != "committed":
            summary["n_refused"] += 1
            summary["by_question"].append({"id": qid, "question": question,
                                           "outcome": outcome,
                                           "refusal_id": rec.get("refusal_id")})
            print(f"[run]    -> {outcome}", flush=True)
            continue

        # ---- Reload prediction (orchestrator wrote to ledger) ----
        # We have prediction_id; build a Prediction object from the ledger record
        pred = _last_ledger_prediction(run_dir, rec["prediction_id"])
        if pred is None:
            print(f"[run]    !! couldn't find prediction in ledger", flush=True)
            continue

        # ---- Verdict ----
        v = make_verdict(pred, reality, q["verdict"])
        orch.commit_verdict(v)
        summary["n_committed"] += 1
        if v.kind == VerdictKind.RIGHT_AND_RIGHT_REASON:
            summary["n_right"] += 1
        elif v.kind == VerdictKind.UNFALSIFIABLE:
            summary["n_unfalsifiable"] += 1
        else:
            summary["n_wrong"] += 1

        # ---- Reflect ----
        edits = orch.reflect(pred, reality, v)
        summary["n_kb_edits"] += len(edits)

        summary["by_question"].append({
            "id": qid, "question": question,
            "outcome": "committed",
            "claim": pred.claim,
            "confidence": pred.confidence.value,
            "cited_rule_ids": pred.cited_rule_ids,
            "verdict": v.kind.value,
            "verdict_notes": v.notes,
            "n_edits": len(edits),
        })
        print(f"[run]    -> committed: {v.kind.value} ({len(edits)} edits)", flush=True)

    # ---------------- Persist summary + final KB ----------------
    with open(os.path.join(run_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)
    kb.save(os.path.join(run_dir, "kb_after.json"))
    print(f"[run] DONE  committed={summary['n_committed']} "
          f"right={summary['n_right']} wrong={summary['n_wrong']} "
          f"refused={summary['n_refused']} edits={summary['n_kb_edits']}",
          flush=True)
    return summary


def _last_ledger_prediction(run_dir: str, prediction_id: str) -> Prediction | None:
    path = os.path.join(run_dir, "ledger.jsonl")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            if d.get("id") == prediction_id:
                return Prediction(
                    id=d["id"],
                    question=d["question"],
                    claim=d["claim"],
                    rationale=d["rationale"],
                    confidence=Confidence(d["confidence"]),
                    falsifier=d["falsifier"],
                    cited_rule_ids=d["cited_rule_ids"],
                    quantitative=d.get("quantitative"),
                    stage=d.get("stage"),
                    primary_model=d.get("primary_model", ""),
                    revisions=d.get("revisions", 0),
                    created_at=d.get("created_at", ""),
                )
    return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario_dir")
    ap.add_argument("--dispatcher", default="mock", choices=["mock", "subagent", "replay"])
    ap.add_argument("--replay-trace", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    run(args.scenario_dir, args.dispatcher, args.replay_trace, args.limit)

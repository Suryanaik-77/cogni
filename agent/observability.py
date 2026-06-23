"""
agent.observability
===================
Per-session rollup of cost, latency, and behavior metrics.

Reads `meta.json` files written by transports (one per LLM call) plus
the session's `ledger.jsonl` and `summary.json`, and produces a single
`metrics.json` document that answers:

  Q1. How much did this run cost?           -> token+$ totals
  Q2. How long did each stage take?         -> latency rollup
  Q3. Did predictor populate structured_claim? -> coverage %
  Q4. Did reflector overrule low-conf verdicts? -> overrule rate
  Q5. Did any transport silently fail?      -> error_count by transport

Pricing is approximate and drawn from public list prices at the time of
this writing; treat the dollar figures as order-of-magnitude estimates.
"""
from __future__ import annotations
import glob
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# Pricing table  ($/1M tokens, input/output)
# Source: vendor public list prices (May 2026). Update when these move.
# ---------------------------------------------------------------------------

_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-4-5":     (15.0, 75.0),
    "claude-sonnet-4-5":   (3.0,  15.0),
    # OpenAI
    "gpt-5":               (1.25, 10.0),
    "gpt-5-mini":          (0.25,  2.0),
    # Google
    "gemini-2.5-flash":    (0.075, 0.30),
    "gemini-2.5-pro":      (1.25, 10.0),
}

# Bedrock model ids are long & region-prefixed (e.g.
# "us.meta.llama3-3-70b-instruct-v1:0"), so they're matched by keyword
# rather than exact id. Approximate Bedrock list prices per 1M tokens.
_BEDROCK_PRICING: list[tuple[str, tuple[float, float]]] = [
    ("claude-opus",   (15.0, 75.0)),
    ("claude-sonnet", (3.0,  15.0)),
    ("claude-haiku",  (0.80, 4.0)),
    ("llama",         (0.72, 0.72)),
    ("mistral-large", (2.0,  6.0)),
    ("nova-pro",      (0.80, 3.20)),
    ("nova-lite",     (0.06, 0.24)),
]


def _price(model: str, in_tok: int, out_tok: int) -> float:
    """Return USD cost; 0.0 if model unknown so we never crash on rollup."""
    p = _PRICING.get(model)
    if not p:
        # Bedrock ids: substring-match against the keyword table.
        m = model.lower()
        for needle, rate in _BEDROCK_PRICING:
            if needle in m:
                p = rate
                break
    if not p:
        return 0.0
    return (in_tok * p[0] + out_tok * p[1]) / 1_000_000.0


# ---------------------------------------------------------------------------
# Stage classifier (by call-name suffix)
# ---------------------------------------------------------------------------

def _classify_stage(name: str) -> str:
    """Map a call_dir name to a stage. Names look like
    `<scenario>.<qid>.<stage>` or `attention.<slug>` etc."""
    # run_real names: "<scen>.<qid>.attention" / ".predict" / ".verify_gpt" / ".verify_gemini" / ".reflect"
    for suffix in ("attention", "predict", "verify_gpt", "verify_gemini", "reflect"):
        if name.endswith("." + suffix) or name == suffix:
            return suffix.replace("verify_gpt", "verify").replace("verify_gemini", "verify")
    if "verify" in name:
        return "verify"
    if "reflect" in name:
        return "reflect"
    if "predict" in name:
        return "predict"
    if "attention" in name:
        return "attention"
    return "other"


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------

@dataclass
class StageStats:
    n_calls: int = 0
    n_failed: int = 0
    elapsed_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def rollup(session_dir: str) -> dict[str, Any]:
    """Walk a session_dir and produce metrics.json content.

    Idempotent and side-effect-free apart from reading files.
    """
    calls_dir = os.path.join(session_dir, "llm_calls")
    by_stage: dict[str, StageStats] = {}
    by_model: dict[str, StageStats] = {}
    errors: list[dict] = []

    for meta_path in glob.glob(os.path.join(calls_dir, "*", "meta.json")):
        try:
            with open(meta_path) as f:
                m = json.load(f)
        except Exception:
            continue
        call_name = os.path.basename(os.path.dirname(meta_path))
        stage = _classify_stage(call_name)
        model = m.get("model", "unknown")
        in_tok = int(m.get("input_tokens") or 0)
        out_tok = int(m.get("output_tokens") or 0)
        elapsed = float(m.get("elapsed_s") or 0.0)
        cost = _price(model, in_tok, out_tok)

        for bucket, key in ((by_stage, stage), (by_model, model)):
            s = bucket.setdefault(key, StageStats())
            s.n_calls += 1
            s.elapsed_s += elapsed
            s.input_tokens += in_tok
            s.output_tokens += out_tok
            s.cost_usd += cost
            if not m.get("ok", True):
                s.n_failed += 1

        if not m.get("ok", True):
            errors.append({
                "call": call_name,
                "stage": stage,
                "model": model,
                "error": m.get("error", ""),
            })

    # ---- Behavior metrics: structured_claim coverage + reflector overrule ----
    n_predictions = 0
    n_with_structured = 0
    n_low_conf_verdicts = 0
    n_reflector_overruled = 0

    ledger_path = os.path.join(session_dir, "ledger.jsonl")
    if os.path.exists(ledger_path):
        with open(ledger_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                n_predictions += 1
                sc = d.get("structured_claim")
                if isinstance(sc, dict) and any(sc.get(k) for k in
                        ("intervals", "enum", "ranking", "includes", "excludes")):
                    n_with_structured += 1

    # Reflector overrule: reflect.output.json has a verdict_overrule field set.
    # We look for low-confidence verdicts in summary.json and check their
    # reflect output for overrule signals.
    summary_path = os.path.join(session_dir, "summary.json")
    if os.path.exists(summary_path):
        with open(summary_path) as f:
            summary = json.load(f)
        for scen_name, scen in (summary.get("scenarios") or {}).items():
            for q in scen.get("questions", []):
                # We don't currently emit verdict_confidence in summary.json
                # rows; read the full reflect output if it exists.
                qid = q.get("id", "")
                reflect_dir = os.path.join(
                    calls_dir, f"{scen_name}.{qid}.reflect"
                )
                rout = os.path.join(reflect_dir, "output.json")
                if not os.path.exists(rout):
                    continue
                try:
                    with open(rout) as f:
                        ref = json.load(f)
                except Exception:
                    continue
                # Heuristic: reflector overrules when its `kb_edits` flag a
                # disagreement with the mechanical verdict OR when its
                # `verdict_overrule` field is set (future-proofing — we'll
                # add this field next).
                if ref.get("verdict_overrule"):
                    n_reflector_overruled += 1
                # Counting low-conf verdicts requires reading the verdict
                # record — verdict_confidence lives on Verdict but is not
                # currently propagated to summary rows. We log presence
                # only; numerator/denominator below are best-effort.

    # Latency banner extraction: parse stage totals if a banner file exists.
    # (Not required — by_stage already has elapsed sums.)

    out = {
        "totals": {
            "n_calls": sum(s.n_calls for s in by_stage.values()),
            "n_failed": sum(s.n_failed for s in by_stage.values()),
            "elapsed_s_sum": round(sum(s.elapsed_s for s in by_stage.values()), 2),
            "input_tokens": sum(s.input_tokens for s in by_stage.values()),
            "output_tokens": sum(s.output_tokens for s in by_stage.values()),
            "cost_usd": round(sum(s.cost_usd for s in by_stage.values()), 4),
        },
        "by_stage": {k: {**asdict(v),
                         "elapsed_s": round(v.elapsed_s, 2),
                         "cost_usd": round(v.cost_usd, 4)}
                     for k, v in sorted(by_stage.items())},
        "by_model": {k: {**asdict(v),
                         "elapsed_s": round(v.elapsed_s, 2),
                         "cost_usd": round(v.cost_usd, 4)}
                     for k, v in sorted(by_model.items())},
        "errors": errors,
        "behavior": {
            "n_predictions": n_predictions,
            "n_with_structured_claim": n_with_structured,
            "structured_claim_coverage_pct": (
                round(100.0 * n_with_structured / n_predictions, 1)
                if n_predictions else 0.0
            ),
            "n_reflector_overruled": n_reflector_overruled,
        },
    }
    return out


def write_rollup(session_dir: str) -> str:
    """Compute and persist metrics.json. Returns the path."""
    metrics = rollup(session_dir)
    path = os.path.join(session_dir, "metrics.json")
    with open(path, "w") as f:
        json.dump(metrics, f, indent=2)
    return path

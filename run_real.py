"""
cogni.run_real
==============
Stage-locked real-LLM driver. Avoids the SubagentDispatcher's blocking
poll loop by exposing each stage as a separate command. The parent
agent (the human + me) fires actual subagents between stages.

Usage:
    # 1. Prepare everything: perceive, recall, write attention briefs
    python3 run_real.py prepare scenarios/ibex_signoff scenarios/ibex_synth

    # 2. After firing all attention subagents and dropping output.json
    #    files in the right places, write predict briefs:
    python3 run_real.py predict <session_dir>

    # 3. After firing predict subagents:
    python3 run_real.py verify  <session_dir>

    # 4. After firing verifier subagents:
    python3 run_real.py reflect <session_dir>

    # 5. Final summary:
    python3 run_real.py finalize <session_dir>

Each stage produces a `pending.json` listing the briefs that need a
subagent run. The agent reads it, spawns subagents in parallel, waits,
and proceeds to the next stage. Outputs accumulate on disk and the
final stage rolls them up into ledger.jsonl, verdicts.jsonl, etc.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.core import (Prediction, Reality, Verdict, VerdictKind, Refusal, Confidence,
                        new_id, jsonl_append, _now)
from agent.kb import KnowledgeBase
from agent.perceiver import Perceiver
from agent import organs, verdict as verdict_engine
from agent.llm import (
    LLMCall, validate_schema,
    MODEL_OPUS, MODEL_GPT, MODEL_GEMINI, is_test_mode, enable_test_mode,
    is_bedrock_mode,
)
import agent.llm as _llm
from agent.llm.transports import (
    OpenAITransport, GeminiTransport, ClaudeTransport,
    run_briefs_concurrently,
)
from run_scenario import (read_config, make_adapter, make_oracle, make_verdict)

# Default vendor models for direct-API verifier execution.
# Test mode swaps gpt-5 -> gpt-5-mini to cut the ~70s verify floor.
VENDOR_GPT_MODEL    = "gpt-5-mini" if is_test_mode() else "gpt-5"
VENDOR_GEMINI_MODEL = "gemini-2.5-flash"  # Flash works on free tier; Pro requires paid.



SESSION_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs", "real")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _save_call(call: LLMCall, run_dir: str) -> dict:
    paths = call.write_brief(run_dir)
    return {
        "name": call.name, "model": call.model, "role": call.role,
        "prompt": paths["prompt"], "schema": paths["schema"],
        "inputs": paths["inputs"], "output": paths["output"],
        "objective": call.subagent_objective(run_dir=run_dir),
    }


def _load_output(call_name: str, run_dir: str, schema: dict) -> dict | None:
    out_path = os.path.join(run_dir, "llm_calls", call_name, "output.json")
    if not os.path.exists(out_path):
        return None
    with open(out_path) as f:
        data = json.load(f)
    validate_schema(data, schema)
    return data


def _save_state(run_dir: str, name: str, obj):
    with open(os.path.join(run_dir, f"{name}.json"), "w") as f:
        json.dump(obj, f, indent=2, default=str)


def _load_state(run_dir: str, name: str) -> dict:
    with open(os.path.join(run_dir, f"{name}.json")) as f:
        return json.load(f)


def _save_pending(run_dir: str, stage: str, briefs: list[dict]):
    p = os.path.join(run_dir, f"pending_{stage}.json")
    with open(p, "w") as f:
        json.dump({"stage": stage, "briefs": briefs, "saved_at": _now()},
                  f, indent=2, default=str)
    print(f"[{stage}] wrote {len(briefs)} briefs to {p}", flush=True)


# ---------------------------------------------------------------------------
# Stage 1: PREPARE — perceive, recall, write attention briefs
# ---------------------------------------------------------------------------

def cmd_prepare(scenario_dirs: list[str]):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    session_dir = os.path.join(SESSION_ROOT, ts)
    os.makedirs(session_dir, exist_ok=True)
    print(f"[prepare] session_dir={session_dir}", flush=True)

    state = {"session_dir": session_dir, "started_at": _now(),
             "scenarios": {}, "questions": []}
    all_attention_briefs: list[dict] = []

    for scen_dir in scenario_dirs:
        cfg = read_config(os.path.join(scen_dir, "config.yaml"))
        name = cfg["name"]

        # --- KB (mirror so mutations don't clobber pack) ---
        kb = KnowledgeBase.load(cfg["pack_path"])
        kb_path = os.path.join(session_dir, f"{name}_kb.json")
        kb.save(kb_path)

        adapter = make_adapter(cfg)
        perceiver = Perceiver(adapter)
        oracle = make_oracle(cfg)

        scen_state = {"config": cfg, "kb_path": kb_path,
                      "scenario_dir": scen_dir, "questions": []}

        # Build (question, world, reality) triples
        triples = []
        # Single-shot perceive scenarios: one world, one reality, N questions.
        # Covers ibex_signoff (legacy), ibex_synth (v1 synth pack on Ibex),
        # rtl_demo (v1 RTL pack on cmd_alu.sv), and any future scenario that
        # ships a questions.json + a perceiver/oracle pair.
        if name in ("ibex_signoff", "ibex_synth", "rtl_demo"):
            qs_path = os.path.join(scen_dir, "questions.json")
            qs = json.load(open(qs_path))["questions"]
            # Perceiver input: legacy ibex_signoff used a list of paths;
            # rtl_demo / ibex_synth use a manifest_path baked into config
            # (no perceive() args required). Pass `rtl_root` if present
            # for backward compatibility, else an empty list.
            perceive_input = [cfg["rtl_root"]] if cfg.get("rtl_root") else []
            world = perceiver.perceive(perceive_input)
            reality = oracle.from_existing()
            world_path = os.path.join(session_dir, f"{name}_world.json")
            reality_path = os.path.join(session_dir, f"{name}_reality.json")
            with open(world_path, "w") as f: json.dump(world.to_dict(), f, indent=2)
            with open(reality_path, "w") as f: json.dump(asdict(reality), f, indent=2, default=str)
            for q in qs:
                triples.append((q, world, reality, world_path, reality_path))

        # Now write attention briefs for each question
        for q, world, reality, wp, rp in triples:
            cands = kb.recall(world, stage=q.get("stage"))
            qid = q["id"]
            scen_state["questions"].append({
                "id": qid,
                "question": q["question"],
                "stage": q.get("stage"),
                "verdict_spec": q["verdict"],
                "candidate_rule_ids": [r.id for r in cands],
                "world_path": wp, "reality_path": rp,
            })
            state["questions"].append({"scenario": name, "id": qid})
            if not cands:
                # immediate refusal — log and skip
                ref = Refusal(id=new_id("ref"), question=q["question"],
                              reason="No KB rules apply",
                              insufficient_rules=[],
                              missing_evidence=["KB has no applicable rule"])
                jsonl_append(os.path.join(session_dir, "refusals.jsonl"),
                             {**asdict(ref), "scenario": name, "qid": qid,
                              "confidence": ref.confidence.value})
                continue
            call = organs.attention_call(world, cands, q["question"], q.get("stage"))
            # Make call name unique per (scenario, question)
            call.name = f"{name}.{qid}.attention"
            all_attention_briefs.append(_save_call(call, session_dir))

        state["scenarios"][name] = scen_state

    _save_state(session_dir, "session", state)
    _save_pending(session_dir, "attention", all_attention_briefs)
    return session_dir


# ---------------------------------------------------------------------------
# Stage 2: PREDICT — read attention outputs, write predict briefs
# ---------------------------------------------------------------------------

def cmd_predict(session_dir: str):
    state = _load_state(session_dir, "session")
    predict_briefs = []
    for scen_name, scen in state["scenarios"].items():
        kb = KnowledgeBase.load(scen["kb_path"])
        for qrec in scen["questions"]:
            qid = qrec["id"]
            call_name = f"{scen_name}.{qid}.attention"
            out = _load_output(call_name, session_dir, organs.ATTENTION_SCHEMA)
            if out is None:
                continue  # subagent not run, skip
            qrec["attention"] = out

            focused_rule_ids = out.get("focused_rule_ids", [])
            focused_rules = [kb.by_id(rid) for rid in focused_rule_ids if kb.by_id(rid)]
            # Load world
            with open(qrec["world_path"]) as f:
                world_d = json.load(f)
            focused_facts = {k: world_d["facts"][k]["value"] for k in out.get("focused_fact_keys", [])
                             if k in world_d["facts"]}

            if not focused_rules:
                # refusal — log and skip
                ref = Refusal(id=new_id("ref"), question=qrec["question"],
                              reason="Attention selected no rules",
                              insufficient_rules=qrec.get("candidate_rule_ids", []),
                              missing_evidence=["Need rules specific to this question"])
                jsonl_append(os.path.join(session_dir, "refusals.jsonl"),
                             {**asdict(ref), "scenario": scen_name, "qid": qid,
                              "confidence": ref.confidence.value})
                qrec["outcome"] = "refused_no_focus"
                continue

            # Build a synthetic WorldModel for the predictor_call helper —
            # we already have facts dict and stage; organs.predictor_call only uses focused_rules + focused_facts.
            from agent.core import WorldModel
            w = WorldModel(domain=world_d["domain"])
            w.tags = set(world_d.get("tags", []))
            # Pass the verdict spec's measurement_key so the predictor
            # writes structured_claim against the SAME key reality uses.
            mkey = qrec.get("verdict_spec", {}).get("measurement_key")
            call = organs.predictor_call(w, focused_rules, focused_facts,
                                         qrec["question"], qrec.get("stage"),
                                         measurement_key_hint=mkey)
            call.name = f"{scen_name}.{qid}.predict"
            predict_briefs.append(_save_call(call, session_dir))
    _save_state(session_dir, "session", state)
    _save_pending(session_dir, "predict", predict_briefs)


# ---------------------------------------------------------------------------
# Stage 3: VERIFY — read predict outputs, build prediction objects, write 2 verifier briefs each
# ---------------------------------------------------------------------------

def cmd_verify(session_dir: str):
    state = _load_state(session_dir, "session")
    verify_briefs = []
    for scen_name, scen in state["scenarios"].items():
        kb = KnowledgeBase.load(scen["kb_path"])
        for qrec in scen["questions"]:
            qid = qrec["id"]
            if qrec.get("outcome", "").startswith("refused"):
                continue
            pred_call = f"{scen_name}.{qid}.predict"
            out = _load_output(pred_call, session_dir, organs.PREDICTOR_SCHEMA)
            if out is None:
                continue
            qrec["predictor_output"] = out
            if out.get("decision") == "refuse":
                ref = Refusal(id=new_id("ref"), question=qrec["question"],
                              reason=out.get("refusal_reason", "primary refused"),
                              insufficient_rules=out.get("rules_considered_but_rejected", []),
                              missing_evidence=out.get("missing_evidence", []))
                jsonl_append(os.path.join(session_dir, "refusals.jsonl"),
                             {**asdict(ref), "scenario": scen_name, "qid": qid,
                              "confidence": ref.confidence.value})
                qrec["outcome"] = "refused_by_primary"
                continue

            # Build the Prediction object now and persist (committed).
            confidence = Confidence(out.get("confidence", "uncertain"))
            pred = Prediction(
                id=new_id("pred"), question=qrec["question"],
                claim=out.get("claim", ""),
                rationale=out.get("rationale", ""),
                confidence=confidence,
                falsifier=out.get("falsifier", ""),
                cited_rule_ids=out.get("cited_rule_ids", []),
                quantitative=out.get("quantitative"),
                structured_claim=out.get("structured_claim"),
                stage=qrec.get("stage"),
                primary_model=MODEL_OPUS,
            )
            d = asdict(pred); d["confidence"] = pred.confidence.value
            d.update({"scenario": scen_name, "qid": qid})
            jsonl_append(os.path.join(session_dir, "ledger.jsonl"), d)
            qrec["prediction"] = d

            # Build verifier briefs
            focused_ids = qrec.get("attention", {}).get("focused_rule_ids", [])
            focused_rules = [kb.by_id(rid) for rid in focused_ids if kb.by_id(rid)]
            with open(qrec["world_path"]) as f:
                world_d = json.load(f)
            focused_facts = {k: world_d["facts"][k]["value"]
                             for k in qrec.get("attention", {}).get("focused_fact_keys", [])
                             if k in world_d["facts"]}
            calls = organs.verifier_calls(asdict(pred), focused_rules, focused_facts, qrec["question"])
            for c in calls:
                if c.role == "verifier (gpt)":
                    c.name = f"{scen_name}.{qid}.verify_gpt"
                else:
                    c.name = f"{scen_name}.{qid}.verify_gemini"
                verify_briefs.append(_save_call(c, session_dir))
            qrec["outcome"] = "predicted"
    _save_state(session_dir, "session", state)
    _save_pending(session_dir, "verify", verify_briefs)


# ---------------------------------------------------------------------------
# Stage 3b: VERIFY-API — same as verify, but executes briefs in-process
# against direct vendor APIs (OpenAI + Gemini) instead of staging subagents.
# ---------------------------------------------------------------------------

def cmd_verify_api(session_dir: str):
    """Build verifier briefs (same as cmd_verify) then execute them locally
    via the OpenAI and Gemini APIs. Skips writing pending_verify.json since
    nothing external needs to be spawned.
    """
    # Step 1 — build briefs (reuse cmd_verify's exact logic by calling it).
    cmd_verify(session_dir)

    # Step 2 — load the pending list and execute each brief locally.
    pending_path = os.path.join(session_dir, "pending_verify.json")
    if not os.path.exists(pending_path):
        print("[verify-api] no pending_verify.json found; nothing to run.")
        return
    with open(pending_path) as f:
        pending_doc = json.load(f)
    pending = pending_doc["briefs"] if isinstance(pending_doc, dict) else pending_doc

    # Lazy-init transports once. In Bedrock mode both verifiers run on
    # Bedrock-hosted models (Llama + Mistral/Nova); otherwise GPT + Gemini.
    if is_bedrock_mode():
        from agent.llm.transports import transport_for_model
        gpt = transport_for_model("bedrock_llama")
        gem = transport_for_model("bedrock_mistral")
    else:
        gpt = OpenAITransport(model=VENDOR_GPT_MODEL)
        gem = GeminiTransport(model=VENDOR_GEMINI_MODEL)

    n_ok = n_fail = 0
    for brief in pending:
        call_dir = os.path.dirname(brief["prompt"])
        name = brief["name"]
        if name.endswith(".verify_gpt"):
            tport = gpt; tag = "gpt"
        elif name.endswith(".verify_gemini"):
            tport = gem; tag = "gemini"
        else:
            print(f"[verify-api] skip {name} (unknown verifier suffix)")
            continue
        # Skip if already done (idempotent re-run).
        out_path = os.path.join(call_dir, "output.json")
        if os.path.exists(out_path):
            print(f"[verify-api] cached  {name}")
            n_ok += 1
            continue
        print(f"[verify-api] running {name} ({tag})... ", end="", flush=True)
        res = tport.run(call_dir)
        if res.ok:
            print(f"ok ({res.elapsed_s:.1f}s)")
            n_ok += 1
        else:
            print(f"FAIL ({res.elapsed_s:.1f}s): {res.error[:160] if res.error else '?'}")
            n_fail += 1
    print(f"[verify-api] done — {n_ok} ok, {n_fail} fail (of {len(pending)} briefs)")


# ---------------------------------------------------------------------------
# Stage 4: REFLECT — collect verifier outputs, compute verdicts, write reflect briefs
# ---------------------------------------------------------------------------

def cmd_reflect(session_dir: str):
    state = _load_state(session_dir, "session")
    reflect_briefs = []

    for scen_name, scen in state["scenarios"].items():
        kb = KnowledgeBase.load(scen["kb_path"])
        for qrec in scen["questions"]:
            qid = qrec["id"]
            if qrec.get("outcome") != "predicted":
                continue

            # Load verifier verdicts (informational only; no revision round in this run)
            v_gpt = _load_output(f"{scen_name}.{qid}.verify_gpt", session_dir, organs.VERIFIER_SCHEMA) or {"agrees": True, "concerns": [], "suggested_revisions": []}
            v_gem = _load_output(f"{scen_name}.{qid}.verify_gemini", session_dir, organs.VERIFIER_SCHEMA) or {"agrees": True, "concerns": [], "suggested_revisions": []}
            qrec["verifier_verdicts"] = [
                {"verifier_model": MODEL_GPT, **v_gpt},
                {"verifier_model": MODEL_GEMINI, **v_gem},
            ]

            # Compute Verdict
            with open(qrec["reality_path"]) as f:
                rd = json.load(f)
            reality = Reality(id=rd["id"], source=rd["source"],
                              measurements=rd["measurements"],
                              artifacts=rd.get("artifacts", []))
            pred_d = qrec["prediction"]
            pred = Prediction(
                id=pred_d["id"], question=pred_d["question"],
                claim=pred_d["claim"], rationale=pred_d["rationale"],
                confidence=Confidence(pred_d["confidence"]),
                falsifier=pred_d["falsifier"],
                cited_rule_ids=pred_d["cited_rule_ids"],
                quantitative=pred_d.get("quantitative"),
                structured_claim=pred_d.get("structured_claim"),
                stage=pred_d.get("stage"),
                primary_model=pred_d.get("primary_model", ""),
            )
            v = make_verdict(pred, reality, qrec["verdict_spec"])
            vd = asdict(v); vd["kind"] = v.kind.value
            vd.update({"scenario": scen_name, "qid": qid})
            jsonl_append(os.path.join(session_dir, "verdicts.jsonl"), vd)
            qrec["verdict"] = vd

            # Reflector brief
            cited = [kb.by_id(rid) for rid in pred.cited_rule_ids if kb.by_id(rid)]
            call = organs.reflector_call(asdict(pred), asdict(reality), asdict(v), cited)
            call.name = f"{scen_name}.{qid}.reflect"
            reflect_briefs.append(_save_call(call, session_dir))

    _save_state(session_dir, "session", state)
    _save_pending(session_dir, "reflect", reflect_briefs)


# ---------------------------------------------------------------------------
# Stage 5: FINALIZE — apply KB edits, write summary
# ---------------------------------------------------------------------------

def cmd_finalize(session_dir: str):
    from agent.core import KBEdit, KBEditKind, RuleStrength, RuleStatus, Rule, Surprise

    state = _load_state(session_dir, "session")
    summary = {"session_dir": session_dir, "scenarios": {}, "totals": {
        "n_predicted": 0, "n_right": 0, "n_wrong": 0, "n_unfalsifiable": 0,
        "n_refused": 0, "n_kb_edits": 0,
    }}

    for scen_name, scen in state["scenarios"].items():
        kb = KnowledgeBase.load(scen["kb_path"])
        scen_summary = {"questions": [], "n_right": 0, "n_wrong": 0, "n_refused": 0, "n_edits": 0}
        for qrec in scen["questions"]:
            qid = qrec["id"]
            row = {"id": qid, "question": qrec["question"]}
            if qrec.get("outcome", "").startswith("refused"):
                row["outcome"] = qrec["outcome"]
                summary["totals"]["n_refused"] += 1
                scen_summary["n_refused"] += 1
                scen_summary["questions"].append(row)
                continue
            if qrec.get("outcome") != "predicted":
                row["outcome"] = qrec.get("outcome", "no_output")
                scen_summary["questions"].append(row)
                continue

            v = qrec.get("verdict", {})
            row["claim"] = qrec["prediction"]["claim"]
            row["confidence"] = qrec["prediction"]["confidence"]
            row["cited_rules"] = qrec["prediction"]["cited_rule_ids"]
            row["verdict"] = v.get("kind")
            row["verdict_notes"] = v.get("notes", "")
            row["verifiers_agree"] = [vv["agrees"] for vv in qrec.get("verifier_verdicts", [])]
            summary["totals"]["n_predicted"] += 1
            if v.get("kind") == "right_and_right_reason":
                summary["totals"]["n_right"] += 1; scen_summary["n_right"] += 1
            elif v.get("kind") == "unfalsifiable":
                summary["totals"]["n_unfalsifiable"] += 1
            else:
                summary["totals"]["n_wrong"] += 1; scen_summary["n_wrong"] += 1

            # ---- Apply reflector edits ----
            ref_out = _load_output(f"{scen_name}.{qid}.reflect", session_dir, organs.REFLECTOR_SCHEMA)
            if not ref_out:
                row["edits"] = []
                scen_summary["questions"].append(row)
                continue

            # rule outcomes
            for rid, status in (ref_out.get("rule_attribution") or {}).items():
                mapped = {"supported": "right", "failed": "wrong", "neutral": "unfalsifiable"}.get(status, "unfalsifiable")
                kb.record_outcome(rid, mapped, prediction_id=qrec["prediction"]["id"])

            # surprise
            if v.get("kind") in ("wrong_and_wrong_reason", "wrong_but_right_direction", "right_but_wrong_reason"):
                sup = ref_out.get("surprise") or {}
                surprise = Surprise(
                    id=new_id("sup"),
                    prediction_id=qrec["prediction"]["id"],
                    verdict_id=v.get("id", ""),
                    what_we_expected=sup.get("what_we_expected", ""),
                    what_actually_happened=sup.get("what_actually_happened", ""),
                    why_we_missed_it=sup.get("why_we_missed_it", ""),
                    suggested_kb_action="; ".join(e.get("rationale", "") for e in ref_out.get("kb_edits", [])),
                )
                jsonl_append(os.path.join(session_dir, "surprises.jsonl"),
                             {**asdict(surprise), "scenario": scen_name, "qid": qid})

            # KB edits
            row_edits = []
            for e in ref_out.get("kb_edits", []):
                kind = KBEditKind(e["kind"])
                new_rule = None
                if e.get("new_rule"):
                    nr = e["new_rule"]
                    new_rule = Rule(
                        id=nr.get("id") or new_id("r"),
                        statement=nr.get("statement", ""),
                        when=nr.get("when", []), unless=nr.get("unless", []),
                        stage=nr.get("stage"),
                        strength=RuleStrength(nr.get("strength", "tendency")),
                        citations=nr.get("citations", []),
                        rationale=nr.get("rationale", ""),
                    )
                new_strength = RuleStrength(e["new_strength"]) if e.get("new_strength") else None
                edit = KBEdit(
                    id=new_id("kbe"), kind=kind,
                    target_rule_id=e.get("target_rule_id"),
                    new_rule=new_rule, new_strength=new_strength,
                    added_unless=e.get("added_unless", []),
                    rationale=e.get("rationale", ""),
                )
                kb.apply(edit)
                edrec = {"id": edit.id, "kind": edit.kind.value, "target_rule_id": edit.target_rule_id,
                         "new_strength": edit.new_strength.value if edit.new_strength else None,
                         "added_unless": edit.added_unless, "rationale": edit.rationale,
                         "applied": edit.applied,
                         "scenario": scen_name, "qid": qid}
                if edit.new_rule:
                    edrec["new_rule"] = edit.new_rule.to_dict()
                jsonl_append(os.path.join(session_dir, "kb_edits.jsonl"), edrec)
                row_edits.append(edrec)
                summary["totals"]["n_kb_edits"] += 1
                scen_summary["n_edits"] += 1
            row["edits"] = row_edits
            scen_summary["questions"].append(row)

        # save updated KB
        kb_after_path = os.path.join(session_dir, f"{scen_name}_kb_after.json")
        kb.save(kb_after_path)
        scen_summary["kb_after_path"] = kb_after_path
        summary["scenarios"][scen_name] = scen_summary

    _save_state(session_dir, "summary", summary)

    # ---- Observability rollup ----
    # Reads per-call meta.json (cost, tokens, latency) + ledger + summary,
    # writes metrics.json. Done after summary is saved so the rollup can
    # see structured_claim coverage from the ledger.
    try:
        from agent.observability import write_rollup
        metrics_path = write_rollup(session_dir)
        with open(metrics_path) as f:
            metrics = json.load(f)
        summary["metrics"] = metrics["totals"]
        summary["metrics_by_stage"] = metrics["by_stage"]
        summary["behavior"] = metrics["behavior"]
        if metrics["errors"]:
            summary["errors"] = metrics["errors"]
        # Re-save with metrics merged in.
        _save_state(session_dir, "summary", summary)
    except Exception as exc:
        print(f"[finalize] metrics rollup failed (non-fatal): {exc}")

    print(json.dumps(summary, indent=2, default=str))
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Unified async runner: prepare -> attention -> predict -> verify -> reflect -> finalize
# ---------------------------------------------------------------------------

import asyncio


def _load_pending_briefs(session_dir: str, stage: str) -> list[dict]:
    p = os.path.join(session_dir, f"pending_{stage}.json")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        d = json.load(f)
    return d["briefs"] if isinstance(d, dict) else d


async def _exec_stage(session_dir: str, stage: str, concurrency: int) -> dict:
    """Run all pending briefs for a stage concurrently. Returns counts."""
    briefs = _load_pending_briefs(session_dir, stage)
    if not briefs:
        print(f"[{stage}] no pending briefs.")
        return {"total": 0, "ok": 0, "fail": 0, "cached": 0, "wall_s": 0.0}

    n_ok = n_fail = n_cached = 0

    def _progress(idx, brief, status, elapsed):
        nonlocal n_ok, n_fail, n_cached
        if status == "cached":
            n_cached += 1
        elif status == "ok":
            n_ok += 1
        else:
            n_fail += 1
        # Compact line
        print(f"  [{stage}/{idx+1:>2}] {brief['name']:<54} {status:<6} {elapsed:5.1f}s")

    print(f"[{stage}] {len(briefs)} brief(s), concurrency={concurrency}")
    t0 = time.time()
    results = await run_briefs_concurrently(
        briefs, concurrency=concurrency, on_progress=_progress,
    )
    wall = time.time() - t0
    print(f"[{stage}] done — {n_ok} ok, {n_fail} fail, {n_cached} cached, wall={wall:.1f}s")

    # Surface any errors so the user sees them.
    for r in results:
        if not r.ok and r.error:
            print(f"  ERROR [{r.transport}/{r.model}]: {r.error[:200]}")

    return {"total": len(briefs), "ok": n_ok, "fail": n_fail,
            "cached": n_cached, "wall_s": wall}


def cmd_run_all_api(args: list[str], concurrency: int = 8) -> None:
    """End-to-end async runner. Either resume an existing session_dir, or
    start fresh from one or more scenario dirs.

    Usage:
      run_real.py run-all-api <scenario_dir> [<scenario_dir>...]   # fresh
      run_real.py run-all-api <session_dir>                        # resume
    """
    if not args:
        raise SystemExit("run-all-api requires a session_dir or scenario_dir(s)")

    # Detect: a session_dir contains session.json; a scenario_dir contains config.yaml
    first = args[0]
    is_session = os.path.exists(os.path.join(first, "session.json"))

    if is_session:
        if len(args) > 1:
            raise SystemExit("resume mode takes one session_dir")
        session_dir = first
        print(f"[run-all-api] resuming {session_dir}")
    else:
        cmd_prepare(list(args))
        # cmd_prepare prints the session_dir on stdout in run #1; instead,
        # discover it as the most recent dir under SESSION_ROOT.
        sessions = sorted(
            (d for d in os.listdir(SESSION_ROOT)
             if os.path.isdir(os.path.join(SESSION_ROOT, d))),
        )
        if not sessions:
            raise SystemExit("prepare ran but no session_dir found")
        session_dir = os.path.join(SESSION_ROOT, sessions[-1])
        print(f"[run-all-api] fresh session at {session_dir}")

    overall_t0 = time.time()
    stats = {}

    # Stage 1: ATTENTION (already staged by cmd_prepare)
    stats["attention"] = asyncio.run(
        _exec_stage(session_dir, "attention", concurrency)
    )

    # Stage 2: PREDICT — build briefs, then exec
    cmd_predict(session_dir)
    stats["predict"] = asyncio.run(
        _exec_stage(session_dir, "predict", concurrency)
    )

    # Stage 3: VERIFY — build briefs, then exec
    cmd_verify(session_dir)
    stats["verify"] = asyncio.run(
        _exec_stage(session_dir, "verify", concurrency)
    )

    # Stage 4: REFLECT — build briefs (also computes verdicts), then exec
    cmd_reflect(session_dir)
    stats["reflect"] = asyncio.run(
        _exec_stage(session_dir, "reflect", concurrency)
    )

    # Stage 5: FINALIZE (no LLM calls, just KB edits + summary.json)
    cmd_finalize(session_dir)

    overall = time.time() - overall_t0
    print()
    print("=" * 70)
    print(f"[run-all-api] OVERALL wall-clock: {overall:.1f}s")
    print("=" * 70)
    print(f"  {'stage':<10} {'total':>5}  {'ok':>3}  {'fail':>4}  {'cached':>6}  {'wall_s':>7}")
    for stage, s in stats.items():
        print(f"  {stage:<10} {s['total']:>5}  {s['ok']:>3}  {s['fail']:>4}  {s['cached']:>6}  {s['wall_s']:>7.1f}")
    total_calls = sum(s["total"] for s in stats.values())
    total_ok = sum(s["ok"] for s in stats.values())
    total_fail = sum(s["fail"] for s in stats.values())
    print(f"  {'TOTAL':<10} {total_calls:>5}  {total_ok:>3}  {total_fail:>4}")
    print(f"  session_dir: {session_dir}")

    # Cost & behavior summary (loaded from metrics.json written by finalize).
    metrics_path = os.path.join(session_dir, "metrics.json")
    if os.path.exists(metrics_path):
        with open(metrics_path) as f:
            mx = json.load(f)
        t = mx["totals"]; b = mx["behavior"]
        print()
        print(f"  cost            : ${t['cost_usd']:.4f}  ({t['input_tokens']:,} in / {t['output_tokens']:,} out)")
        print(f"  structured_claim: {b['n_with_structured_claim']}/{b['n_predictions']} predictions ({b['structured_claim_coverage_pct']}%)")
        if mx.get("errors"):
            print(f"  silent_failures : {len(mx['errors'])} (see metrics.json[errors])")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "predict", "verify", "verify-api",
                                      "reflect", "finalize", "run-all-api"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="Max concurrent API calls per stage (default 8)")
    ap.add_argument("--test-mode", action="store_true",
                    help="Use cheap/fast models (Sonnet + gpt-5-mini) for iteration. "
                         "Equivalent to COGNI_TEST_MODE=1.")
    ns = ap.parse_args()
    if ns.test_mode and not is_test_mode():
        enable_test_mode()
        # Refresh module-level names in run_real that were imported (by
        # value) before enable_test_mode() ran.
        import sys
        _self = sys.modules[__name__]
        _self.MODEL_OPUS = _llm.MODEL_OPUS
        _self.MODEL_GPT  = _llm.MODEL_GPT
        _self.VENDOR_GPT_MODEL = "gpt-5-mini"
    if is_bedrock_mode():
        from agent.llm.transports import (
            _BEDROCK_PREDICT_MODEL, _BEDROCK_VERIFY1_MODEL, _BEDROCK_VERIFY2_MODEL,
        )
        print("=" * 70)
        print("  COGNI_BEDROCK active  \u2014  all roles on AWS Bedrock "
              f"(region={os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'us-east-1'}):")
        print(f"     predictor/reflector : {_BEDROCK_PREDICT_MODEL}")
        print(f"     verifier 1          : {_BEDROCK_VERIFY1_MODEL}")
        print(f"     verifier 2          : {_BEDROCK_VERIFY2_MODEL}")
        print("  Data stays in your AWS account. Override ids via "
              "COGNI_BEDROCK_{PREDICT,VERIFY1,VERIFY2}_MODEL.")
        print("=" * 70)
    elif is_test_mode():
        # Banner so the run is unmistakable in logs.
        print("=" * 70)
        print("  COGNI_TEST_MODE active  \u2014  models swapped:")
        print("     attention/predict/reflect : claude_opus_4_7  \u2192  claude_sonnet_4_6")
        print("     verifier (gpt)            : gpt-5            \u2192  gpt-5-mini")
        print("  Production runs require unsetting both the env var and --test-mode.")
        print("=" * 70)
    if ns.cmd == "prepare":
        cmd_prepare(ns.args)
    elif ns.cmd == "predict":
        cmd_predict(ns.args[0])
    elif ns.cmd == "verify":
        cmd_verify(ns.args[0])
    elif ns.cmd == "verify-api":
        cmd_verify_api(ns.args[0])
    elif ns.cmd == "reflect":
        cmd_reflect(ns.args[0])
    elif ns.cmd == "finalize":
        cmd_finalize(ns.args[0])
    elif ns.cmd == "run-all-api":
        # ns.args[0] is either:
        #   - existing session_dir (resume)
        #   - one or more scenario dirs (fresh start)
        cmd_run_all_api(ns.args, concurrency=ns.concurrency)

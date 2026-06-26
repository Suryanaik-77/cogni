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
        # ANY scenario with a perceiver/oracle pair qualifies — questions come
        # from questions.json or are auto-generated. So you can drop in a new
        # scenario folder and run it without editing this file.
        _qs_path = os.path.join(scen_dir, "questions.json")
        _can_run = (os.path.exists(_qs_path)
                    or str(cfg.get("generate_questions", "")).lower() in ("true", "1", "yes")
                    or cfg.get("stage") in ("rtl", "synth"))
        if _can_run:
            # Build the world first (live Verilator facts), so questions can be
            # auto-generated from it when no questions.json is provided.
            perceive_input = [cfg["rtl_root"]] if cfg.get("rtl_root") else []
            world = perceiver.perceive(perceive_input)
            reality = oracle.from_existing()

            # Questions: hand-written questions.json wins; otherwise (or when
            # config sets generate_questions: true) auto-generate them from the
            # world facts + rule pack. Same dict shape either way.
            qs_path = os.path.join(scen_dir, "questions.json")
            force_gen = str(cfg.get("generate_questions", "")).lower() in ("true", "1", "yes")
            if os.path.exists(qs_path) and not force_gen:
                with open(qs_path, encoding="utf-8") as f:
                    qs = json.load(f)["questions"]
            else:
                from agent.question_gen import generate_questions
                with open(cfg["pack_path"], encoding="utf-8") as f:
                    _pack = json.load(f)
                qs = generate_questions(world, _pack, stage=cfg.get("stage", "rtl"))
                print(f"[prepare] {name}: auto-generated {len(qs)} questions "
                      f"(no questions.json or generate_questions=true)")

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
            # Always give the predictor the raw source if available — a human
            # reviewer reasons from the code, not from aggregate counts. The
            # oracle (Verilator lint) stays a separate computation, so this is
            # input to cognition, not the answer key. Prevents blanket refusals
            # on count questions ("I can't see the actual RTL").
            if "rtl.source" in world_d["facts"] and "rtl.source" not in focused_facts:
                focused_facts["rtl.source"] = world_d["facts"]["rtl.source"]["value"]

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

def cmd_reflect(session_dir: str, verify: bool = True):
    state = _load_state(session_dir, "session")
    reflect_briefs = []

    for scen_name, scen in state["scenarios"].items():
        kb = KnowledgeBase.load(scen["kb_path"])
        for qrec in scen["questions"]:
            qid = qrec["id"]
            if qrec.get("outcome") != "predicted":
                continue

            # Load verifier verdicts (informational only; no revision round in
            # this run). The panel is opt-in: when it wasn't run, record an empty
            # list rather than fabricating "agrees" placeholders that would read
            # as a passed check in the summary.
            if verify:
                v_gpt = _load_output(f"{scen_name}.{qid}.verify_gpt", session_dir, organs.VERIFIER_SCHEMA) or {"agrees": True, "concerns": [], "suggested_revisions": []}
                v_gem = _load_output(f"{scen_name}.{qid}.verify_gemini", session_dir, organs.VERIFIER_SCHEMA) or {"agrees": True, "concerns": [], "suggested_revisions": []}
                qrec["verifier_verdicts"] = [
                    {"verifier_model": MODEL_GPT, **v_gpt},
                    {"verifier_model": MODEL_GEMINI, **v_gem},
                ]
            else:
                qrec["verifier_verdicts"] = []

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

# Predicate ops the v1 engine understands (agent/predicates.py), incl. the
# LLM-spelling aliases. A dict carrying one of these is a REAL predicate and
# must be preserved verbatim — flattening it to a tag silently kills the gate.
_PRED_OPS = frozenset({
    "all", "any", "not", "tag", "eq", "ne", "lt", "lte", "gt", "gte",
    "in", "contains", "includes", "matches", "exists", "tool",
    "equals", "equal", "==", "not_equals", "notequals", "!=",
})


def _normalize_unless(items) -> list:
    """Normalize an `unless`/`added_unless` list to valid v1 predicate nodes.

    The durable destination is the master pack, which is loaded as v1 — where
    `unless` entries are evaluated as predicate dicts. A BARE STRING node there
    evaluates to False (never blocks), and a real predicate flattened to a tag
    becomes a tag that never matches; either way the intended gate silently
    vanishes. So:

      * real predicate dict ({"op": "in", ...})  -> kept verbatim
      * tag string "foo"                          -> {"op": "tag", "name": "foo"}
      * tag-shaped dict ({"name"/"tag"/...})      -> {"op": "tag", "name": ...}
      * anything truly unparseable                -> folded to a tag so one bad
                                                     entry can't crash reflection

    (Previously this flattened EVERY dict to a string, turning
    `{op:in,key:rtl.foo,values:[...]}` into the tag "rtl.foo" — a gate that
    could never fire.)
    """
    out: list = []
    for x in items or []:
        if isinstance(x, str):
            out.append({"op": "tag", "name": x})
        elif isinstance(x, dict):
            if x.get("op") in _PRED_OPS:
                out.append(x)  # real predicate — preserve verbatim
            else:
                tag = (x.get("name") or x.get("tag") or x.get("condition")
                       or x.get("key") or x.get("value"))
                out.append({"op": "tag",
                            "name": str(tag) if tag is not None
                            else json.dumps(x, sort_keys=True)})
        else:
            out.append({"op": "tag", "name": str(x)})
    return out

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
                # Surface WHY it refused (from the predictor's output, or the
                # earlier no-candidates/no-focus reason stored on the qrec).
                po = qrec.get("predictor_output") or {}
                row["reason"] = (po.get("refusal_reason")
                                 or qrec.get("refusal_reason")
                                 or "no applicable rule / insufficient evidence")
                if po.get("missing_evidence"):
                    row["missing_evidence"] = po["missing_evidence"]
                if po.get("rules_considered_but_rejected"):
                    row["rules_rejected"] = po["rules_considered_but_rejected"]
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
            # The predictor's reasoning behind the claim (why it predicted this).
            row["reason"] = qrec["prediction"].get("rationale", "")
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
                    nr_stage = nr.get("stage")
                    new_rule = Rule(
                        id=nr.get("id") or new_id("r"),
                        statement=nr.get("statement", ""),
                        when=nr.get("when", []),
                        unless=_normalize_unless(nr.get("unless", [])),
                        stage=nr_stage,
                        strength=RuleStrength(nr.get("strength", "tendency")),
                        citations=nr.get("citations", []),
                        rationale=nr.get("rationale", ""),
                        # Carry predicts so the learned rule is GRADEABLE next
                        # run (key+channel+band). Without this it lands with an
                        # empty predicts and can never be checked or promoted.
                        predicts=nr.get("predicts", []),
                        # Born v1: when/unless/predicts are predicate-dict shaped,
                        # so the rule must be marked v1 and given a v1 stage
                        # filter. Under the v0 default (schema_version=0),
                        # applies_to() takes the tag-set path and these dict
                        # predicates would never match — the freshly-learned rule
                        # would be silently unrecallable within this same run.
                        schema_version=1,
                        applies_to_v1={"stage": [nr_stage] if nr_stage else []},
                    )
                new_strength = RuleStrength(e["new_strength"]) if e.get("new_strength") else None
                edit = KBEdit(
                    id=new_id("kbe"), kind=kind,
                    target_rule_id=e.get("target_rule_id"),
                    new_rule=new_rule, new_strength=new_strength,
                    # Normalize to valid v1 predicate nodes so a real predicate
                    # survives into the pack instead of being flattened to a
                    # never-matching tag (and a malformed entry can't crash).
                    added_unless=_normalize_unless(e.get("added_unless", [])),
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
                    # Warn loudly (not silently) if a learned rule is born with
                    # no gradeable predict — promote will skip it, so the lesson
                    # is lost unless a human notices. (The reflector schema now
                    # blocks `[{}]`; this still catches an empty `[]`.)
                    if not any(isinstance(p, dict) and p.get("measurement_key")
                               for p in (edit.new_rule.predicts or [])):
                        print(f"[finalize] WARNING: learned rule "
                              f"{edit.new_rule.id} has no gradeable predicts — "
                              f"promote will SKIP it (scenario={scen_name}, "
                              f"qid={qid})", flush=True)
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


def cmd_promote(session_dir: str, *, pack_path: str = "packs/rtl/rules.json",
                apply: bool = False, scenario: str | None = None,
                include_ungradeable: bool = False) -> None:
    """Promote a session's learned KB edits back into the master pack.

    Dry-run by default (prints the plan); --apply writes after backing the
    pack up to <pack>.bak. This is what gives the agent MEMORY across runs:
    without it every run reloads the pristine pack and re-invents the same
    rule. See agent/promote.py for the dedup + quality-gate rules.
    """
    from agent.promote import promote_session
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    res = promote_session(session_dir, pack_path, apply=apply,
                          scenario=scenario,
                          include_ungradeable=include_ungradeable, today=today)

    print("=" * 60)
    print(f"  PROMOTE  session={res['session']}")
    print(f"  pack    ={res['pack_path']}")
    print(f"  mode    ={'APPLY (writing)' if apply else 'dry-run (preview only)'}")
    print("=" * 60)
    if not res["plan"]:
        print("  nothing to promote (no kb_edits.jsonl edits matched).")
        return
    for p in res["plan"]:
        mark = "[apply]" if p["decision"] == "apply" else "[skip ]"
        print(f"  {mark} {p['op']:<11} {p['rule_id']}")
        if p["detail"]:
            print(f"          -> {p['detail']}")
    n_apply = sum(1 for p in res["plan"] if p["decision"] == "apply")
    print("-" * 60)
    if res["applied"]:
        s = res["summary"]
        print(f"  WROTE {n_apply} change(s): {s}")
        print(f"  backup: {res['backup']}")
    else:
        if n_apply:
            print(f"  {n_apply} change(s) WOULD be applied. Re-run with --apply to write.")
        else:
            print("  no changes to apply (all skipped — see reasons above).")
    print("=" * 60)


def cmd_fix_phase(session_dir: str, concurrency: int = 4) -> None:
    """PHASE 3 (opt-in via --with-fixes): close the fix loop with reality.

    Using the rulebook AS UPDATED by the learn phase, detect where the design
    violates rules, propose a patch per violation, apply the patches to a
    COPY of the source, and re-run Verilator to confirm. The real source
    files are never touched — the operator applies the confirmed patches.
    """
    from agent.sweep import sweep
    from agent.fixer import propose_fixes_sync
    from agent.fix_verify import verify_fixes, format_report

    state = _load_state(session_dir, "session")
    print()
    print("=" * 70)
    print("[with-fixes] PHASE 3 - detect violations -> propose -> apply-to-copy -> re-check")
    print("=" * 70)

    for scen_name, scen in state["scenarios"].items():
        cfg = scen.get("config") or read_config(
            os.path.join(scen["scenario_dir"], "config.yaml"))
        stage = cfg.get("stage", "rtl")
        if stage != "rtl":
            print(f"[with-fixes] {scen_name}: stage={stage} unsupported; skipping.")
            continue

        # Rebuild world (LIVE Verilator facts) + reality (answer key), exactly
        # as prepare did. Using the live world means human/AI code-origin facts
        # are present, so origin-gated rules fire correctly.
        adapter = make_adapter(cfg)
        perceiver = Perceiver(adapter)
        oracle = make_oracle(cfg)
        perceive_input = [cfg["rtl_root"]] if cfg.get("rtl_root") else []
        world = perceiver.perceive(perceive_input)
        reality = oracle.from_existing()

        # Load the UPDATED rulebook (post-learn) for the sweep.
        kb_after = os.path.join(session_dir, f"{scen_name}_kb_after.json")
        pack_path = kb_after if os.path.exists(kb_after) else cfg["pack_path"]
        with open(pack_path, encoding="utf-8") as f:
            pack = json.load(f)
        pack["__path__"] = pack_path

        rep = sweep(pack, world, reality, stage_filter=stage)
        violations = rep.violations()
        print(f"[with-fixes] {scen_name}: {len(violations)} violation(s) detected")
        if not violations:
            continue

        rtl_root = (cfg.get("oracle") or {}).get("rtl_root") \
            or os.path.join(scen["scenario_dir"], "rtl")
        if not (rtl_root and os.path.isdir(rtl_root)):
            print(f"[with-fixes] {scen_name}: no rtl_root; cannot fix.")
            continue

        fix_dir = os.path.join(session_dir, f"{scen_name}_fixes")
        fixes = propose_fixes_sync(violations, fix_dir, rtl_root=rtl_root,
                                   concurrency=concurrency)
        patches = [{"target_file": f.target_file,
                    "patch_unified_diff": f.patch_unified_diff,
                    "fixed_file": f.fixed_file,
                    "rule_id": f.rule_id}
                   for f in fixes if (f.patch_unified_diff or f.fixed_file)]
        print(f"[with-fixes] {scen_name}: {len(patches)} patch(es) proposed")
        if not patches:
            continue

        top = (cfg.get("perceiver") or {}).get("top")
        res = verify_fixes(patches, rtl_root, top=top)
        print(format_report(res))


# ---------------------------------------------------------------------------
# READY — RTL -> gate-level readiness gate, with auto-fix-until-ready loop
# ---------------------------------------------------------------------------

def _repoint_cfg(cfg: dict, orig_root: str, new_root: str) -> dict:
    """Copy `cfg` with every RTL path under `orig_root` rewritten to `new_root`,
    so the perceiver + oracle measure the WORKING copy instead of the original."""
    import copy
    c = copy.deepcopy(cfg)
    ao = os.path.abspath(orig_root)

    def rep(p):
        if not isinstance(p, str):
            return p
        ap = os.path.abspath(p)
        if ap == ao:
            return new_root
        if ap.startswith(ao + os.sep):
            return os.path.join(new_root, os.path.relpath(ap, ao))
        return p

    def rep_files(val):
        if isinstance(val, str):
            return ",".join(rep(x.strip()) for x in val.split(",") if x.strip())
        if isinstance(val, list):
            return [rep(x) for x in val]
        return val

    for sub in ("perceiver", "oracle"):
        d = c.get(sub) or {}
        if "rtl_root" in d:
            d["rtl_root"] = rep(d["rtl_root"])
        if "rtl_files" in d:
            d["rtl_files"] = rep_files(d["rtl_files"])
        c[sub] = d
    if "rtl_root" in c:
        c["rtl_root"] = rep(c["rtl_root"])
    return c


def cmd_ready(args: list[str], *, max_rounds: int = 3, fix: bool = True,
              netlist: bool = True, learn: bool = True, concurrency: int = 4) -> None:
    """RTL -> gate-level readiness gate, with the REAL netlist step.

    One command, the whole loop:
      1. RTL gate   : sweep the RTL (live Verilator) -> GO/NO-GO, grouped by the
                      downstream stage each violation would break.
      2. fix        : (unless --no-fix) propose a fix per blocker, apply to a
                      WORKING COPY, re-measure. Repeat until RTL is clean.
      3. NETLIST gate: once RTL is clean, actually SYNTHESIZE with Yosys and
                      check the real netlist for hazards that only appear in
                      gates (inferred latch cells, multidriven nets). Any found
                      are fed back as blockers and fixed in RTL.
      4. repeat until the design passes BOTH gates, stops progressing, or hits
         --max-rounds.

    The original source is never touched; cleaned RTL + one `.patch` per changed
    file land under <scenario>/ready_out/. If Yosys isn't installed the netlist
    gate reports UNVERIFIED (never a false GO).
    """
    from agent.sweep import sweep
    from agent.fixer import propose_fixes_sync
    from agent.fix_verify import (lint_counts, gather_rtl_files,
                                  synthesize_diffs, _write_fixed_file, _apply_patch)
    from agent import gate as gate_mod
    import shutil

    if not args:
        raise SystemExit("ready requires a scenario_dir")
    scen_dir = args[0]
    cfg = read_config(os.path.join(scen_dir, "config.yaml"))
    stage = cfg.get("stage", "rtl")
    if stage != "rtl":
        raise SystemExit(f"ready: stage={stage!r} unsupported (rtl only for now).")

    orig_root = (cfg.get("oracle") or {}).get("rtl_root") or cfg.get("rtl_root")
    if not (orig_root and os.path.isdir(orig_root)):
        raise SystemExit("ready: need oracle.rtl_root pointing at a directory of RTL.")
    top = (cfg.get("perceiver") or {}).get("top")

    with open(cfg["pack_path"], encoding="utf-8") as f:
        pack = json.load(f)
    pack["__path__"] = cfg["pack_path"]

    # Working copy we mutate across rounds; the original is read-only.
    out_root = os.path.abspath(os.path.join(scen_dir, "ready_out"))
    if os.path.exists(out_root):
        shutil.rmtree(out_root)
    shutil.copytree(orig_root, out_root)
    rounds_root = os.path.join(out_root, ".cogni_rounds")
    os.makedirs(rounds_root, exist_ok=True)

    def measure_rtl():
        """Sweep the WORKING copy with live Verilator: (violations, lint)."""
        cfg2 = _repoint_cfg(cfg, orig_root, out_root)
        world = Perceiver(make_adapter(cfg2)).perceive([])
        reality = make_oracle(cfg2).from_existing()
        rep = sweep(pack, world, reality, stage_filter=stage)
        return rep.violations(), lint_counts(gather_rtl_files(out_root), top=top)

    def run_synth():
        """Synthesize the working copy. (measurements|None, status, note)."""
        from adapters.synth.yosys import runner as yosys_runner
        try:
            yr = yosys_runner.from_rtl(out_root, top=top)
            return yr.measurements, "ok", ""
        except FileNotFoundError as e:
            return None, "unavailable", str(e)
        except Exception as e:  # synthesis failed (e.g. unsupported SV)
            return None, "failed", (str(e).splitlines() or ["synthesis error"])[0]

    def apply_fixes(rulechecks, rnd):
        fixdir = os.path.join(rounds_root, f"round{rnd}")
        fixes = propose_fixes_sync(rulechecks, fixdir, rtl_root=out_root,
                                   concurrency=concurrency)
        n_apply = 0
        for fx in fixes:
            if fx.fixed_file and fx.target_file:
                ok = _write_fixed_file(out_root, fx.target_file, fx.fixed_file)
            elif fx.patch_unified_diff:
                ok = _apply_patch(out_root, fx.patch_unified_diff)
            else:
                ok = False
            n_apply += 1 if ok else 0
        return len(fixes), n_apply

    print("=" * 70)
    print(f"[ready] RTL -> gate-level readiness (one command): {scen_dir}")
    print("=" * 70)

    rnd = 0
    prev_total = None
    seen_surprise: dict = {}   # measurement_key -> surprise (first occurrence)
    rtl_blockers, net_blockers, net_status, net_note = [], [], "not_run", ""
    while True:
        rtl_v, lints = measure_rtl()
        rtl_blockers = gate_mod.classify(rtl_v)
        print(gate_mod.format_readiness(
            rtl_blockers, lint=lints, title="RTL READINESS (Verilator lint)",
            round_no=(rnd or None), max_rounds=max_rounds))

        net_blockers, net_checks = [], []
        if netlist and not rtl_blockers:
            meas, net_status, net_note = run_synth()
            if net_status == "ok":
                net_blockers = gate_mod.netlist_blockers(meas)
                net_checks = [gate_mod.blocker_to_rulecheck(b) for b in net_blockers]
                # SURPRISE: RTL gate was clean but synthesis found a hazard the
                # rulebook didn't predict -> record it once to learn from later.
                if learn:
                    from agent.gate_learn import netlist_surprises
                    for s in netlist_surprises(rtl_blockers, net_blockers):
                        seen_surprise.setdefault(s["measurement_key"], s)
                netlint = {k: meas.get(k) for k in
                           ("synth.total_cells", "synth.warnings.latch",
                            "synth.warnings.multidriven") if meas.get(k) is not None}
                print(gate_mod.format_readiness(
                    net_blockers, lint=netlint,
                    title="NETLIST GATE (real Yosys synthesis)"))
            else:
                print("\n=== NETLIST GATE (real Yosys synthesis) ===")
                print(f"VERDICT: UNVERIFIED -- synthesis {net_status}: {net_note}")

        blockers = rtl_blockers + net_blockers
        total = len(blockers)
        if total == 0:
            break
        if not fix or rnd >= max_rounds:
            break
        if prev_total is not None and total >= prev_total:
            print("[ready] no progress this round -- stopping the loop.")
            break
        prev_total = total
        rnd += 1
        nprop, napp = apply_fixes(rtl_v + net_checks, rnd)
        print(f"\n[ready] round {rnd}: proposed {nprop} fix(es), applied {napp}")

    # LEARN: mint a rule from each netlist surprise and promote it into the
    # master pack, so next run's RTL gate anticipates the hazard pre-synthesis.
    learned = {"learned": 0}
    if learn and seen_surprise:
        from agent.gate_learn import learn_from_surprises
        design = cfg.get("name") or os.path.basename(os.path.normpath(scen_dir))
        today = datetime.now(timezone.utc).date().isoformat()
        learned = learn_from_surprises(
            list(seen_surprise.values()),
            session_dir=os.path.join(out_root, ".learn"),
            pack_path=cfg["pack_path"], design=design, today=today)
        print()
        print("=== LEARNED (netlist surprises -> master pack) ===")
        for a in (learned.get("promote") or {}).get("plan", []):
            print(f"  [{a['decision']:5}] {a['op']:9} {a['rule_id']}  {a['detail']}")

    # Emit cleaned RTL + one .patch per changed file.
    diffs = synthesize_diffs(orig_root, out_root)
    patch_dir = os.path.join(out_root, "_patches")
    if diffs:
        os.makedirs(patch_dir, exist_ok=True)
        for rel, d in diffs.items():
            pf = os.path.join(patch_dir, rel.replace(os.sep, "__") + ".patch")
            with open(pf, "w", encoding="utf-8") as fh:
                fh.write(d if d.endswith("\n") else d + "\n")

    # Final combined verdict over BOTH gates.
    final_blockers = rtl_blockers + net_blockers
    if not netlist:
        final = "GO" if not rtl_blockers else "NO-GO"
    elif net_status == "ok":
        final = "GO" if not final_blockers else "NO-GO"
    elif net_status in ("unavailable", "failed"):
        final = ("GO (RTL clean) / NETLIST UNVERIFIED" if not rtl_blockers else "NO-GO")
    else:
        final = "NO-GO"  # netlist never reached (RTL never went clean)

    print()
    print("=" * 70)
    print(f"[ready] FINAL VERDICT: {final}   (fix rounds run: {rnd})")
    print(f"        cleaned RTL : {out_root}")
    print(f"        patches     : "
          + (f"{patch_dir}  ({len(diffs)} file(s) changed)" if diffs
             else "none (RTL unchanged)"))
    if learn:
        nlearn = learned.get("learned", 0)
        promo = (learned.get("promote") or {}).get("summary") or {}
        print(f"        learned     : {nlearn} netlist surprise(s) -> "
              f"pack +{promo.get('added', 0)} new, {promo.get('strengthened', 0)} strengthened")
    if netlist and net_status in ("unavailable", "failed"):
        print(f"        netlist     : {net_status} -- {net_note}")
    if final_blockers:
        print(f"        UNFIXED ({len(final_blockers)}):")
        for b in final_blockers:
            print(f"          - [{b.rule_id}] {b.measurement_key}={b.measured} "
                  f"-> {', '.join(b.downstream_stages) or 'rtl'}")
    print("=" * 70)


def cmd_run_all_api(args: list[str], concurrency: int = 8,
                    with_fixes: bool = False, promote: bool = False,
                    include_ungradeable: bool = False, verify: bool = False) -> None:
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

    # Stage 3: VERIFY — opt-in via --verify. With a LIVE ORACLE the verdict is
    # computed from the tool's measurements (see cmd_reflect/make_verdict), not
    # the LLM panel, so the panel is informational only and OFF by default.
    # Enable it for no-oracle judgment questions or to scrutinize reasoning.
    if verify:
        cmd_verify(session_dir)
        stats["verify"] = asyncio.run(
            _exec_stage(session_dir, "verify", concurrency)
        )
    else:
        print("[verify] skipped (pass --verify to run the cross-LLM panel). "
              "Verdicts come from the oracle.")

    # Stage 4: REFLECT — build briefs (also computes verdicts), then exec
    cmd_reflect(session_dir, verify=verify)
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

    # PHASE 3 (opt-in): detect violations -> propose -> apply-to-copy -> re-check.
    if with_fixes:
        cmd_fix_phase(session_dir, concurrency=max(2, concurrency // 2))

    # Opt-in: write what was learned back into each scenario's MASTER pack, so
    # the NEXT run recalls it instead of re-learning. Without this every run
    # reloads the pristine pack and re-derives the same rules. One pack per
    # scenario; promote each once.
    if promote:
        from agent.promote import promote_session
        state = _load_state(session_dir, "session")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        promoted_packs: set[str] = set()
        print()
        for scen_name, scen in state["scenarios"].items():
            pack_path = scen.get("config", {}).get("pack_path")
            if not pack_path or pack_path in promoted_packs:
                continue
            promoted_packs.add(pack_path)
            res = promote_session(session_dir, pack_path, apply=True,
                                  scenario=scen_name,
                                  include_ungradeable=include_ungradeable,
                                  today=today)
            n_apply = sum(1 for p in res["plan"] if p["decision"] == "apply")
            print(f"[promote] {scen_name} -> {pack_path}: "
                  f"{n_apply} change(s) written"
                  + (f", backup {res.get('backup')}" if res.get("applied") else
                     " (nothing gradeable to promote)"))
            for p in res["plan"]:
                mark = "[apply]" if p["decision"] == "apply" else "[skip ]"
                print(f"           {mark} {p['op']:<10} {p['rule_id']}  {p['detail']}")


if __name__ == "__main__":
    # RTL/question text contains UTF-8 (em-dashes etc.). Some shells expose a
    # latin-1 stdout, which makes print() crash on those chars. Force UTF-8 so
    # output never dies on a stray character regardless of locale.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "predict", "verify", "verify-api",
                                      "reflect", "finalize", "run-all-api",
                                      "promote", "ready"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="Max concurrent API calls per stage (default 8)")
    ap.add_argument("--apply", action="store_true",
                    help="promote: actually write learned rules back into the "
                         "master pack (default is a dry-run preview).")
    ap.add_argument("--pack", default="packs/rtl/rules.json",
                    help="promote: master pack to merge learning into.")
    ap.add_argument("--scenario", default=None,
                    help="promote: only promote edits from this scenario name.")
    ap.add_argument("--include-ungradeable", action="store_true",
                    help="promote: also promote new rules with no predicts "
                         "(tool/plumbing notes). Off by default.")
    ap.add_argument("--promote", action="store_true",
                    help="run-all-api: after the run, write learned rules back "
                         "into each scenario's MASTER pack in the SAME command "
                         "(so the next run recalls them instead of re-learning).")
    ap.add_argument("--test-mode", action="store_true",
                    help="Use cheap/fast models (Sonnet + gpt-5-mini) for iteration. "
                         "Equivalent to COGNI_TEST_MODE=1.")
    ap.add_argument("--with-fixes", action="store_true",
                    help="After predict+learn, run PHASE 3: detect violations, "
                         "propose patches, apply to a COPY, and re-run Verilator "
                         "to confirm. Only the run-all-api command. rtl stage only.")
    ap.add_argument("--verify", action="store_true",
                    help="run-all-api: run the cross-LLM verifier panel "
                         "(verifier 1 + verifier 2). OFF by default — with a "
                         "live oracle the tool is ground truth and the panel is "
                         "informational only. Enable it for no-oracle judgment "
                         "questions or to scrutinize a prediction's reasoning.")
    ap.add_argument("--max-rounds", type=int, default=3,
                    help="ready: max auto-fix rounds before giving up (default 3).")
    ap.add_argument("--no-fix", action="store_true",
                    help="ready: only emit the GO/NO-GO readiness verdict; do "
                         "not run the auto-fix loop.")
    ap.add_argument("--no-netlist", action="store_true",
                    help="ready: skip the real Yosys synthesis / netlist gate; "
                         "stop at RTL readiness.")
    ap.add_argument("--no-learn", action="store_true",
                    help="ready: do not mint/promote rules from netlist "
                         "surprises (RTL clean but synthesis found a hazard).")
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
        print("  COGNI_BEDROCK active  --  all roles on AWS Bedrock "
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
        print("  COGNI_TEST_MODE active  --  models swapped:")
        print("     attention/predict/reflect : claude_opus_4_7  ->  claude_sonnet_4_6")
        print("     verifier (gpt)            : gpt-5            ->  gpt-5-mini")
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
        cmd_run_all_api(ns.args, concurrency=ns.concurrency, with_fixes=ns.with_fixes,
                        promote=ns.promote, include_ungradeable=ns.include_ungradeable,
                        verify=ns.verify)
    elif ns.cmd == "promote":
        cmd_promote(ns.args[0], pack_path=ns.pack, apply=ns.apply,
                    scenario=ns.scenario,
                    include_ungradeable=ns.include_ungradeable)
    elif ns.cmd == "ready":
        cmd_ready(ns.args, max_rounds=ns.max_rounds, fix=not ns.no_fix,
                  netlist=not ns.no_netlist, learn=not ns.no_learn,
                  concurrency=ns.concurrency)

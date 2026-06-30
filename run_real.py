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
from agent.memory import DesignMemory, design_id_from_config, get_or_create

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

    # Record run start in per-design memory
    for scen_name, scen in state["scenarios"].items():
        cfg = scen.get("config", {})
        mem = get_or_create(scen_name)
        mem.set_metadata(
            scenario_dir=scen.get("scenario_dir"),
            pack_path=cfg.get("pack_path"),
            stage=cfg.get("stage"),
        )
        mem.record_run_start(session_dir, command="run-all-api")

    return session_dir


# ---------------------------------------------------------------------------
# Stage 2: PREDICT — read attention outputs, write predict briefs
# ---------------------------------------------------------------------------

def cmd_predict(session_dir: str):
    state = _load_state(session_dir, "session")
    for scen_name in state.get("scenarios", {}):
        get_or_create(scen_name).set_phase("predicting", session_dir=session_dir)
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
    for scen_name in state.get("scenarios", {}):
        get_or_create(scen_name).set_phase("verifying", session_dir=session_dir)
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
    for scen_name in state.get("scenarios", {}):
        get_or_create(scen_name).set_phase("reflecting", session_dir=session_dir)
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

    # ---- Update per-design memory ----
    session_id = os.path.basename(os.path.normpath(session_dir))
    for scen_name, scen_sum in summary.get("scenarios", {}).items():
        mem = get_or_create(scen_name)
        mem.set_phase("finalizing", session_dir=session_dir)

        # Collect findings from verdicts
        verdicts_path = os.path.join(session_dir, "verdicts.jsonl")
        if os.path.exists(verdicts_path):
            with open(verdicts_path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    vd = json.loads(line)
                    if vd.get("scenario") != scen_name:
                        continue
                    notes = vd.get("notes", "")
                    # Extract measurement from verdict notes (format: "[channel] key: actual=X, ...")
                    if "actual=" in notes:
                        for part in notes.split(";"):
                            part = part.strip()
                            if "actual=" not in part:
                                continue
                            key_part = part.split(":")[0].strip()
                            # Strip channel prefix like "[intervals] "
                            if "]" in key_part:
                                key_part = key_part.split("]", 1)[1].strip()
                            actual_str = part.split("actual=")[1].split(",")[0].strip()
                            try:
                                actual = float(actual_str)
                            except ValueError:
                                actual = actual_str
                            mem.record_finding(
                                key_part, actual,
                                session_id=session_id,
                                verdict=vd.get("kind"),
                            )

        # Collect rules learned from kb_edits
        rules_learned = []
        edits_path = os.path.join(session_dir, "kb_edits.jsonl")
        if os.path.exists(edits_path):
            with open(edits_path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    e = json.loads(line)
                    if e.get("scenario") != scen_name:
                        continue
                    if e.get("kind") == "add" and e.get("new_rule", {}).get("id"):
                        rules_learned.append(e["new_rule"]["id"])

        cost = (summary.get("metrics") or {}).get("cost_usd", 0)
        mem.record_run_end(
            session_id,
            stats={
                "n_predicted": summary["totals"].get("n_predicted", 0),
                "n_right": summary["totals"].get("n_right", 0),
                "n_wrong": summary["totals"].get("n_wrong", 0),
                "n_refused": summary["totals"].get("n_refused", 0),
                "n_kb_edits": summary["totals"].get("n_kb_edits", 0),
                "cost_usd": cost,
            },
            rules_learned=rules_learned,
        )

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


def cmd_ready(args: list[str], *, max_rounds: int = 0, max_passes: int = 3,
              fix: bool = True, netlist: bool = True, learn: bool = True,
              optimize: bool = True, apply_to_source: bool = False,
              concurrency: int = 4) -> None:
    """RTL -> gate-level readiness gate, with optimization + learning.

    One command, the whole loop. INNER loop (one pass):
      1. RTL gate   : sweep the RTL (live Verilator) -> GO/NO-GO, grouped by the
                      downstream stage each violation would break.
      2. fix        : (unless --no-fix) propose a fix per blocker, apply to a
                      WORKING COPY, re-measure. Repeat until RTL is clean,
                      stops progressing, or hits --max-rounds.
      3. OPTIMIZE   : (unless --no-optimize) once RTL is lint-clean, run the
                      RTL optimizer to propose synthesis-friendly improvements.
                      Re-verify with Verilator; reject any file that introduces
                      new warnings. Only accepted optimizations survive.
      4. NETLIST gate: actually SYNTHESIZE with Yosys and check the real netlist
                      for hazards that only appear in gates. If netlist issues
                      are found, loop back to step 1 (RTL fix).

    OUTER loop (--max-passes): after each pass, LEARN+promote the netlist
    surprises into the master pack, then RE-RUN the whole inner loop with the
    updated rulebook -- repeating until the design comes out clean OR it can no
    longer improve (a pass that applies no fix and learns nothing == fixpoint).

    HUMAN-IN-THE-LOOP: at the end, if --apply is not set, the user is asked
    whether to apply the changes to the original source files.
    """
    from agent.sweep import sweep
    from agent.fixer import propose_fixes_sync
    from agent.fix_verify import (lint_counts as lint_counts_fn, gather_rtl_files,
                                  synthesize_diffs, _write_fixed_file, _apply_patch)
    from agent.optimizer import optimize_rtl_sync
    from agent import gate as gate_mod
    from agent.rule_health import (RuleHealthStore, diagnose_remaining,
                                   record_clean_rules, apply_corrections,
                                   persist_pack, format_diagnoses,
                                   format_corrections)
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
    design = cfg.get("name") or os.path.basename(os.path.normpath(scen_dir))

    # --- Source hash: skip if unchanged since last GO ---
    import hashlib
    def _source_hash(root):
        h = hashlib.sha256()
        from agent.fix_verify import gather_rtl_files as _grf
        for fp in _grf(root):
            with open(fp, "rb") as fh:
                h.update(fh.read())
        return h.hexdigest()[:16]

    src_hash = _source_hash(orig_root)
    mem = get_or_create(design)
    prior_verdict = mem.data["current_state"].get("readiness_verdict", "")
    if (mem.source_hash == src_hash
            and prior_verdict.startswith("GO")):
        print(f"[ready] design '{design}' already GO and source unchanged "
              f"— skipping. Use --force to re-run.")
        if "--force" not in args:
            return

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

    # Match the oracle's Verilator flags so fix verification sees the
    # same warnings (LATCH, BLKSEQ, …) that the sweep does.
    _vlint_extra = ["-Wall", "-Wno-fatal"]

    def _vlint():
        return lint_counts_fn(gather_rtl_files(out_root), top=top,
                              extra_args=_vlint_extra)

    def measure_rtl():
        """Sweep the WORKING copy with live Verilator: (violations, lint)."""
        cfg2 = _repoint_cfg(cfg, orig_root, out_root)
        # Drop any static findings file — the fix loop needs LIVE
        # Verilator measurements from the working copy, not stale
        # hardcoded counts from the original scenario.
        if "oracle" in cfg2:
            cfg2["oracle"].pop("findings_path", None)
        world = Perceiver(make_adapter(cfg2)).perceive([])
        reality = make_oracle(cfg2).from_existing()
        rep = sweep(pack, world, reality, stage_filter=stage)
        return rep.violations(), _vlint()

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

    def apply_fixes(rulechecks, label):
        fixdir = os.path.join(rounds_root, f"round_{label}")
        mem_ctx = mem.format_fixer_context()
        fixes = propose_fixes_sync(rulechecks, fixdir, rtl_root=out_root,
                                   memory_context=mem_ctx,
                                   concurrency=concurrency)
        conf_rank = {"likely": 0, "high": 0, "uncertain": 1,
                     "medium": 1, "unlikely": 2, "low": 2}
        fixes.sort(key=lambda f: conf_rank.get(f.confidence, 3))

        baseline_total = sum(_vlint().values())

        n_apply = 0
        touched: set[str] = set()
        for fx in fixes:
            tf = fx.target_file or ""
            if tf in touched:
                continue

            dest = os.path.join(out_root, tf) if tf else ""
            backup = None
            if dest and os.path.exists(dest):
                with open(dest, encoding="utf-8") as fh:
                    backup = fh.read()

            if fx.fixed_file and tf:
                ok = _write_fixed_file(out_root, tf, fx.fixed_file)
            elif fx.patch_unified_diff:
                ok = _apply_patch(out_root, fx.patch_unified_diff)
            else:
                ok = False

            if not ok:
                mem.record_fix_attempt(
                    fx.rule_id, tf, "FAILED", session_id=session_id,
                    round_label=label, detail="patch did not apply")
                continue

            post = _vlint()
            post_total = sum(post.values())
            if post_total < baseline_total:
                n_apply += 1
                touched.add(tf)
                before = baseline_total
                baseline_total = post_total
                print(f"  [fix] ACCEPTED {tf} ({fx.rule_id}): "
                      f"warnings {before} -> {post_total}")
                mem.record_fix_attempt(
                    fx.rule_id, tf, "ACCEPTED", session_id=session_id,
                    round_label=label,
                    detail=f"warnings {before} -> {post_total}")
            else:
                if backup is not None:
                    with open(dest, "w", encoding="utf-8") as fh:
                        fh.write(backup)
                print(f"  [fix] REVERTED {tf} ({fx.rule_id}): "
                      f"warnings unchanged ({post_total})")
                mem.record_fix_attempt(
                    fx.rule_id, tf, "REVERTED", session_id=session_id,
                    round_label=label,
                    detail=f"warnings unchanged ({post_total})")
        mem.save()
        return len(fixes), n_apply

    def run_pass(pass_no):
        """One full inner loop: fix RTL + netlist blockers until the design is
        clean, stops progressing, or hits --max-rounds. Returns the end-state."""
        rnd = 0
        prev_lint_total: int | None = None
        surprises: dict = {}
        rtl_b, net_b, net_st, net_nt = [], [], "not_run", ""
        fixes_applied = 0
        while True:
            rtl_v, lints = measure_rtl()
            rtl_b = gate_mod.classify(rtl_v)
            print(gate_mod.format_readiness(
                rtl_b, lint=lints, title=f"RTL READINESS (pass {pass_no})",
                round_no=(rnd or None), max_rounds=(max_rounds or None)))

            net_b, net_checks = [], []
            if netlist and not rtl_b:
                # ---- OPTIMIZE before synthesis ----
                if optimize:
                    opt_dir = os.path.join(rounds_root, f"optimize_p{pass_no}r{rnd}")
                    proposals = optimize_rtl_sync(
                        out_root, opt_dir, lint_counts=lints,
                        memory_context=mem.format_fixer_context(),
                        concurrency=concurrency)
                    if proposals:
                        print(f"\n=== RTL OPTIMIZATION (pass {pass_no}) ===")
                        for prop in proposals:
                            backup = prop.original_content
                            dest = os.path.join(out_root, prop.target_file)
                            with open(dest, "w", encoding="utf-8") as fh:
                                fh.write(prop.optimized_content)
                            post_lints = _vlint()
                            new_warnings = sum(post_lints.values())
                            if new_warnings > sum(lints.values()):
                                # Reject: optimization introduced warnings
                                with open(dest, "w", encoding="utf-8") as fh:
                                    fh.write(backup)
                                print(f"  REJECTED {prop.target_file}: "
                                      f"introduced {new_warnings} warning(s) "
                                      f"(was {sum(lints.values())}), reverted")
                            else:
                                lints = post_lints
                                for cs in prop.changes_summary:
                                    print(f"  ACCEPTED {prop.target_file}: {cs}")
                        # Re-measure RTL after optimization to confirm still clean
                        rtl_v, lints = measure_rtl()
                        rtl_b = gate_mod.classify(rtl_v)
                        if rtl_b:
                            print("[optimize] optimization introduced RTL "
                                  "blockers — looping back to fix...")
                            continue

                meas, net_st, net_nt = run_synth()
                if net_st == "ok":
                    net_b = gate_mod.netlist_blockers(meas)
                    net_checks = [gate_mod.blocker_to_rulecheck(b) for b in net_b]
                    # SURPRISE: RTL clean but synthesis found a hazard the
                    # rulebook didn't predict -> record once to learn from.
                    if learn:
                        from agent.gate_learn import netlist_surprises
                        for s in netlist_surprises(rtl_b, net_b):
                            surprises.setdefault(s["measurement_key"], s)
                    netlint = {k: meas.get(k) for k in
                               ("synth.total_cells", "synth.warnings.latch",
                                "synth.warnings.multidriven") if meas.get(k) is not None}
                    print(gate_mod.format_readiness(
                        net_b, lint=netlint,
                        title="NETLIST GATE (real Yosys synthesis)"))
                else:
                    print("\n=== NETLIST GATE (real Yosys synthesis) ===")
                    print(f"VERDICT: UNVERIFIED -- synthesis {net_st}: {net_nt}")

            cur = rtl_b + net_b
            if not cur:
                break
            if not fix:
                break
            if max_rounds and rnd >= max_rounds:
                print(f"[ready] hit --max-rounds ({max_rounds}) -- stopping.")
                break
            lint_total = sum(lints.values())
            if prev_lint_total is not None and lint_total >= prev_lint_total:
                print(f"[ready] no progress this round (warnings still "
                      f"{lint_total}) -- ending inner loop.")
                break
            prev_lint_total = lint_total
            rnd += 1
            nprop, napp = apply_fixes(rtl_v + net_checks, f"p{pass_no}r{rnd}")
            fixes_applied += napp
            print(f"\n[ready] pass {pass_no} round {rnd}: proposed {nprop} "
                  f"fix(es), {napp} verified by Verilator")
        return {"rtl": rtl_b, "net": net_b, "net_status": net_st,
                "net_note": net_nt, "surprises": surprises,
                "fixes_applied": fixes_applied}

    # Set up memory tracking before the loop so apply_fixes can record attempts.
    mem.set_metadata(scenario_dir=scen_dir, pack_path=cfg.get("pack_path"),
                     stage=stage)
    session_id = f"ready_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    mem.record_run_start(out_root, command="ready", session_id=session_id)

    if mem.has_been_processed():
        ctx = mem.format_fixer_context()
        if ctx:
            print(f"[memory] loaded prior knowledge for '{design}' "
                  f"({mem.run_count()} prior run(s))")

    print("=" * 70)
    print(f"[ready] RTL -> gate-level readiness (one command): {scen_dir}")
    print("=" * 70)

    # OUTER LOOP: run the inner loop, learn+promote its netlist surprises, then
    # RE-RUN with the updated rulebook -- repeating until the design comes out
    # clean OR it can no longer improve (a pass that applies no fix AND learns
    # nothing new == fixpoint; running again would only repeat itself).
    pass_no = 0
    total_learned = 0
    promo = {"added": 0, "strengthened": 0}
    rtl_blockers, net_blockers, net_status, net_note = [], [], "not_run", ""
    while True:
        pass_no += 1
        # Reload the master pack so rules promoted on the previous pass are live.
        with open(cfg["pack_path"], encoding="utf-8") as f:
            pack = json.load(f)
        pack["__path__"] = cfg["pack_path"]
        print(f"\n########## PASS {pass_no}/{max_passes} ##########")

        res = run_pass(pass_no)
        rtl_blockers, net_blockers = res["rtl"], res["net"]
        net_status, net_note = res["net_status"], res["net_note"]
        clean = (not rtl_blockers) and (
            (not netlist) or (net_status == "ok" and not net_blockers))

        # Learn + promote this pass's surprises into the master pack.
        pass_learned = 0
        if learn and res["surprises"]:
            from agent.gate_learn import learn_from_surprises
            today = datetime.now(timezone.utc).date().isoformat()
            lr = learn_from_surprises(
                list(res["surprises"].values()),
                session_dir=os.path.join(out_root, ".learn"),
                pack_path=cfg["pack_path"], design=design, today=today)
            pass_learned = lr.get("learned", 0)
            total_learned += pass_learned
            s = (lr.get("promote") or {}).get("summary") or {}
            promo["added"] += s.get("added", 0)
            promo["strengthened"] += s.get("strengthened", 0)
            print(f"\n=== LEARNED pass {pass_no} (netlist surprises -> master pack) ===")
            for a in (lr.get("promote") or {}).get("plan", []):
                print(f"  [{a['decision']:5}] {a['op']:9} {a['rule_id']}  {a['detail']}")

        if clean:
            print(f"\n[ready] design CLEAN after pass {pass_no} -- done.")
            break
        if pass_no >= max_passes:
            print(f"\n[ready] reached --max-passes ({max_passes}) -- stopping.")
            break
        if res["fixes_applied"] == 0 and pass_learned == 0:
            print("\n[ready] no fix applied and nothing new learned -- fixpoint "
                  "reached, can't get cleaner. Stopping.")
            break
        print("\n[ready] re-running the loop with the updated rulebook...")

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
    print(f"[ready] FINAL VERDICT: {final}   (passes run: {pass_no})")
    print(f"        cleaned RTL : {out_root}")
    print(f"        patches     : "
          + (f"{patch_dir}  ({len(diffs)} file(s) changed)" if diffs
             else "none (RTL unchanged)"))
    if learn:
        print(f"        learned     : {total_learned} netlist surprise(s) -> "
              f"pack +{promo['added']} new, {promo['strengthened']} strengthened")
    if netlist and net_status in ("unavailable", "failed"):
        print(f"        netlist     : {net_status} -- {net_note}")
    if final_blockers:
        print(f"        UNFIXED ({len(final_blockers)}):")
        for b in final_blockers:
            print(f"          - [{b.rule_id}] {b.measurement_key}={b.measured} "
                  f"-> {', '.join(b.downstream_stages) or 'rtl'}")
    print("=" * 70)

    # Record readiness gate results in per-design memory
    for b in final_blockers:
        if b.measurement_key:
            mem.record_finding(b.measurement_key, b.measured,
                               session_id=session_id)
    # Collect learned rule ids
    learned_ids = []
    learn_dir = os.path.join(out_root, ".learn", "kb_edits.jsonl")
    if os.path.exists(learn_dir):
        with open(learn_dir, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                e = json.loads(line)
                if e.get("kind") == "add" and e.get("new_rule", {}).get("id"):
                    learned_ids.append(e["new_rule"]["id"])
    surprises_list = []
    for s_vals in [res.get("surprises", {})]:
        if isinstance(s_vals, dict):
            surprises_list.extend(s_vals.values())
    mem.record_run_end(
        session_id,
        stats={"passes": pass_no, "fixes_applied": sum(
            1 for d in (diffs or {})
        )},
        readiness_verdict=final,
        blockers_remaining=len(final_blockers),
        rules_learned=learned_ids,
        surprises=surprises_list,
    )
    mem.set_source_hash(src_hash)

    # ---- HUMAN-IN-THE-LOOP: apply changes to original source? ----
    if diffs:
        do_apply = apply_to_source
        if not do_apply:
            print()
            print("-" * 70)
            print(f"  {len(diffs)} file(s) changed in the working copy:")
            for rel in sorted(diffs):
                print(f"    - {rel}")
            print()
            print("  Working copy : " + out_root)
            print("  Original     : " + orig_root)
            print("-" * 70)
            try:
                answer = input(
                    "\n  Apply these changes to the ORIGINAL source files? [y/N] "
                ).strip().lower()
                do_apply = answer in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                print("\n  (no input) -- changes NOT applied.")
                do_apply = False

        if do_apply:
            applied_count = 0
            for rel in diffs:
                src = os.path.join(out_root, rel)
                dst = os.path.join(orig_root, rel)
                if os.path.exists(src):
                    shutil.copy2(src, dst)
                    applied_count += 1
            print(f"\n  APPLIED {applied_count} file(s) to {orig_root}")
            mem.data["current_state"]["changes_applied"] = True
            mem.save()
        else:
            print(f"\n  Changes NOT applied. Cleaned RTL is at: {out_root}")
            print(f"  To apply later:  cp {out_root}/*.sv {orig_root}/")


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


def cmd_memory(args: list[str], *, show_history: bool = False) -> None:
    """Query per-design memory: what the agent has done before and current state.

    Usage:
      run_real.py memory                    # list all known designs
      run_real.py memory buggy_demo         # summary for one design
      run_real.py history buggy_demo        # full run history for one design
    """
    if not args:
        designs = DesignMemory.list_designs()
        if not designs:
            print("[memory] no designs in memory yet. Run a scenario first.")
            return
        print(f"[memory] {len(designs)} design(s) in memory:\n")
        for did in designs:
            mem = DesignMemory.load(did)
            d = mem.data
            st = d["current_state"]
            runs = d.get("total_runs", 0)
            phase = st.get("phase", "idle")
            rv = st.get("readiness_verdict")
            rv_str = f"  verdict={rv}" if rv else ""
            print(f"  {did:<25} runs={runs}  phase={phase}{rv_str}")
        return

    design_id = args[0]
    mem = DesignMemory.load(design_id)
    if not mem.has_been_processed():
        print(f"[memory] no memory for design '{design_id}'.")
        return

    if show_history:
        print(mem.format_history())
    else:
        print(mem.format_summary())


def cmd_rules(args: list[str]) -> None:
    """Rule management: list, inspect, validate, check RTL, show health.

    Usage:
      run_real.py rules                             # list rules in default pack
      run_real.py rules packs/rtl/rules.json        # list rules in a specific pack
      run_real.py rules show <rule_id>               # detail view of one rule
      run_real.py rules check <scenario_dir>         # check RTL against rules (no fix)
      run_real.py rules validate                     # validate rule format
      run_real.py rules health                       # show rule health across runs
    """
    from agent.rule_health import (
        RuleHealthStore, format_rule_detail, format_pack_summary,
        validate_rule, diagnose_remaining, record_clean_rules,
        apply_corrections, persist_pack, format_diagnoses,
        format_corrections,
    )

    default_pack = "packs/rtl/rules.json"

    def _load_pack(path=None):
        p = path or default_pack
        if not os.path.exists(p):
            raise SystemExit(f"rules: pack not found: {p}")
        with open(p) as f:
            pack = json.load(f)
        pack["__path__"] = p
        return pack, p

    # Parse subcommand
    subcmd = args[0] if args else "list"

    # ---- rules list [pack_path] ----
    if subcmd == "list" or (subcmd and os.path.exists(subcmd)
                            and subcmd.endswith(".json")):
        pack_path = subcmd if subcmd != "list" else (args[1] if len(args) > 1 else None)
        pack, pp = _load_pack(pack_path)
        health = RuleHealthStore()
        print(format_pack_summary(pack, pack_path=pp, health=health))
        return

    # ---- rules show <rule_id> [pack_path] ----
    if subcmd == "show":
        if len(args) < 2:
            raise SystemExit("rules show: need a rule_id")
        rule_id = args[1]
        pack_path = args[2] if len(args) > 2 else None
        pack, _ = _load_pack(pack_path)
        health = RuleHealthStore()
        rule = next((r for r in pack["rules"] if r["id"] == rule_id), None)
        if not rule:
            # Fuzzy match
            matches = [r for r in pack["rules"] if rule_id in r["id"]]
            if matches:
                print(f"No exact match for '{rule_id}'. Did you mean:")
                for m in matches:
                    print(f"  {m['id']}")
                return
            raise SystemExit(f"rules show: rule '{rule_id}' not found in pack")
        print(format_rule_detail(rule, health=health))
        return

    # ---- rules validate [pack_path] ----
    if subcmd == "validate":
        pack_path = args[1] if len(args) > 1 else None
        pack, pp = _load_pack(pack_path)
        key_index = pack.get("key_index", {})
        total_problems = 0
        for r in pack["rules"]:
            if r.get("status") == "retired":
                continue
            problems = validate_rule(r, key_index)
            if problems:
                print(f"  {r['id']}:")
                for p in problems:
                    print(f"    - {p}")
                total_problems += len(problems)
        if total_problems == 0:
            print(f"[validate] all {len(pack['rules'])} rules OK")
        else:
            print(f"\n[validate] {total_problems} problem(s) found")
        return

    # ---- rules check <scenario_dir> [pack_path] ----
    if subcmd == "check":
        if len(args) < 2:
            raise SystemExit("rules check: need a scenario_dir (with RTL)")
        scen_dir = args[1]
        pack_path = args[2] if len(args) > 2 else None

        cfg = read_config(os.path.join(scen_dir, "config.yaml"))
        stage = cfg.get("stage", "rtl")
        rtl_root = (cfg.get("oracle") or {}).get("rtl_root") or cfg.get("rtl_root")
        if not rtl_root or not os.path.isdir(rtl_root):
            raise SystemExit("rules check: need oracle.rtl_root in config")
        top = (cfg.get("perceiver") or {}).get("top")
        design = cfg.get("name") or os.path.basename(os.path.normpath(scen_dir))

        if pack_path:
            with open(pack_path) as f:
                pack = json.load(f)
            pack["__path__"] = pack_path
        else:
            with open(cfg["pack_path"]) as f:
                pack = json.load(f)
            pack["__path__"] = cfg["pack_path"]

        from agent.sweep import sweep
        from agent.perceiver import Perceiver
        from agent.func_check import (run_functional_checks,
                                      inject_measurements, format_results
                                      as format_func_results)

        world = Perceiver(make_adapter(cfg)).perceive([])
        oracle = make_oracle(cfg)
        reality = oracle.from_existing()

        # Cross-validate with VCS if available (runs both tools)
        import shutil
        if stage == "rtl" and shutil.which("vcs"):
            try:
                from adapters.rtl.vcs.oracle import VCSRTLOracle
                vcs_oracle = VCSRTLOracle(
                    top=top, rtl_root=rtl_root,
                    rtl_files=cfg.get("rtl_files"))
                vcs_reality = vcs_oracle.from_live_lint()
                vcs_counts = {k: v for k, v in vcs_reality.measurements.items()
                              if isinstance(v, (int, float))
                              and k != "vcs.lint.raw_classes"}
                # Merge VCS findings: take the MAX of each shared key
                # (if either tool flags it, it's a real issue)
                for key, val in vcs_counts.items():
                    old = reality.measurements.get(key)
                    if old is None:
                        reality.measurements[key] = val
                    elif isinstance(old, (int, float)):
                        reality.measurements[key] = max(old, val)
                print(f"[vcs] cross-validated with VCS lint ({len(vcs_counts)} keys)")
            except Exception as e:
                print(f"[vcs] skipped: {e}")

        # Run functional checks (pattern/SVA/protocol) and inject results
        # into reality so the sweep can grade them alongside lint counts.
        func_results = run_functional_checks(pack, rtl_root, top=top)
        if func_results:
            inject_measurements(func_results, reality)

        report = sweep(pack, world, reality, stage_filter=stage)

        health = RuleHealthStore()
        record_clean_rules(health, report, design=design)

        # Display results
        if func_results:
            print(format_func_results(func_results))
            print()
        print(f"=== RULE CHECK: {design} ({scen_dir}) ===")
        print(f"  Rules total       : {report.n_rules_total}")
        print(f"  Rules applicable  : {report.n_rules_applicable}")
        print(f"  Clean             : {report.n_clean}")
        print(f"  Violations        : {report.n_violations}")
        print(f"  Skipped           : {report.n_skipped}")
        print(f"  N/A               : {report.n_na}")
        print()

        if report.n_violations > 0:
            print("VIOLATIONS:")
            for rc in report.violations():
                print(f"  [{rc.strength:>6}] {rc.rule_id}")
                print(f"          {rc.statement[:80]}")
                for ch in rc.checks:
                    if ch.status == "violation":
                        print(f"          {ch.measurement_key}: {ch.reason}")
                print()

            # Research each violation via LLM to determine: design bug
            # or wrong rule?  Only suggests corrections when the LLM
            # concludes (with reasoning) that the rule itself is wrong.
            from agent.gate import classify as gate_classify
            blockers = gate_classify(report.violations())

            run_dir = os.path.join(scen_dir, "runs", "rule_diagnosis")
            os.makedirs(run_dir, exist_ok=True)

            print("\n[researching violations -- analyzing each rule against RTL best practices...]")
            diagnoses = diagnose_remaining(
                health, pack, blockers, design=design, run_dir=run_dir)

            if diagnoses:
                print(format_diagnoses(diagnoses))
                print()
                # Offer to apply corrections
                try:
                    answer = input("  Apply rule corrections? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = "n"
                if answer in ("y", "yes"):
                    corrections = apply_corrections(pack, diagnoses, health)
                    if corrections:
                        print(format_corrections(corrections))
                        persist_pack(pack, pack["__path__"])
                        print(f"  Pack updated: {pack['__path__']}")
                    else:
                        print("  No corrections applied.")
            else:
                print("\n[research complete] All violations are design bugs -- rules are correct.")

        clean_rules = [rc for rc in report.rules if rc.status == "clean"]
        if clean_rules:
            print(f"CLEAN ({len(clean_rules)} rules passed):")
            for rc in clean_rules[:10]:
                keys = ", ".join(ch.measurement_key for ch in rc.checks if ch.measurement_key)
                print(f"  [  OK  ] {rc.rule_id}  ({keys})")
            if len(clean_rules) > 10:
                print(f"  ... and {len(clean_rules) - 10} more")
        return

    # ---- rules health [pack_path] ----
    if subcmd == "health":
        health = RuleHealthStore()
        if not health.data.get("rules"):
            print("[health] no rule health data yet. Run 'rules check' on some designs first.")
            return
        pack_path = args[1] if len(args) > 1 else None
        pack, pp = _load_pack(pack_path)

        print(f"=== RULE HEALTH ({pp}) ===")
        print(f"  Rules tracked: {len(health.data['rules'])}")
        print(f"  Corrections applied: {len(health.data.get('corrections', []))}")
        print()

        # Sort by accuracy (worst first)
        scored = []
        for rule_id, r in health.data["rules"].items():
            acc = health.accuracy(rule_id)
            scored.append((rule_id, r, acc))
        scored.sort(key=lambda x: (x[2] if x[2] is not None else 1.0))

        print(f"  {'RULE ID':<45} {'EVALS':>5} {'RIGHT':>5} {'WRONG':>5} {'ACCURACY':>8}")
        print("  " + "-" * 75)
        for rule_id, r, acc in scored:
            acc_str = f"{acc:.0%}" if acc is not None else "n/a"
            print(f"  {rule_id:<45} {r['evaluations']:>5} "
                  f"{r['correct']:>5} {r['wrong']:>5} {acc_str:>8}")

        # Show recent corrections
        corrections = health.data.get("corrections", [])
        if corrections:
            print(f"\nRecent corrections:")
            for c in corrections[-10:]:
                print(f"  [{c.get('recommendation', '?'):>10}] {c['rule_id']} "
                      f"— {c.get('detail', '')[:60]}")
        return

    if subcmd == "enhance":
        from agent.rule_enhance import (find_gaps, format_gaps,
                                        enhance_rules_sync,
                                        format_enhancements)
        pack_path_e = args[1] if len(args) > 1 else None
        if not pack_path_e:
            for candidate in ["packs/rtl/rules.json",
                               "packs/default/rules.json"]:
                if os.path.exists(candidate):
                    pack_path_e = candidate
                    break
        if not pack_path_e:
            print("No pack found. Specify a pack path.")
            return
        with open(pack_path_e) as f:
            pack = json.load(f)

        gaps = find_gaps(pack)
        print(format_gaps(gaps))

        if not gaps:
            return

        max_rules = 10
        if len(args) > 2:
            try:
                max_rules = int(args[2])
            except ValueError:
                pass

        run_dir = os.path.join("runs", "enhance_latest")
        enhancements = enhance_rules_sync(
            pack, run_dir, max_rules=max_rules)

        print(format_enhancements(enhancements))

        applied = [e for e in enhancements
                   if any([e.added_violating, e.added_compliant,
                           e.improved_statement, e.added_rationale,
                           e.added_predicts, e.added_prevents])]
        if applied:
            persist_pack(pack, pack_path_e)
            print(f"\nPack saved to {pack_path_e} "
                  f"({len(applied)} rule(s) updated)")
        return

    # ---- rules auto [pack_path] [--no-generate] [--max-rounds N] ----
    if subcmd == "auto":
        from agent.rule_enhance import (
            auto_loop_sync, format_auto_report, find_gaps,
            cross_validate,
        )
        pack_path_a = None
        max_rounds = 5
        do_generate = True
        for a in args[1:]:
            if a == "no-generate":
                do_generate = False
            elif a.endswith(".json"):
                pack_path_a = a
            else:
                try:
                    max_rounds = int(a)
                except ValueError:
                    pass
        if not pack_path_a:
            for candidate in ["packs/rtl/rules.json",
                               "packs/default/rules.json"]:
                if os.path.exists(candidate):
                    pack_path_a = candidate
                    break
        if not pack_path_a:
            print("No pack found. Specify a pack path.")
            return

        with open(pack_path_a) as f:
            pack = json.load(f)

        initial_gaps = len(find_gaps(pack))
        initial_rules = len([r for r in pack.get("rules", [])
                             if r.get("status") != "retired"])
        initial_issues = len(cross_validate(pack))

        print(f"=== AUTONOMOUS RULES LOOP ===")
        print(f"  Pack          : {pack_path_a}")
        print(f"  Rules (active): {initial_rules}")
        print(f"  Gaps          : {initial_gaps}")
        print(f"  Issues        : {initial_issues}")
        print(f"  Max rounds    : {max_rounds}")
        print(f"  Generate new  : {'yes' if do_generate else 'no'}")

        run_dir = os.path.join("runs", "auto_latest")
        report = auto_loop_sync(
            pack, run_dir,
            max_rounds=max_rounds,
            do_generate=do_generate,
        )

        print(format_auto_report(report))

        # Summarize changes before asking for permission
        final_rules = len([r for r in pack.get("rules", [])
                           if r.get("status") != "retired"])
        changes = (report.total_enhanced + report.total_generated
                   + report.total_retired)
        if changes == 0:
            print("\nNo changes to apply.")
            return

        print(f"\nSummary of changes:")
        print(f"  Rules: {initial_rules} -> {final_rules} "
              f"(+{report.total_generated}, -{report.total_retired})")
        print(f"  Enhanced: {report.total_enhanced}")
        print(f"  Gaps: {initial_gaps} -> {report.final_gaps}")

        try:
            answer = input("\n  Save all changes to pack? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"

        if answer in ("y", "yes"):
            persist_pack(pack, pack_path_a)
            print(f"  Pack saved: {pack_path_a}")
        else:
            print("  Changes discarded.")
        return

    print(f"Unknown rules subcommand: {subcmd}")
    print("Usage: rules [list|show|check|validate|enhance|auto|health] ...")


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
                                      "promote", "ready", "memory", "history",
                                      "rules"])
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
    ap.add_argument("--max-rounds", type=int, default=0,
                    help="ready: max auto-fix rounds per pass (0 = unlimited).")
    ap.add_argument("--max-passes", type=int, default=3,
                    help="ready: max outer passes -- after each pass it learns + "
                         "promotes, then re-runs the loop until clean or fixpoint "
                         "(default 3).")
    ap.add_argument("--no-fix", action="store_true",
                    help="ready: only emit the GO/NO-GO readiness verdict; do "
                         "not run the auto-fix loop.")
    ap.add_argument("--no-netlist", action="store_true",
                    help="ready: skip the real Yosys synthesis / netlist gate; "
                         "stop at RTL readiness.")
    ap.add_argument("--no-learn", action="store_true",
                    help="ready: do not mint/promote rules from netlist "
                         "surprises (RTL clean but synthesis found a hazard).")
    ap.add_argument("--no-optimize", action="store_true",
                    help="ready: skip the RTL optimization step before "
                         "synthesis. By default, once RTL is lint-clean the "
                         "agent proposes synthesis-friendly improvements.")
    ap.add_argument("--apply-to-source", action="store_true",
                    help="ready: automatically apply changes to the original "
                         "source files without asking. By default the agent "
                         "asks the user interactively.")
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
        # Only the predict/reflect role always runs. The verifier panel runs
        # ONLY for the explicit verify commands or run-all-api --verify; `ready`
        # never uses it. Don't advertise verifier 1/2 when they won't fire.
        verifiers_active = (
            ns.cmd in ("verify", "verify-api")
            or (ns.cmd == "run-all-api" and getattr(ns, "verify", False))
        )
        region = (os.environ.get("AWS_REGION")
                  or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
        print("=" * 70)
        print(f"  COGNI_BEDROCK active  --  AWS Bedrock (region={region}):")
        print(f"     predictor/reflector : {_BEDROCK_PREDICT_MODEL}")
        if verifiers_active:
            print(f"     verifier 1          : {_BEDROCK_VERIFY1_MODEL}")
            print(f"     verifier 2          : {_BEDROCK_VERIFY2_MODEL}")
        else:
            print("     verifiers           : OFF (no verifier 1/2 calls this run)")
        print("  Data stays in your AWS account. Override the predictor via "
              "COGNI_BEDROCK_PREDICT_MODEL.")
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
        cmd_ready(ns.args, max_rounds=ns.max_rounds, max_passes=ns.max_passes,
                  fix=not ns.no_fix, netlist=not ns.no_netlist,
                  learn=not ns.no_learn, optimize=not ns.no_optimize,
                  apply_to_source=ns.apply_to_source,
                  concurrency=ns.concurrency)
    elif ns.cmd == "memory":
        cmd_memory(ns.args)
    elif ns.cmd == "history":
        cmd_memory(ns.args, show_history=True)
    elif ns.cmd == "rules":
        cmd_rules(ns.args)

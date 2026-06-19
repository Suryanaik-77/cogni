"""
cogni.agent.orchestrator
========================
The agent itself. Runs the cognitive loop:

    perceive -> attend -> recall -> predict (or refuse) -> verify panel
    -> revise once if dissent -> commit -> observe reality -> reflect
    -> apply KB edits

Subagent dispatch is abstracted via a `Dispatcher` — in this sandbox it's
backed by run_subagent; on a local machine it would be backed by direct
SDK calls. The orchestrator does not care which.
"""
from __future__ import annotations
import json
import os
from dataclasses import asdict
from typing import Callable, Protocol
from .core import (
    WorldModel, Prediction, Refusal, VerifierVerdict, Reality, Verdict,
    VerdictKind, Surprise, KBEdit, KBEditKind, Confidence, RuleStrength,
    Rule, RuleStatus, RulePerformance, new_id, jsonl_append, _now,
)
from .kb import KnowledgeBase
from .llm import LLMCall
from . import organs


class Dispatcher(Protocol):
    """How LLM calls are actually executed."""
    def dispatch(self, call: LLMCall, run_dir: str) -> dict:
        """Materialize call, run it, return parsed output."""
        ...

    def dispatch_parallel(self, calls: list[LLMCall], run_dir: str) -> list[dict]:
        ...


class Orchestrator:
    def __init__(self, kb: KnowledgeBase, dispatcher: Dispatcher, run_dir: str,
                 confidence_floor: Confidence = Confidence.UNCERTAIN):
        self.kb = kb
        self.dispatcher = dispatcher
        self.run_dir = run_dir
        self.confidence_floor = confidence_floor
        os.makedirs(run_dir, exist_ok=True)

    # -----------------------------------------------------------------
    # The cognitive cycle for one (question, stage) pair
    # -----------------------------------------------------------------
    def cycle(self, world: WorldModel, question: str, stage: str | None,
              measurement_key_hint: str | None = None) -> dict:
        """Run perceive -> ... -> commit. Returns a record of what happened.
        Reality + reflection happen in a separate method."""
        cycle_id = new_id("cyc")
        record = {"cycle_id": cycle_id, "question": question, "stage": stage}

        # ---- Recall (mechanical, no LLM): get all candidate rules ----
        candidates = self.kb.recall(world, stage=stage)
        record["n_candidate_rules"] = len(candidates)
        if not candidates:
            # No rules apply at all — refuse before even calling the LLM
            refusal = Refusal(
                id=new_id("ref"), question=question,
                reason="No rules in the knowledge base apply to this world model + stage.",
                insufficient_rules=[], missing_evidence=["KB has no applicable rule"],
            )
            self._log_refusal(refusal)
            record["outcome"] = "refused_no_candidates"
            record["refusal_id"] = refusal.id
            return record

        # ---- Attention: focus the candidate set ----
        att_call = organs.attention_call(world, candidates, question, stage)
        att_out = self.dispatcher.dispatch(att_call, self.run_dir)
        focused_rule_ids = att_out.get("focused_rule_ids", [])
        focused_rules = [r for r in candidates if r.id in focused_rule_ids]
        focused_facts = {k: world.facts[k].value for k in att_out.get("focused_fact_keys", []) if k in world.facts}
        record["attention"] = {
            "focused_rule_ids": focused_rule_ids,
            "ignored_rule_ids": att_out.get("ignored_rule_ids", []),
            "rationale": att_out.get("rationale", ""),
        }

        # If attention focused away to nothing, refuse
        if not focused_rules:
            refusal = Refusal(
                id=new_id("ref"), question=question,
                reason="Attention determined no candidate rule directly addresses this question.",
                insufficient_rules=[r.id for r in candidates],
                missing_evidence=["Need rules specific to this question; none of the recalled rules fit."],
            )
            self._log_refusal(refusal)
            record["outcome"] = "refused_no_focus"
            record["refusal_id"] = refusal.id
            return record

        # ---- Predict (or refuse) ----
        pred_call = organs.predictor_call(world, focused_rules, focused_facts, question, stage,
                                          measurement_key_hint=measurement_key_hint)
        pred_out = self.dispatcher.dispatch(pred_call, self.run_dir)

        if pred_out.get("decision") == "refuse":
            refusal = Refusal(
                id=new_id("ref"), question=question,
                reason=pred_out.get("refusal_reason", "primary refused"),
                insufficient_rules=pred_out.get("rules_considered_but_rejected", []),
                missing_evidence=pred_out.get("missing_evidence", []),
            )
            self._log_refusal(refusal)
            record["outcome"] = "refused_by_primary"
            record["refusal_id"] = refusal.id
            return record

        # Build a Prediction object (not yet committed to ledger)
        confidence = Confidence(pred_out.get("confidence", "uncertain"))

        # Confidence-gating: if model said "uncertain" and we want stricter, refuse
        # (This is a knob; default floor is UNCERTAIN which lets uncertain through.)
        if confidence.p < self.confidence_floor.p and confidence != self.confidence_floor:
            refusal = Refusal(
                id=new_id("ref"), question=question,
                reason=f"Confidence {confidence.value} is below floor {self.confidence_floor.value}.",
                insufficient_rules=[r.id for r in focused_rules],
                missing_evidence=pred_out.get("missing_evidence", []),
            )
            self._log_refusal(refusal)
            record["outcome"] = "refused_low_confidence"
            record["refusal_id"] = refusal.id
            return record

        prediction = Prediction(
            id=new_id("pred"), question=question,
            claim=pred_out.get("claim", ""),
            rationale=pred_out.get("rationale", ""),
            confidence=confidence,
            falsifier=pred_out.get("falsifier", ""),
            cited_rule_ids=pred_out.get("cited_rule_ids", []),
            quantitative=pred_out.get("quantitative"),
            stage=stage,
            primary_model=pred_call.model,
        )

        # ---- Verifier panel ----
        ver_calls = organs.verifier_calls(asdict(prediction), focused_rules, focused_facts, question)
        ver_outs = self.dispatcher.dispatch_parallel(ver_calls, self.run_dir)

        verifier_verdicts = []
        any_dissent = False
        for c, o in zip(ver_calls, ver_outs):
            v = VerifierVerdict(
                verifier_model=c.model,
                agrees=bool(o.get("agrees", False)),
                concerns=o.get("concerns", []),
                suggested_revisions=o.get("suggested_revisions", []),
                raw=json.dumps(o),
            )
            verifier_verdicts.append(v)
            if not v.agrees:
                any_dissent = True

        record["verifier_verdicts"] = [asdict(v) for v in verifier_verdicts]

        # ---- Revision round if any dissent ----
        if any_dissent:
            rev_call = organs.revision_call(
                world, focused_rules, focused_facts, question, stage,
                prior_prediction=asdict(prediction),
                verifier_feedback=[asdict(v) for v in verifier_verdicts],
            )
            rev_out = self.dispatcher.dispatch(rev_call, self.run_dir)
            if rev_out.get("decision") == "refuse":
                refusal = Refusal(
                    id=new_id("ref"), question=question,
                    reason="Revised to refusal after verifier dissent: " + rev_out.get("refusal_reason", ""),
                    insufficient_rules=rev_out.get("rules_considered_but_rejected", []),
                    missing_evidence=rev_out.get("missing_evidence", []),
                )
                self._log_refusal(refusal)
                record["outcome"] = "refused_after_revision"
                record["refusal_id"] = refusal.id
                return record

            # Replace prediction with revised one (keep id continuity? new id, link via revisions counter)
            prediction = Prediction(
                id=new_id("pred"), question=question,
                claim=rev_out.get("claim", ""),
                rationale=rev_out.get("rationale", ""),
                confidence=Confidence(rev_out.get("confidence", "uncertain")),
                falsifier=rev_out.get("falsifier", ""),
                cited_rule_ids=rev_out.get("cited_rule_ids", []),
                quantitative=rev_out.get("quantitative"),
                stage=stage,
                primary_model=rev_call.model,
                revisions=1,
            )

        # ---- Commit to immutable ledger ----
        for rid in prediction.cited_rule_ids:
            self.kb.record_citation(rid, qid=prediction.id, session_dir=self.run_dir)
        self._log_prediction(prediction)
        record["outcome"] = "committed"
        record["prediction_id"] = prediction.id
        return record

    # -----------------------------------------------------------------
    # Reflection: after reality lands, update KB
    # -----------------------------------------------------------------
    def reflect(self, prediction: Prediction, reality: Reality,
                verdict: Verdict) -> list[KBEdit]:
        cited = [self.kb.by_id(rid) for rid in prediction.cited_rule_ids if self.kb.by_id(rid)]
        call = organs.reflector_call(asdict(prediction), asdict(reality), asdict(verdict), cited)
        out = self.dispatcher.dispatch(call, self.run_dir)

        # Record per-rule outcomes
        for rid, status in (out.get("rule_attribution") or {}).items():
            mapped = {"supported": "right", "failed": "wrong", "neutral": "unfalsifiable"}.get(status, "unfalsifiable")
            self.kb.record_outcome(
                rid, mapped, prediction_id=prediction.id,
                qid=prediction.id, session_dir=self.run_dir,
                evidence_key=(verdict.evidence_key if hasattr(verdict, "evidence_key") else ""),
                evidence_value=(verdict.evidence_value if hasattr(verdict, "evidence_value") else None),
            )

        # Surprise log
        if verdict.kind in (VerdictKind.WRONG_AND_WRONG_REASON, VerdictKind.WRONG_BUT_RIGHT_DIRECTION,
                            VerdictKind.RIGHT_BUT_WRONG_REASON):
            sup = out.get("surprise") or {}
            surprise = Surprise(
                id=new_id("sup"), prediction_id=prediction.id, verdict_id=verdict.id,
                what_we_expected=sup.get("what_we_expected", ""),
                what_actually_happened=sup.get("what_actually_happened", ""),
                why_we_missed_it=sup.get("why_we_missed_it", ""),
                suggested_kb_action="; ".join(e.get("rationale", "") for e in out.get("kb_edits", [])),
            )
            self._log_surprise(surprise)

        # Build KBEdit objects
        edits = []
        for e in out.get("kb_edits", []):
            kind = KBEditKind(e["kind"])
            new_rule = None
            if e.get("new_rule"):
                nr = e["new_rule"]
                # require minimum fields
                new_rule = Rule(
                    id=nr.get("id") or new_id("r"),
                    statement=nr.get("statement", ""),
                    when=nr.get("when", []),
                    unless=nr.get("unless", []),
                    stage=nr.get("stage"),
                    strength=RuleStrength(nr.get("strength", "tendency")),
                    citations=nr.get("citations", []),
                    rationale=nr.get("rationale", ""),
                )
            new_strength = RuleStrength(e["new_strength"]) if e.get("new_strength") else None
            edit = KBEdit(
                id=new_id("kbe"), kind=kind,
                target_rule_id=e.get("target_rule_id"),
                new_rule=new_rule,
                new_strength=new_strength,
                added_unless=e.get("added_unless", []),
                rationale=e.get("rationale", ""),
            )
            self.kb.apply(edit)
            self._log_kb_edit(edit)
            edits.append(edit)

        # Persist updated KB
        if self.kb.pack_path:
            self.kb.save()

        return edits

    # -----------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------
    def _log_prediction(self, p: Prediction):
        d = asdict(p); d["confidence"] = p.confidence.value
        jsonl_append(os.path.join(self.run_dir, "ledger.jsonl"), d)

    def _log_refusal(self, r: Refusal):
        d = asdict(r); d["confidence"] = r.confidence.value
        jsonl_append(os.path.join(self.run_dir, "refusals.jsonl"), d)

    def _log_surprise(self, s: Surprise):
        jsonl_append(os.path.join(self.run_dir, "surprises.jsonl"), asdict(s))

    def _log_kb_edit(self, e: KBEdit):
        d = {"id": e.id, "kind": e.kind.value, "target_rule_id": e.target_rule_id,
             "new_rule": e.new_rule.to_dict() if e.new_rule else None,
             "new_strength": e.new_strength.value if e.new_strength else None,
             "added_unless": e.added_unless, "rationale": e.rationale,
             "applied": e.applied, "created_at": e.created_at}
        jsonl_append(os.path.join(self.run_dir, "kb_edits.jsonl"), d)

    def commit_verdict(self, v: Verdict):
        d = asdict(v); d["kind"] = v.kind.value
        jsonl_append(os.path.join(self.run_dir, "verdicts.jsonl"), d)

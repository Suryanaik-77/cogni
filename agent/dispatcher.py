"""
cogni.agent.dispatcher
======================
Concrete dispatchers that turn an LLMCall into an actual answer.

Two implementations live here:

  1. SubagentDispatcher  -- production: writes the brief, signals the
     parent agent (this conversation) to spawn a subagent, blocks on
     the output file. Used when we want real model cognition.

  2. MockDispatcher      -- offline: synthesizes a deterministic, schema-
     valid response from the call's name + inputs. Used for plumbing
     tests, smoke runs, and CI. The mock is *intentionally dumb* — it
     does not pretend to reason. It exists so we can validate the
     framework without burning credits.

A third helper, RecordingDispatcher, wraps any dispatcher and writes
every (call, output) pair to a JSONL trace for later replay.

Why two flavors?
The cognitive-agent framework's value is in the *structure* of cognition
(perceive→attend→predict→verify→reflect, with refusal and rule-lifecycle).
The structure is testable without an LLM. Real LLM dispatch is a
swap-in upgrade, not a rewrite.
"""
from __future__ import annotations
import json
import os
import time
from dataclasses import asdict
from typing import Any
from .llm import LLMCall, validate_schema


# ============================================================================
# SubagentDispatcher
# ============================================================================
# The "real" dispatcher. The orchestrator runs inside a Python process; it
# cannot call the platform's run_subagent tool directly. So we use a
# file-based handshake:
#
#   1. dispatch() writes the brief to <run_dir>/llm_calls/<name>/.
#   2. dispatch() writes a "pending" marker file the parent agent watches.
#   3. dispatch() spins on output.json existing.
#   4. The parent agent (this conversation) periodically scans the run_dir,
#      finds pending calls, fires run_subagent for each, drops output.json,
#      and removes the pending marker.
#   5. dispatch() reads output.json, validates, returns parsed dict.
#
# This keeps the orchestrator *synchronous* and trace-friendly while
# offloading LLM work to subagents. The cost is a tight polling loop, but
# at human-scale rates (a few calls per minute) it's harmless.
# ============================================================================

class SubagentDispatcher:
    """File-based handshake with the parent agent.

    The parent agent is responsible for actually invoking run_subagent.
    This class just writes briefs and waits for outputs.
    """

    def __init__(self, run_dir: str, poll_interval: float = 2.0,
                 timeout: float = 1800.0):
        self.run_dir = run_dir
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.pending_dir = os.path.join(run_dir, "_pending")
        self.done_dir = os.path.join(run_dir, "_done")
        os.makedirs(self.pending_dir, exist_ok=True)
        os.makedirs(self.done_dir, exist_ok=True)

    def _signal_pending(self, call: LLMCall, paths: dict[str, str]):
        marker = os.path.join(self.pending_dir, f"{call.name}.json")
        with open(marker, "w") as f:
            json.dump({
                "name": call.name,
                "model": call.model,
                "role": call.role,
                "objective": call.subagent_objective(),
                "paths": paths,
                "queued_at": time.time(),
            }, f, indent=2)

    def _wait_for_output(self, call: LLMCall) -> dict:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if os.path.exists(call.output_path):
                with open(call.output_path) as f:
                    data = json.load(f)
                validate_schema(data, call.schema)
                # mark done
                marker = os.path.join(self.pending_dir, f"{call.name}.json")
                if os.path.exists(marker):
                    os.replace(marker, os.path.join(self.done_dir, f"{call.name}.json"))
                return data
            time.sleep(self.poll_interval)
        raise TimeoutError(f"LLM call '{call.name}' did not finish within {self.timeout}s")

    def dispatch(self, call: LLMCall, run_dir: str) -> dict:
        paths = call.write_brief(run_dir)
        self._signal_pending(call, paths)
        return self._wait_for_output(call)

    def dispatch_parallel(self, calls: list[LLMCall], run_dir: str) -> list[dict]:
        # signal all, then wait on all
        all_paths = []
        for c in calls:
            paths = c.write_brief(run_dir)
            self._signal_pending(c, paths)
            all_paths.append(paths)
        return [self._wait_for_output(c) for c in calls]


# ============================================================================
# MockDispatcher
# ============================================================================
# Produces deterministic, schema-valid outputs without any LLM. The mock
# is honest: its outputs are clearly synthetic, easy to spot in logs.
# Used to validate the cognitive loop's plumbing.
#
# Mock policy by role:
#   attention  : focus on first 5 candidate rules, ignore the rest
#   predictor  : claim "MOCK: <rule statement>" with confidence=likely
#                citing the first 3 candidate rules
#   verifier   : agrees=True, no concerns (so no revision is triggered)
#   reflector  : proposes one STRENGTHEN edit on the first cited rule
# ============================================================================

class MockDispatcher:
    """Schema-valid synthetic outputs. For plumbing only."""

    def __init__(self, run_dir: str):
        self.run_dir = run_dir

    def dispatch(self, call: LLMCall, run_dir: str) -> dict:
        call.write_brief(run_dir)
        out = self._synthesize(call)
        # write to disk so trace looks identical to real run
        with open(call.output_path, "w") as f:
            json.dump(out, f, indent=2)
        validate_schema(out, call.schema)
        return out

    def dispatch_parallel(self, calls: list[LLMCall], run_dir: str) -> list[dict]:
        return [self.dispatch(c, run_dir) for c in calls]

    def _synthesize(self, call: LLMCall) -> dict:
        role = call.role
        inputs = call.inputs

        if "attention" in role:
            cands = inputs.get("candidate_rules", [])
            focused = [r["id"] for r in cands[:5]]
            ignored = [r["id"] for r in cands[5:]]
            facts = list(inputs.get("world_facts", {}).keys())[:8]
            return {
                "focused_fact_keys": facts,
                "focused_rule_ids": focused,
                "ignored_rule_ids": ignored,
                "rationale": "MOCK attention: kept first 5 candidate rules.",
            }

        if "predictor" in role or role == "predictor":
            cands = inputs.get("focused_rules", []) or inputs.get("candidate_rules", [])
            if not cands:
                return {
                    "decision": "refuse",
                    "claim": "",
                    "rationale": "",
                    "confidence": "unknown",
                    "falsifier": "",
                    "cited_rule_ids": [],
                    "rules_considered_but_rejected": [],
                    "missing_evidence": ["MOCK: no focused rules"],
                    "refusal_reason": "MOCK: no focused rules to lean on",
                }
            stmt = cands[0].get("statement", "(no statement)")
            return {
                "decision": "predict",
                "claim": f"MOCK prediction: {stmt[:120]}",
                "rationale": "MOCK rationale: applied the first focused rule.",
                "confidence": "likely",
                "falsifier": "MOCK falsifier: oracle would show this rule did not hold.",
                "cited_rule_ids": [r["id"] for r in cands[:3]],
                "rules_considered_but_rejected": [],
                "missing_evidence": [],
                "refusal_reason": "",
            }

        if "verifier" in role:
            return {
                "agrees": True,
                "concerns": [],
                "suggested_revisions": [],
            }

        if "revis" in role:
            # revision after dissent — re-emit with one revision marker
            prior = inputs.get("prior_prediction", {})
            rev_out = {
                "decision": "predict",
                "claim": f"MOCK revised: {prior.get('claim', '')[:120]}",
                "rationale": "MOCK revised rationale.",
                "confidence": "likely",
                "falsifier": prior.get("falsifier", "MOCK falsifier"),
                "cited_rule_ids": prior.get("cited_rule_ids", []),
                "rules_considered_but_rejected": [],
                "missing_evidence": [],
                "refusal_reason": "",
            }
            q = prior.get("quantitative")
            if isinstance(q, dict):
                rev_out["quantitative"] = q
            return rev_out

        if "reflect" in role:
            cited = inputs.get("cited_rules", [])
            target = cited[0]["id"] if cited else None
            edits = []
            if target:
                edits.append({
                    "kind": "strengthen",
                    "target_rule_id": target,
                    "new_strength": "strong",
                    "rationale": "MOCK reflector: bumped strength on first cited rule.",
                    "added_unless": [],
                    "new_rule": None,
                })
            return {
                "rule_attribution": {r["id"]: "supported" for r in cited},
                "kb_edits": edits,
                "surprise": {
                    "what_we_expected": "MOCK expected",
                    "what_actually_happened": "MOCK happened",
                    "why_we_missed_it": "MOCK reason",
                },
            }

        # fallback
        return {}


# ============================================================================
# RecordingDispatcher
# ============================================================================
# Wraps another dispatcher; logs every call/output pair to a JSONL trace.
# Useful for after-run audits and for replaying without re-spending credits.
# ============================================================================

class RecordingDispatcher:
    def __init__(self, inner, trace_path: str):
        self.inner = inner
        self.trace_path = trace_path
        os.makedirs(os.path.dirname(trace_path), exist_ok=True)

    def _log(self, call: LLMCall, out: dict):
        rec = {
            "name": call.name, "model": call.model, "role": call.role,
            "output": out, "ts": time.time(),
        }
        with open(self.trace_path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def dispatch(self, call: LLMCall, run_dir: str) -> dict:
        out = self.inner.dispatch(call, run_dir)
        self._log(call, out)
        return out

    def dispatch_parallel(self, calls: list[LLMCall], run_dir: str) -> list[dict]:
        outs = self.inner.dispatch_parallel(calls, run_dir)
        for c, o in zip(calls, outs):
            self._log(c, o)
        return outs


# ============================================================================
# ReplayDispatcher
# ============================================================================
# Reads outputs from a previous trace by call name. Useful for re-running
# the orchestrator's logic against the *same* LLM outputs (e.g. after a
# bugfix in the orchestrator) without burning credits.
# ============================================================================

class ReplayDispatcher:
    def __init__(self, trace_path: str):
        self.by_name: dict[str, dict] = {}
        with open(trace_path) as f:
            for line in f:
                rec = json.loads(line)
                self.by_name[rec["name"]] = rec["output"]

    def dispatch(self, call: LLMCall, run_dir: str) -> dict:
        call.write_brief(run_dir)
        out = self.by_name.get(call.name)
        if out is None:
            raise KeyError(f"No replay output for call '{call.name}'")
        with open(call.output_path, "w") as f:
            json.dump(out, f, indent=2)
        validate_schema(out, call.schema)
        return out

    def dispatch_parallel(self, calls: list[LLMCall], run_dir: str) -> list[dict]:
        return [self.dispatch(c, run_dir) for c in calls]

"""
cogni · core types
==================
Domain-agnostic dataclasses that hold the state of cognition.

Design principle: the agent imports from this file. Nothing in this file
imports from any pack, adapter, or oracle. Cognition does not know whether
it is reasoning about chips, netlists, or code.
"""
from __future__ import annotations
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uid(prefix: str = "id") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# WorldModel — the agent's typed perception of the situation
# ---------------------------------------------------------------------------

@dataclass
class Fact:
    """One piece of perceived information with provenance."""
    key: str                       # e.g. "core.has_multiplier"
    value: Any
    source: str                    # e.g. "rtl/ibex_pkg.sv:line 142"
    confidence: float = 1.0        # how certain the perceiver is
    tags: list[str] = field(default_factory=list)


@dataclass
class WorldModel:
    """Generic, domain-agnostic perception. Tags are the recall key."""
    domain: str                              # which pack does this belong to
    facts: dict[str, Fact] = field(default_factory=dict)
    tags: set[str] = field(default_factory=set)        # recall keys
    raw_inputs: list[str] = field(default_factory=list)  # paths or refs
    created_at: str = field(default_factory=_now)

    def add(self, key: str, value: Any, source: str, tags: list[str] = None, conf: float = 1.0):
        self.facts[key] = Fact(key=key, value=value, source=source, confidence=conf, tags=tags or [])
        for t in tags or []:
            self.tags.add(t)

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "tags": sorted(self.tags),
            "facts": {k: asdict(v) for k, v in self.facts.items()},
            "raw_inputs": self.raw_inputs,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Rule — knowledge base unit with provenance and lifecycle history
# ---------------------------------------------------------------------------

class RuleStrength(Enum):
    LAW = "law"                    # universally true within scope
    STRONG = "strong"              # >90% of cases
    TENDENCY = "tendency"          # 60-90%
    SITUATIONAL = "situational"    # depends on conditions
    HEURISTIC = "heuristic"        # rough, often-useful


class RuleStatus(Enum):
    ACTIVE = "active"
    SCOPED = "scoped"              # narrowed scope after failures
    RETIRED = "retired"            # removed from active recall
    PROPOSED = "proposed"          # awaiting human/agent confirmation


@dataclass
class RulePerformance:
    """How this rule has performed over its lifetime."""
    times_cited: int = 0
    times_right: int = 0
    times_wrong: int = 0
    times_unfalsifiable: int = 0
    last_cited_at: str | None = None
    last_failed_at: str | None = None
    failures: list[str] = field(default_factory=list)  # prediction_ids that went wrong


@dataclass
class Rule:
    """A causal/empirical rule. The atomic unit of knowledge.

    v0 (legacy) and v1 (current) fields coexist on the same dataclass
    so old packs continue to load unchanged. v1 fields default-empty;
    v0 fields are kept indefinitely for back-compat.
    """
    id: str
    statement: str                          # human-readable claim
    # ---- v0 predicates (tag-only). Kept for legacy packs. ----
    when: list = field(default_factory=list)
    unless: list = field(default_factory=list)
    stage: str | None = None                # optional pipeline stage tag (v0)
    strength: RuleStrength = RuleStrength.TENDENCY
    status: RuleStatus = RuleStatus.ACTIVE
    citations: list = field(default_factory=list)  # URLs/papers/sources
    rationale: str = ""                     # why this rule exists
    performance: RulePerformance = field(default_factory=RulePerformance)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    parent_rule_id: str | None = None       # if this rule was derived from another

    # ---- v1 fields (optional; populated when loading kb-rule/v1 packs) ----
    schema_version: int = 0                       # 0 = legacy v0, 1 = v1
    kind: str = "tendency"                        # constraint|tendency|heuristic|identity
    applies_to_v1: dict = field(default_factory=dict)  # {stage,tools,pdks,design_class,code_origin}
    predicts: list = field(default_factory=list)        # [{measurement_key,channel,value,horizon},...]
    prevents: list = field(default_factory=list)        # [{downstream_stage,downstream_key,...},...]
    examples: dict = field(default_factory=dict)        # {violating:[...], compliant:[...]}
    history: list = field(default_factory=list)         # append-only events
    authored_by: str = ""
    authored_at: str = ""

    def applies_to(self,
                    world: WorldModel,
                    stage: str | None = None,
                    reality=None,
                    scenario_target: dict | None = None) -> bool:
        """Whether this rule fires given the current world.

        v1 path uses the predicate evaluator over
        (facts ∪ measurements ∪ target). v0 path keeps the original
        tag-set behavior so legacy packs are unaffected.
        """
        if self.status == RuleStatus.RETIRED:
            return False

        if self.schema_version >= 1:
            # v1 stage filter: scenario stage must intersect applies_to.stage
            atstages = (self.applies_to_v1 or {}).get("stage") or []
            if stage and atstages and stage not in atstages:
                return False
            # v1 code_origin filter (best-effort: looks at WorldModel tag
            # `core_code_origin_<value>` if scenario has tagged it)
            from .predicates import evaluate_when_unless
            return evaluate_when_unless(self.when, self.unless,
                                          world, reality=reality,
                                          scenario_target=scenario_target)

        # ---- v0 legacy path: tag-set semantics ----
        if stage and self.stage and self.stage != stage:
            return False
        if not all(t in world.tags for t in self.when):
            return False
        if any(t in world.tags for t in self.unless):
            return False
        return True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["strength"] = self.strength.value
        d["status"] = self.status.value
        return d


# ---------------------------------------------------------------------------
# Confidence — the cognitive agent's epistemic state
# ---------------------------------------------------------------------------

class Confidence(Enum):
    """Five-step ladder. UNKNOWN is a *refusal* signal, not a prediction."""
    CERTAIN = "certain"            # 0.95
    CONFIDENT = "confident"        # 0.80
    LIKELY = "likely"              # 0.65
    UNCERTAIN = "uncertain"        # 0.50  -- below this = refuse
    UNKNOWN = "unknown"            # refuses to commit a prediction

    @property
    def p(self) -> float:
        return {"certain": 0.95, "confident": 0.80, "likely": 0.65,
                "uncertain": 0.50, "unknown": 0.0}[self.value]


# ---------------------------------------------------------------------------
# Prediction — what the agent commits to before observing reality
# ---------------------------------------------------------------------------

@dataclass
class Prediction:
    id: str
    question: str                          # the focused question being answered
    claim: str                             # the predicted answer (natural language)
    rationale: str                         # the reasoning chain
    confidence: Confidence
    falsifier: str                         # what reality would prove this wrong
    cited_rule_ids: list[str]              # rules this prediction leans on
    quantitative: dict | None = None       # optional numeric bounds (legacy)
    # Structured claim for the mechanical verdict engine. May contain any of:
    #   intervals: {key: [lo, hi]}
    #   enum:      {key: value}
    #   ranking:   {set_key, target, position_band: [lo, hi]}
    #   includes:  [substrings reality summary should contain]
    #   excludes:  [substrings reality summary must NOT contain]
    structured_claim: dict | None = None
    stage: str | None = None
    primary_model: str = ""                # which model produced this
    revisions: int = 0                     # how many revision rounds happened
    created_at: str = field(default_factory=_now)


@dataclass
class Refusal:
    """The agent declined to predict. Logged with reason."""
    id: str
    question: str
    reason: str                            # why the agent refused
    insufficient_rules: list[str]          # rule ids considered but rejected
    missing_evidence: list[str]            # what would unblock a prediction
    confidence: Confidence = Confidence.UNKNOWN
    created_at: str = field(default_factory=_now)


@dataclass
class VerifierVerdict:
    """A verifier model's judgment on a primary prediction."""
    verifier_model: str
    agrees: bool
    concerns: list[str]                    # specific issues raised
    suggested_revisions: list[str]         # what should change
    raw: str = ""                          # full verifier response


# ---------------------------------------------------------------------------
# Reality — what actually happened
# ---------------------------------------------------------------------------

@dataclass
class Reality:
    id: str
    source: str                            # e.g. "yosys+sv2v on ibex_core"
    measurements: dict[str, Any]
    artifacts: list[str] = field(default_factory=list)  # paths to logs/reports
    created_at: str = field(default_factory=_now)


# ---------------------------------------------------------------------------
# Verdict — the comparison
# ---------------------------------------------------------------------------

class VerdictKind(Enum):
    RIGHT_AND_RIGHT_REASON = "right_and_right_reason"
    RIGHT_BUT_WRONG_REASON = "right_but_wrong_reason"
    WRONG_BUT_RIGHT_DIRECTION = "wrong_but_right_direction"
    WRONG_AND_WRONG_REASON = "wrong_and_wrong_reason"
    UNFALSIFIABLE = "unfalsifiable"


@dataclass
class Verdict:
    id: str
    prediction_id: str
    kind: VerdictKind
    rule_attribution: dict[str, str]       # rule_id -> "supported" | "failed" | "neutral"
    notes: str = ""
    surprise: bool = False
    # How much to trust this mechanical verdict. "high" = numeric/enum/ranking
    # match — reflector should defer. "medium" = includes/excludes match —
    # reflector may overrule with rationale. "low" = text fallback —
    # reflector should treat as advisory only.
    verdict_confidence: str = "high"
    # Which structured channel produced this verdict (numeric|enum|ranking
    # |includes|excludes|text|none). Useful for debugging and reflector input.
    channel: str = "none"
    created_at: str = field(default_factory=_now)


@dataclass
class Surprise:
    """A wrong prediction that suggests the KB is missing or wrong."""
    id: str
    prediction_id: str
    verdict_id: str
    what_we_expected: str
    what_actually_happened: str
    why_we_missed_it: str                  # the agent's diagnosis
    suggested_kb_action: str               # natural language
    created_at: str = field(default_factory=_now)


# ---------------------------------------------------------------------------
# KBEdit — the rule-lifecycle action
# ---------------------------------------------------------------------------

class KBEditKind(Enum):
    ADD = "add"                            # new rule
    STRENGTHEN = "strengthen"              # bump strength up
    WEAKEN = "weaken"                      # bump strength down
    SCOPE = "scope"                        # add `unless` tags to narrow applicability
    RETIRE = "retire"                      # remove from active recall
    REWRITE = "rewrite"                    # replace statement


@dataclass
class KBEdit:
    id: str
    kind: KBEditKind
    target_rule_id: str | None             # None for ADD
    new_rule: Rule | None = None           # for ADD/REWRITE
    new_strength: RuleStrength | None = None  # for STRENGTHEN/WEAKEN
    added_unless: list[str] = field(default_factory=list)  # for SCOPE
    triggered_by_surprise_id: str | None = None
    rationale: str = ""
    applied: bool = False
    created_at: str = field(default_factory=_now)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def new_id(prefix: str) -> str:
    return _uid(prefix)


def jsonl_append(path: str, obj: Any):
    """Append one JSON record to a JSONL file."""
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        if hasattr(obj, "to_dict"):
            d = obj.to_dict()
        elif hasattr(obj, "__dataclass_fields__"):
            d = asdict(obj)
            # convert enums
            for k, v in list(d.items()):
                if hasattr(v, "value"):
                    d[k] = v.value
        else:
            d = obj
        f.write(json.dumps(d, default=str) + "\n")

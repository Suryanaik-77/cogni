"""
cogni.agent.verdict (v2)
========================
Mechanical comparison of Prediction vs Reality. Produces a Verdict.

This is intentionally not LLM-driven. Verdicts must be reproducible:
the same prediction + reality must always give the same kind. The
LLM-driven step is the *Reflector* which interprets the verdict.

v2 design (claim-typed dispatcher):
-----------------------------------
The predictor emits an optional `structured_claim` with up to five
channels. The verdict engine tries every channel the predictor filled,
in order of decreasing reliability:

  1. intervals    -> numeric bounds vs measurements[key]                 (high)
  2. enum         -> exact categorical equality                          (high)
  3. ranking      -> target lands inside position_band of a set          (high)
  4. includes     -> all required substrings present in reality summary  (medium)
  5. excludes     -> none of the forbidden substrings present            (medium)
  6. legacy       -> contains_verdict on positive_substrings (back-compat) (low)
  7. text-only    -> nothing structured -> UNFALSIFIABLE                 (low)

Each channel reports a per-channel verdict; the engine combines them:
- All channels agree right          -> RIGHT_AND_RIGHT_REASON
- All channels agree wrong          -> WRONG_AND_WRONG_REASON
- Numeric channels say wrong but    -> WRONG_BUT_RIGHT_DIRECTION
  predicted band is within tolerance
  of actual
- Channels disagree                 -> RIGHT_BUT_WRONG_REASON (mixed)
- No channel could decide           -> UNFALSIFIABLE

Verdict.verdict_confidence reflects which channel(s) decided:
- "high"   : numeric/enum/ranking decided cleanly
- "medium" : only includes/excludes decided
- "low"    : only legacy contains decided, or text-only fallback

Reflector reads `verdict_confidence` and `channel` and is instructed
to overrule low-confidence verdicts with rationale.

Backward compatibility:
- `numeric_verdict`, `categorical_verdict`, `contains_verdict` are kept
  as thin wrappers that call into the new engine. Nothing in the
  codebase that imports them needs to change.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from .core import Prediction, Reality, Verdict, VerdictKind, new_id


# ---------------------------------------------------------------------------
# Per-channel result
# ---------------------------------------------------------------------------

@dataclass
class _ChannelResult:
    channel: str                      # "intervals" | "enum" | ... | "text"
    decided: bool                     # could this channel render a verdict?
    correct: Optional[bool] = None    # True/False if decided
    direction_ok: bool = False        # for numeric: actual within tolerance of band
    confidence: str = "high"          # "high" | "medium" | "low"
    note: str = ""


# ---------------------------------------------------------------------------
# Channel implementations
# ---------------------------------------------------------------------------

def _channel_intervals(structured: dict, reality: Reality,
                       tolerance: float = 0.40) -> list[_ChannelResult]:
    """One result per (key, [lo, hi]) pair the predictor named."""
    out = []
    intervals = structured.get("intervals") or {}
    if not isinstance(intervals, dict):
        return out
    for key, bounds in intervals.items():
        if not (isinstance(bounds, (list, tuple)) and len(bounds) == 2
                and all(isinstance(x, (int, float)) for x in bounds)):
            continue
        actual = reality.measurements.get(key)
        if actual is None or not isinstance(actual, (int, float)):
            out.append(_ChannelResult(channel="intervals", decided=False,
                                      note=f"reality has no numeric '{key}'"))
            continue
        lo, hi = float(bounds[0]), float(bounds[1])
        a = float(actual)
        inside = lo <= a <= hi
        # Direction tolerance: actual within tol*midpoint of bounds.
        mid = 0.5 * (lo + hi)
        direction_ok = abs(a - mid) / max(abs(mid), 1e-9) <= tolerance
        out.append(_ChannelResult(
            channel="intervals", decided=True, correct=inside,
            direction_ok=direction_ok, confidence="high",
            note=f"{key}: actual={a}, band=[{lo}, {hi}]"
            + ("" if inside else f", direction_ok={direction_ok}"),
        ))
    return out


def _channel_enum(structured: dict, reality: Reality) -> list[_ChannelResult]:
    out = []
    enum = structured.get("enum") or {}
    if not isinstance(enum, dict):
        return out
    for key, expected in enum.items():
        actual = reality.measurements.get(key)
        if actual is None:
            out.append(_ChannelResult(channel="enum", decided=False,
                                      note=f"reality has no '{key}'"))
            continue
        # Case-insensitive string compare; otherwise exact equality.
        if isinstance(expected, str) and isinstance(actual, str):
            correct = actual.strip().lower() == expected.strip().lower()
        else:
            correct = actual == expected
        out.append(_ChannelResult(
            channel="enum", decided=True, correct=correct, confidence="high",
            note=f"{key}: actual={actual!r}, expected={expected!r}",
        ))
    return out


def _channel_ranking(structured: dict, reality: Reality) -> list[_ChannelResult]:
    out = []
    rank = structured.get("ranking") or {}
    if not isinstance(rank, dict):
        return out
    set_key = rank.get("set_key")
    target = rank.get("target")
    band = rank.get("position_band")
    if not (set_key and target and isinstance(band, (list, tuple)) and len(band) == 2):
        return out
    seq = reality.measurements.get(set_key)
    if not isinstance(seq, list):
        out.append(_ChannelResult(channel="ranking", decided=False,
                                  note=f"reality '{set_key}' is not a list"))
        return out
    # Find target's position (1-indexed). Each item may be a string or
    # an object with a 'name' or 'module' key.
    pos = None
    for i, item in enumerate(seq, start=1):
        name = item if isinstance(item, str) else (
            item.get("name") if isinstance(item, dict) else None
        ) or (item.get("module") if isinstance(item, dict) else None)
        if isinstance(name, str) and target.lower() in name.lower():
            pos = i
            break
    if pos is None:
        out.append(_ChannelResult(channel="ranking", decided=True, correct=False,
                                  confidence="high",
                                  note=f"target '{target}' not in '{set_key}'"))
        return out
    lo, hi = int(band[0]), int(band[1])
    correct = lo <= pos <= hi
    direction_ok = abs(pos - 0.5 * (lo + hi)) <= max(2, hi - lo)
    out.append(_ChannelResult(
        channel="ranking", decided=True, correct=correct,
        direction_ok=direction_ok, confidence="high",
        note=f"{target} at pos {pos}, band=[{lo}, {hi}]",
    ))
    return out


def _summary_text(reality: Reality, hint_keys: list[str]) -> str:
    """Pick the best string measurement to grade includes/excludes against.
    Hints are tried first; otherwise we concatenate all string measurements."""
    for k in hint_keys:
        v = reality.measurements.get(k)
        if isinstance(v, str) and v.strip():
            return v.lower()
    parts = [str(v) for v in reality.measurements.values() if isinstance(v, str)]
    return " | ".join(parts).lower()


def _channel_includes(structured: dict, reality: Reality,
                      summary_key_hints: list[str]) -> _ChannelResult:
    needed = structured.get("includes") or []
    if not (isinstance(needed, list) and needed):
        return _ChannelResult(channel="includes", decided=False)
    text = _summary_text(reality, summary_key_hints)
    if not text:
        return _ChannelResult(channel="includes", decided=False,
                              note="reality has no string summary")
    hits = [tok for tok in needed if isinstance(tok, str) and tok.lower() in text]
    # Includes is a SOFT match: any hit counts as right. The predictor is
    # asked for synonyms/roots, so requiring all-of would be too strict.
    correct = len(hits) > 0
    return _ChannelResult(
        channel="includes", decided=True, correct=correct, confidence="medium",
        note=f"hits={hits} of {needed}",
    )


def _channel_excludes(structured: dict, reality: Reality,
                      summary_key_hints: list[str]) -> _ChannelResult:
    forbidden = structured.get("excludes") or []
    if not (isinstance(forbidden, list) and forbidden):
        return _ChannelResult(channel="excludes", decided=False)
    text = _summary_text(reality, summary_key_hints)
    if not text:
        return _ChannelResult(channel="excludes", decided=False,
                              note="reality has no string summary")
    bad = [tok for tok in forbidden if isinstance(tok, str) and tok.lower() in text]
    correct = len(bad) == 0
    return _ChannelResult(
        channel="excludes", decided=True, correct=correct, confidence="medium",
        note=f"forbidden_hits={bad} of {forbidden}",
    )


def _channel_legacy_contains(claim_text: str, reality: Reality,
                             positive_substrings: list[str],
                             measurement_key: str) -> _ChannelResult:
    """Original contains-style check, kept for back-compat with scenarios
    whose ground_truth.json has `verdict.type: contains`. Treated as low
    confidence because of the empirical issues seen in run #1."""
    actual = reality.measurements.get(measurement_key)
    if actual is None or not positive_substrings:
        return _ChannelResult(channel="legacy", decided=False,
                              note=f"no '{measurement_key}' or no substrings")
    actual_str = str(actual).lower()
    claim_str = (claim_text or "").lower()
    # Old contract: substring must be in BOTH claim and reality.
    # Relaxed contract: substring must be in reality. (claim text has too
    # many wordings to anchor on.) Old behavior preserved if anyone
    # explicitly uses `contains_verdict`.
    hits = [t for t in positive_substrings if isinstance(t, str) and t.lower() in actual_str]
    correct = len(hits) > 0
    return _ChannelResult(
        channel="legacy", decided=True, correct=correct, confidence="low",
        note=f"hits={hits} (reality only)",
    )


# ---------------------------------------------------------------------------
# Combiner
# ---------------------------------------------------------------------------

_HIGH = {"intervals", "enum", "ranking"}
_MEDIUM = {"includes", "excludes"}
_LOW = {"legacy", "text"}


def _combine(channel_results: list[_ChannelResult]) -> tuple[VerdictKind, str, str]:
    """Returns (kind, verdict_confidence, channel_summary)."""
    decided = [c for c in channel_results if c.decided and c.correct is not None]
    if not decided:
        return VerdictKind.UNFALSIFIABLE, "low", "none"

    # Group by tier.
    high = [c for c in decided if c.channel in _HIGH]
    med = [c for c in decided if c.channel in _MEDIUM]
    low = [c for c in decided if c.channel in _LOW]

    def all_correct(xs):  return all(c.correct for c in xs)
    def all_wrong(xs):    return all(not c.correct for c in xs)

    # If the high tier decided anything, it's authoritative.
    if high:
        if all_correct(high):
            # Medium/low must not strongly contradict for clean RIGHT_AND_RIGHT.
            if med and all_wrong(med):
                return VerdictKind.RIGHT_BUT_WRONG_REASON, "high", "intervals/enum/ranking right, includes/excludes wrong"
            return VerdictKind.RIGHT_AND_RIGHT_REASON, "high", _channels_str(high)
        if all_wrong(high):
            # Numeric direction-ok rescues to wrong-but-right-direction.
            if any(c.channel == "intervals" and c.direction_ok for c in high):
                return VerdictKind.WRONG_BUT_RIGHT_DIRECTION, "high", _channels_str(high)
            return VerdictKind.WRONG_AND_WRONG_REASON, "high", _channels_str(high)
        # Mixed inside the high tier.
        return VerdictKind.RIGHT_BUT_WRONG_REASON, "high", _channels_str(high)

    # No high tier; fall back to medium.
    if med:
        if all_correct(med):
            return VerdictKind.RIGHT_AND_RIGHT_REASON, "medium", _channels_str(med)
        if all_wrong(med):
            return VerdictKind.WRONG_AND_WRONG_REASON, "medium", _channels_str(med)
        return VerdictKind.RIGHT_BUT_WRONG_REASON, "medium", _channels_str(med)

    # Only low/legacy decided.
    if all_correct(low):
        return VerdictKind.RIGHT_AND_RIGHT_REASON, "low", _channels_str(low)
    if all_wrong(low):
        return VerdictKind.WRONG_AND_WRONG_REASON, "low", _channels_str(low)
    return VerdictKind.RIGHT_BUT_WRONG_REASON, "low", _channels_str(low)


def _channels_str(xs: list[_ChannelResult]) -> str:
    return ",".join(c.channel for c in xs)


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------

def verdict_for(prediction: Prediction, reality: Reality,
                summary_key_hints: list[str] | None = None,
                # Legacy contains-style support:
                legacy_measurement_key: str | None = None,
                legacy_positive_substrings: list[str] | None = None) -> Verdict:
    """Unified mechanical verdict.

    The engine reads `prediction.quantitative` (legacy field) AND a
    `prediction.structured_claim` if present. Either path is fine.
    Scenarios may also pass legacy contains-style hints; the legacy
    channel is run as a low-confidence fallback when nothing structured
    decides.
    """
    summary_key_hints = summary_key_hints or []

    # Build a structured dict from whatever the predictor gave us.
    structured = {}
    sc = getattr(prediction, "structured_claim", None)
    if isinstance(sc, dict):
        structured.update(sc)
    # Legacy `quantitative` -> structured.intervals
    q = prediction.quantitative or {}
    if "intervals" not in structured:
        intervals = {}
        for k, v in q.items():
            if isinstance(v, (list, tuple)) and len(v) == 2 \
               and all(isinstance(x, (int, float)) for x in v):
                intervals[k] = [float(v[0]), float(v[1])]
        if intervals:
            structured["intervals"] = intervals

    results: list[_ChannelResult] = []
    results.extend(_channel_intervals(structured, reality))
    results.extend(_channel_enum(structured, reality))
    results.extend(_channel_ranking(structured, reality))
    results.append(_channel_includes(structured, reality, summary_key_hints))
    results.append(_channel_excludes(structured, reality, summary_key_hints))

    # Legacy contains channel — only if scenario passed it AND nothing
    # structured decided already.
    structured_decided_anything = any(c.decided for c in results)
    if (legacy_measurement_key and legacy_positive_substrings
            and not structured_decided_anything):
        results.append(_channel_legacy_contains(
            prediction.claim, reality,
            legacy_positive_substrings, legacy_measurement_key,
        ))

    kind, vconf, chan_summary = _combine(results)

    # Rule attribution: same rule list, status follows the kind.
    if kind == VerdictKind.RIGHT_AND_RIGHT_REASON:
        attr = "supported"
    elif kind in (VerdictKind.WRONG_AND_WRONG_REASON, VerdictKind.WRONG_BUT_RIGHT_DIRECTION):
        attr = "failed" if kind == VerdictKind.WRONG_AND_WRONG_REASON else "neutral"
    elif kind == VerdictKind.RIGHT_BUT_WRONG_REASON:
        attr = "neutral"
    else:
        attr = "neutral"
    rule_attribution = {rid: attr for rid in prediction.cited_rule_ids}

    notes_parts = []
    for r in results:
        if r.decided or (not r.decided and r.note):
            notes_parts.append(f"[{r.channel}] {r.note}")
    notes = " | ".join(notes_parts)[:600]

    return Verdict(
        id=new_id("vrd"),
        prediction_id=prediction.id,
        kind=kind,
        rule_attribution=rule_attribution,
        notes=notes,
        surprise=(kind != VerdictKind.RIGHT_AND_RIGHT_REASON),
        verdict_confidence=vconf,
        channel=chan_summary,
    )


# ---------------------------------------------------------------------------
# Back-compat shims (unchanged signatures; delegate to the new dispatcher)
# ---------------------------------------------------------------------------

def numeric_verdict(prediction: Prediction, reality: Reality,
                    measurement_key: str,
                    rule_attribution_default: str = "supported",
                    direction_tolerance: float = 0.40) -> Verdict:
    """Compare a numeric prediction.quantitative against reality.measurements[key].

    Kept for back-compat. Internally delegates to verdict_for with a
    structured_claim derived from prediction.quantitative.
    """
    # Force the dispatcher to focus on this one key by promoting it.
    bounds = None
    for v in (prediction.quantitative or {}).values():
        if isinstance(v, (list, tuple)) and len(v) == 2 \
           and all(isinstance(x, (int, float)) for x in v):
            bounds = [float(v[0]), float(v[1])]
            break
    if bounds is None:
        # Nothing numeric to grade — UNFALSIFIABLE preserves old behavior.
        return Verdict(
            id=new_id("vrd"), prediction_id=prediction.id,
            kind=VerdictKind.UNFALSIFIABLE,
            rule_attribution={rid: "neutral" for rid in prediction.cited_rule_ids},
            notes=f"reality lacks key '{measurement_key}' or prediction has no numeric bounds",
            verdict_confidence="low", channel="none",
        )
    # Inject as a single-keyed structured_claim so the dispatcher uses it.
    sc = {"intervals": {measurement_key: bounds}}
    setattr(prediction, "structured_claim",
            {**(getattr(prediction, "structured_claim", None) or {}), **sc})
    return verdict_for(prediction, reality, summary_key_hints=[measurement_key])


def categorical_verdict(prediction: Prediction, reality: Reality,
                        measurement_key: str,
                        expected_value_check: Callable[[Any, str], bool]) -> Verdict:
    """Categorical/string check via a custom predicate. Kept for back-compat."""
    actual = reality.measurements.get(measurement_key)
    if actual is None:
        return Verdict(
            id=new_id("vrd"), prediction_id=prediction.id,
            kind=VerdictKind.UNFALSIFIABLE,
            rule_attribution={rid: "neutral" for rid in prediction.cited_rule_ids},
            notes=f"reality lacks key '{measurement_key}'",
            verdict_confidence="low", channel="none",
        )
    matched = bool(expected_value_check(actual, prediction.claim))
    kind = VerdictKind.RIGHT_AND_RIGHT_REASON if matched else VerdictKind.WRONG_AND_WRONG_REASON
    attr = "supported" if matched else "failed"
    return Verdict(
        id=new_id("vrd"), prediction_id=prediction.id, kind=kind,
        rule_attribution={rid: attr for rid in prediction.cited_rule_ids},
        notes=f"actual={actual}",
        surprise=not matched,
        verdict_confidence="medium", channel="enum",
    )


def contains_verdict(prediction: Prediction, reality: Reality,
                     measurement_key: str,
                     positive_substrings: list[str]) -> Verdict:
    """Legacy substring grader. Now low-confidence by construction.

    The new dispatcher's `includes`/`excludes` channels should be preferred.
    Scenarios that still call this directly (via run_scenario.make_verdict)
    will route through the unified engine but be marked verdict_confidence='low'.
    """
    return verdict_for(
        prediction, reality,
        summary_key_hints=[measurement_key],
        legacy_measurement_key=measurement_key,
        legacy_positive_substrings=positive_substrings,
    )

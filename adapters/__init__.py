"""
cogni.adapters
==============

Adapter pairs for the cognitive agent.

A *pair* is (Perceiver, Oracle):
- Perceiver: reads raw stage inputs (RTL files, synth JSON, lint reports,
  weather observations, ...) and emits WorldModel facts.
- Oracle:    runs the deterministic ground-truth tool for that stage and
  returns a Reality keyed under a stage-prefixed namespace
  (`rtl.*`, `synth.*`, `pnr.*`, `weather.*`, ...).

Pairs are keyed by (stage, tool). Stage describes WHERE in the EDA flow
the prediction lives; tool describes WHICH implementation produces
ground truth.

Adding a new pair = drop two files under
adapters/<stage>/<tool>/{perceiver.py,oracle.py} and one entry in
_REGISTRY below. No agent-core changes.

We deliberately avoid plugin auto-discovery: an explicit registry is
trivially greppable, doesn't break with broken __init__ files, and
keeps refactor blast radius small.
"""
from __future__ import annotations

from typing import Any, Callable


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Each value is (perceiver_factory, oracle_factory). Factories take the
# scenario config dict for that side and return a constructed instance.
# Lazy imports keep optional heavy deps (e.g. verilator bindings) from
# loading unless the pair is actually used.

def _yosys_perceiver(_cfg: dict[str, Any]):
    from adapters.synth.yosys.perceiver import IbexRTLAdapter
    return IbexRTLAdapter()


def _yosys_oracle(cfg: dict[str, Any]):
    from adapters.synth.yosys.oracle import YosysOracle
    return YosysOracle(
        reports_dir=cfg["reports_dir"],
        findings_path=cfg.get("findings_path"),
    )


def _verilator_perceiver(cfg: dict[str, Any]):
    from adapters.rtl.verilator.perceiver import VerilatorRTLPerceiver
    return VerilatorRTLPerceiver(
        top=cfg.get("top"),
        include_dirs=cfg.get("include_dirs"),
        defines=cfg.get("defines"),
        manifest_path=cfg.get("manifest_path"),
    )


def _verilator_oracle(cfg: dict[str, Any]):
    from adapters.rtl.verilator.oracle import VerilatorRTLOracle
    return VerilatorRTLOracle(
        top=cfg.get("top"),
        rtl_root=cfg.get("rtl_root"),
        reports_dir=cfg.get("reports_dir"),
        findings_path=cfg.get("findings_path"),
    )


_REGISTRY: dict[tuple[str, str], tuple[Callable[..., Any], Callable[..., Any]]] = {
    # (stage, tool): (perceiver_factory, oracle_factory)
    ("synth", "yosys"):    (_yosys_perceiver,    _yosys_oracle),
    ("rtl",   "verilator"): (_verilator_perceiver, _verilator_oracle),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def make_perceiver(stage: str, tool: str, cfg: dict[str, Any] | None = None):
    """Build the perceiver for (stage, tool)."""
    pair = _REGISTRY.get((stage, tool))
    if pair is None:
        raise ValueError(
            f"unknown adapter pair: stage={stage!r} tool={tool!r}. "
            f"Known pairs: {sorted(_REGISTRY.keys())}"
        )
    return pair[0](cfg or {})


def make_oracle(stage: str, tool: str, cfg: dict[str, Any]):
    """Build the oracle for (stage, tool)."""
    pair = _REGISTRY.get((stage, tool))
    if pair is None:
        raise ValueError(
            f"unknown adapter pair: stage={stage!r} tool={tool!r}. "
            f"Known pairs: {sorted(_REGISTRY.keys())}"
        )
    return pair[1](cfg)


def known_pairs() -> list[tuple[str, str]]:
    return sorted(_REGISTRY.keys())

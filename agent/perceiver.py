"""
cogni.agent.perceiver
=====================
Generic perception. Takes raw inputs (file paths, strings, dicts) and an
*adapter* that knows how to turn those into Facts. The Perceiver itself
is domain-agnostic: it just calls the adapter and assembles a WorldModel.
"""
from __future__ import annotations
from typing import Protocol
from .core import WorldModel


class PerceiverAdapter(Protocol):
    """A domain-specific adapter. Lives in adapters/<domain>/."""
    domain: str

    def perceive(self, world: WorldModel, raw_input: str) -> None:
        """Mutate `world` by adding facts and tags from `raw_input`."""
        ...


class Perceiver:
    def __init__(self, adapter: PerceiverAdapter):
        self.adapter = adapter

    def perceive(self, raw_inputs: list[str]) -> WorldModel:
        world = WorldModel(domain=self.adapter.domain, raw_inputs=list(raw_inputs))
        if raw_inputs:
            for ri in raw_inputs:
                self.adapter.perceive(world, ri)
        else:
            # Adapter is configured to perceive from internal state
            # (manifest_path, fixture file, etc.) — call once with empty
            # raw_input so the manifest path fires.
            self.adapter.perceive(world, "")
        # Auto-attach a `<stage>_stage` tag based on the adapter's stage,
        # so v1 rules whose `when` clauses gate on `tag: synth_stage` /
        # `tag: rtl_stage` / etc. fire without forcing every adapter to
        # remember to set this tag itself.
        adapter_stage = getattr(self.adapter, "stage", None)
        if adapter_stage:
            world.tags.add(f"{adapter_stage}_stage")
        return world

"""
tests/conftest.py
=================
Shared fixtures for the cogni pytest suite.

The verdict layer is the most-coupled module with the agent's
correctness story. These fixtures give every test a one-line way to
build a Prediction or Reality without filling 8 required fields.
"""
from __future__ import annotations
import pytest
from agent.core import (
    Prediction, Reality, Confidence, new_id,
)


def make_prediction(
    *,
    claim: str = "test claim",
    rationale: str = "test rationale",
    confidence: Confidence = Confidence.LIKELY,
    falsifier: str = "would be falsified by X",
    cited_rule_ids: list[str] | None = None,
    quantitative: dict | None = None,
    structured_claim: dict | None = None,
    question: str = "test question",
    stage: str | None = None,
) -> Prediction:
    return Prediction(
        id=new_id("pred"),
        question=question,
        claim=claim,
        rationale=rationale,
        confidence=confidence,
        falsifier=falsifier,
        cited_rule_ids=cited_rule_ids or ["r1", "r2"],
        quantitative=quantitative,
        structured_claim=structured_claim,
        stage=stage,
        primary_model="test",
    )


def make_reality(measurements: dict, *, source: str = "test") -> Reality:
    return Reality(
        id=new_id("real"),
        source=source,
        measurements=measurements,
    )


@pytest.fixture
def pred():
    return make_prediction


@pytest.fixture
def real():
    return make_reality

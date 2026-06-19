"""
tests/test_schema.py
====================
Coverage for the minimal JSON Schema validator in agent.llm.

The validator is a deliberately small subset (type / required / properties /
items / enum). These tests pin down what we DO support so an organ author
doesn't accidentally write a schema that uses anyOf or oneOf and silently
gets a pass on invalid output.
"""
from __future__ import annotations
import pytest

from agent.llm import validate_schema


class TestPrimitives:
    def test_string_ok(self):
        validate_schema("hello", {"type": "string"})

    def test_string_wrong_type_raises(self):
        with pytest.raises(ValueError, match="expected string"):
            validate_schema(42, {"type": "string"})

    def test_number_accepts_int_and_float(self):
        validate_schema(1, {"type": "number"})
        validate_schema(1.5, {"type": "number"})

    def test_number_rejects_bool(self):
        # Python's bool is a subclass of int; the validator must reject it.
        with pytest.raises(ValueError, match="expected number"):
            validate_schema(True, {"type": "number"})

    def test_integer_rejects_float(self):
        with pytest.raises(ValueError, match="expected integer"):
            validate_schema(1.5, {"type": "integer"})

    def test_boolean_ok(self):
        validate_schema(True, {"type": "boolean"})
        validate_schema(False, {"type": "boolean"})

    def test_enum_membership(self):
        validate_schema("rainy", {"type": "string", "enum": ["rainy", "sunny"]})
        with pytest.raises(ValueError, match="not in enum"):
            validate_schema("foggy", {"type": "string", "enum": ["rainy", "sunny"]})


class TestObjects:
    def test_required_keys_must_be_present(self):
        s = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
        validate_schema({"name": "ok"}, s)
        with pytest.raises(ValueError, match="missing required"):
            validate_schema({}, s)

    def test_optional_property_can_be_null(self):
        s = {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "tag":  {"type": "string"},
            },
        }
        # tag=None is allowed because 'tag' is optional.
        validate_schema({"name": "ok", "tag": None}, s)

    def test_property_type_validated_recursively(self):
        s = {
            "type": "object",
            "required": ["count"],
            "properties": {"count": {"type": "integer"}},
        }
        with pytest.raises(ValueError, match="count.*expected integer"):
            validate_schema({"count": "five"}, s)

    def test_unknown_properties_are_allowed(self):
        # Validator is permissive on extra keys (no `additionalProperties`
        # support on purpose). This pins that.
        s = {"type": "object", "properties": {"name": {"type": "string"}}}
        validate_schema({"name": "ok", "extra": 123}, s)


class TestArrays:
    def test_array_of_strings_ok(self):
        validate_schema(["a", "b"], {"type": "array", "items": {"type": "string"}})

    def test_array_item_type_enforced(self):
        with pytest.raises(ValueError, match=r"\[1\].*expected string"):
            validate_schema(["a", 2], {"type": "array", "items": {"type": "string"}})

    def test_array_wrong_type_raises(self):
        with pytest.raises(ValueError, match="expected array"):
            validate_schema({"a": 1}, {"type": "array", "items": {"type": "string"}})


class TestRealisticOrganSchemas:
    """Schemas drawn from agent/organs.py shapes — make sure they still pass."""

    def test_predictor_minimal_payload(self):
        # Predictor schema requires a claim, rationale, confidence enum,
        # falsifier, and cited_rule_ids list. structured_claim and
        # quantitative are optional.
        schema = {
            "type": "object",
            "required": ["claim", "rationale", "confidence", "falsifier", "cited_rule_ids"],
            "properties": {
                "claim":           {"type": "string"},
                "rationale":       {"type": "string"},
                "confidence":      {"type": "string",
                                    "enum": ["certain", "confident", "likely",
                                             "uncertain", "unknown"]},
                "falsifier":       {"type": "string"},
                "cited_rule_ids":  {"type": "array", "items": {"type": "string"}},
                "quantitative":    {"type": "object"},
                "structured_claim":{"type": "object"},
            },
        }
        ok = {
            "claim": "area is 1500-1800 um2",
            "rationale": "by rule r1",
            "confidence": "likely",
            "falsifier": "if synthesis reports < 800 um2",
            "cited_rule_ids": ["r1"],
        }
        validate_schema(ok, schema)

    def test_predictor_bad_confidence_value_rejected(self):
        schema = {
            "type": "object",
            "required": ["confidence"],
            "properties": {
                "confidence": {"type": "string",
                               "enum": ["certain", "confident", "likely",
                                        "uncertain", "unknown"]},
            },
        }
        with pytest.raises(ValueError, match="not in enum"):
            validate_schema({"confidence": "very-likely"}, schema)

    def test_verifier_schema(self):
        schema = {
            "type": "object",
            "required": ["agrees", "concerns", "suggested_revisions"],
            "properties": {
                "agrees":              {"type": "boolean"},
                "concerns":            {"type": "array", "items": {"type": "string"}},
                "suggested_revisions": {"type": "array", "items": {"type": "string"}},
            },
        }
        validate_schema({"agrees": True, "concerns": [], "suggested_revisions": []}, schema)

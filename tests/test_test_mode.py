"""
tests/test_test_mode.py
========================
Pin down the test-mode model-swap contract.

Production runs must default to Opus + gpt-5. Test-mode runs (env var
or --test-mode flag, both routed through enable_test_mode()) swap to
Sonnet + gpt-5-mini. This test makes sure the swap propagates to
agent.organs which captures the constants by value at import time.
"""
from __future__ import annotations
import importlib
import os

import pytest


def _fresh_import(env_value: str | None):
    """Re-import agent.llm and agent.organs with a controlled env var.
    Returns (llm_module, organs_module).
    """
    if env_value is None:
        os.environ.pop("COGNI_TEST_MODE", None)
    else:
        os.environ["COGNI_TEST_MODE"] = env_value
    # Reload in dependency order.
    import agent.llm as llm
    import agent.organs as organs
    importlib.reload(llm)
    importlib.reload(organs)
    return llm, organs


class TestDefaultMode:
    def test_default_uses_opus_and_gpt5(self):
        llm, organs = _fresh_import(None)
        assert llm.MODEL_OPUS == "claude_opus_4_7"
        assert llm.MODEL_GPT == "gpt_5_4"
        assert organs.MODEL_OPUS == "claude_opus_4_7"
        assert organs.MODEL_GPT == "gpt_5_4"
        assert llm.is_test_mode() is False


class TestEnvVarMode:
    def test_env_var_swaps_to_sonnet_and_mini(self):
        llm, organs = _fresh_import("1")
        assert llm.MODEL_OPUS == "claude_sonnet_4_6"
        assert llm.MODEL_GPT == "gpt_5_4_mini"
        assert organs.MODEL_OPUS == "claude_sonnet_4_6"
        assert organs.MODEL_GPT == "gpt_5_4_mini"
        assert llm.is_test_mode() is True

    def test_env_var_truthy_variants(self):
        for v in ("1", "true", "yes"):
            llm, _ = _fresh_import(v)
            assert llm.is_test_mode() is True, f"{v!r} should enable test mode"

    def test_env_var_falsy_variants(self):
        for v in ("0", "false", "no", ""):
            llm, _ = _fresh_import(v)
            assert llm.is_test_mode() is False, f"{v!r} should not enable test mode"


class TestRuntimeEnable:
    def test_enable_after_import_propagates_to_organs(self):
        # Start in default mode; flip on; both modules should observe it.
        llm, organs = _fresh_import(None)
        assert organs.MODEL_OPUS == "claude_opus_4_7"
        llm.enable_test_mode()
        assert llm.MODEL_OPUS == "claude_sonnet_4_6"
        assert llm.MODEL_GPT == "gpt_5_4_mini"
        assert organs.MODEL_OPUS == "claude_sonnet_4_6"
        assert organs.MODEL_GPT == "gpt_5_4_mini"
        assert llm.is_test_mode() is True

    def teardown_method(self):
        # Don't leak test-mode state into sibling test modules.
        os.environ.pop("COGNI_TEST_MODE", None)
        import agent.llm as llm
        importlib.reload(llm)
        import agent.organs as organs
        importlib.reload(organs)

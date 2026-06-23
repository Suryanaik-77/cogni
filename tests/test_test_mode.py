"""
tests/test_test_mode.py
========================
Pin down the test-mode model-swap contract.

NOTE: AWS Bedrock is now the SYSTEM default (every role on Bedrock). The
test-mode swap (Opus->Sonnet, gpt-5->gpt-5-mini) only applies on the
*direct-API* path, which is opt-in via COGNI_DIRECT_API=1 / COGNI_BEDROCK=0.
So the swap tests below force direct mode; a separate class pins the
Bedrock default. The swap must still propagate to agent.organs, which
captures the constants by value at import time.
"""
from __future__ import annotations
import importlib
import os

import pytest


@pytest.fixture(autouse=True)
def _clean_env():
    """Keep mode env vars from leaking across tests/sibling modules."""
    yield
    for k in ("COGNI_TEST_MODE", "COGNI_DIRECT_API", "COGNI_BEDROCK"):
        os.environ.pop(k, None)
    import agent.llm as llm
    import agent.organs as organs
    importlib.reload(llm)
    importlib.reload(organs)


def _fresh_import(env_value: str | None, *, direct: bool = True):
    """Re-import agent.llm and agent.organs with a controlled env var.

    direct=True forces the direct-API path (COGNI_DIRECT_API=1) so the
    test-mode model swap is in effect; direct=False leaves the Bedrock
    default in place. Returns (llm_module, organs_module).
    """
    if direct:
        os.environ["COGNI_DIRECT_API"] = "1"
    else:
        os.environ.pop("COGNI_DIRECT_API", None)
        os.environ.pop("COGNI_BEDROCK", None)
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


class TestBedrockDefault:
    def test_system_default_is_bedrock(self):
        # No mode env vars at all -> Bedrock is the sole path.
        llm, organs = _fresh_import(None, direct=False)
        assert llm.is_bedrock_mode() is True
        assert llm.MODEL_OPUS == "bedrock_claude"
        assert llm.MODEL_GPT == "bedrock_llama"
        assert llm.MODEL_GEMINI == "bedrock_mistral"
        assert organs.MODEL_OPUS == "bedrock_claude"

    def test_bedrock_overrides_test_mode(self):
        # Bedrock default + test-mode set -> still Bedrock ids (no swap).
        llm, _ = _fresh_import("1", direct=False)
        assert llm.is_bedrock_mode() is True
        assert llm.MODEL_OPUS == "bedrock_claude"
        assert llm.MODEL_GPT == "bedrock_llama"


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

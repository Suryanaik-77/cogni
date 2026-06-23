"""
cogni.agent.llm
===============
Thin abstraction over model invocation.

In this sandbox we use the platform's `run_subagent` mechanism: each
LLM call is a subagent that reads its inputs from a file, runs in the
specified model, and writes structured JSON output to another file.

The orchestrator (in agent/orchestrator.py) is responsible for actually
spawning subagents — this module just defines the contract: what a
"call" looks like, what files it reads/writes, and how to validate
the JSON response.

Design choice: instead of an HTTP client, an LLMCall is a *task spec*.
The orchestrator (which has run_subagent access) executes them.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from typing import Any


# Models exposed via run_subagent / direct API.
#
# Test-mode swap (controlled by env var COGNI_TEST_MODE=1):
#   Opus  -> Sonnet      (cheaper, faster, less aggressive refusal)
#   gpt-5 -> gpt-5-mini  (cuts the verifier latency floor by ~3x)
# Gemini is already on Flash so no swap needed there.
#
# The dynamic-default pattern means every organ that imports MODEL_OPUS /
# MODEL_GPT picks up the swap automatically — no per-organ branching.
import os as _os

_TEST_MODE = _os.environ.get("COGNI_TEST_MODE", "").strip() in ("1", "true", "yes")

# Bedrock is the ONLY model path by default: every role (predict, both
# verifiers, reflect, and the fixer) runs on AWS Bedrock-hosted models —
# one AWS privacy boundary, and only AWS credentials are required (no
# ANTHROPIC/OPENAI/GOOGLE keys). The three subagent ids below resolve to
# concrete Bedrock model ids in transports.py (_MODEL_ROUTING), each
# overridable via env. Bedrock takes precedence over the test-mode swap.
#
# Escape hatch — opt back into the direct vendor APIs (Anthropic/OpenAI/
# Google) with COGNI_BEDROCK=0  (or COGNI_DIRECT_API=1).
_DIRECT_API = (
    _os.environ.get("COGNI_BEDROCK", "").strip() in ("0", "false", "no")
    or _os.environ.get("COGNI_DIRECT_API", "").strip() in ("1", "true", "yes")
)
_BEDROCK_MODE = not _DIRECT_API

if _BEDROCK_MODE:
    MODEL_OPUS   = "bedrock_claude"     # predictor / reflector  (Claude on Bedrock)
    MODEL_GPT    = "bedrock_llama"      # verifier 1             (Llama on Bedrock)
    MODEL_GEMINI = "bedrock_mistral"    # verifier 2             (Mistral/Nova on Bedrock)
else:
    MODEL_OPUS   = "claude_sonnet_4_6" if _TEST_MODE else "claude_opus_4_7"
    MODEL_GPT    = "gpt_5_4_mini" if _TEST_MODE else "gpt_5_4"
    MODEL_GEMINI = "gemini_3_1_pro"

MODEL_SONNET       = "claude_sonnet_4_6"
MODEL_GPT_MINI     = "gpt_5_4_mini"
MODEL_GEMINI_FLASH = "gemini_3_flash"


def is_test_mode() -> bool:
    """True when COGNI_TEST_MODE is set. Used by run banners and logging."""
    return _TEST_MODE


def is_bedrock_mode() -> bool:
    """True when all roles run on AWS Bedrock. This is the DEFAULT; it is
    only False when COGNI_BEDROCK=0 / COGNI_DIRECT_API=1 selects the direct
    vendor APIs."""
    return _BEDROCK_MODE


def enable_test_mode() -> None:
    """Programmatically enable test mode after import.

    The module-level MODEL_OPUS / MODEL_GPT constants are resolved at
    import time, so a CLI flag that runs after `import agent.llm` won't
    affect them. This function rebinds those names so subsequent imports
    of `from agent.llm import MODEL_OPUS` (e.g. via organs.py freshly
    re-imported, or call sites that read the attribute) see the swap.

    Note: organs.py captures MODEL_OPUS at its own import time too, so
    we update it there as well if it's already imported.
    """
    global _TEST_MODE, MODEL_OPUS, MODEL_GPT
    _os.environ["COGNI_TEST_MODE"] = "1"
    _TEST_MODE = True
    # In Bedrock mode the role->model mapping is fixed to Bedrock ids; the
    # test-mode swap must not clobber it.
    if _BEDROCK_MODE:
        return
    MODEL_OPUS = "claude_sonnet_4_6"
    MODEL_GPT = "gpt_5_4_mini"
    # Propagate to organs.py if already imported (it does
    # `from .llm import LLMCall, MODEL_OPUS, MODEL_GPT, MODEL_GEMINI`,
    # which copies the *value* into its own namespace).
    import sys
    organs = sys.modules.get("agent.organs")
    if organs is not None:
        organs.MODEL_OPUS = MODEL_OPUS
        organs.MODEL_GPT = MODEL_GPT


@dataclass
class LLMCall:
    """A single LLM invocation as a file-based contract.

    The agent writes `prompt_path` and `schema_path`. The subagent reads
    them, performs the reasoning, validates against the schema, and
    writes `output_path`. The orchestrator then loads `output_path`.
    """
    name: str                      # e.g. "predictor.synthesis_area"
    model: str                     # subagent model id
    role: str                      # "predictor" | "verifier" | "attention" | "reflector"
    prompt: str                    # the natural-language instructions
    schema: dict                   # JSON Schema the response must satisfy
    inputs: dict[str, Any] = field(default_factory=dict)  # context fed to the model
    output_path: str = ""          # where the subagent writes its JSON answer

    def write_brief(self, run_dir: str) -> dict[str, str]:
        """Materialize the call as files. Returns paths."""
        call_dir = os.path.join(run_dir, "llm_calls", self.name)
        os.makedirs(call_dir, exist_ok=True)
        prompt_path = os.path.join(call_dir, "prompt.md")
        schema_path = os.path.join(call_dir, "schema.json")
        inputs_path = os.path.join(call_dir, "inputs.json")
        output_path = self.output_path or os.path.join(call_dir, "output.json")

        # Always UTF-8: prompts/inputs carry em-dashes and other non-ASCII
        # from RTL/question text; the OS default encoding may be latin-1.
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(self.prompt)
        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump(self.schema, f, indent=2)
        with open(inputs_path, "w", encoding="utf-8") as f:
            json.dump(self.inputs, f, indent=2, default=str)

        self.output_path = output_path
        return {
            "prompt": prompt_path,
            "schema": schema_path,
            "inputs": inputs_path,
            "output": output_path,
            "dir": call_dir,
        }

    def subagent_objective(self, run_dir: str = "") -> str:
        """The instruction string fed to run_subagent. Uses absolute paths
        when run_dir is provided so the subagent can find files regardless
        of its working directory."""
        if run_dir:
            paths = {
                "prompt":  os.path.join(run_dir, "llm_calls", self.name, "prompt.md"),
                "schema":  os.path.join(run_dir, "llm_calls", self.name, "schema.json"),
                "inputs":  os.path.join(run_dir, "llm_calls", self.name, "inputs.json"),
                "output":  os.path.join(run_dir, "llm_calls", self.name, "output.json"),
            }
        else:
            paths = {
                "prompt":  os.path.join("llm_calls", self.name, "prompt.md"),
                "schema":  os.path.join("llm_calls", self.name, "schema.json"),
                "inputs":  os.path.join("llm_calls", self.name, "inputs.json"),
                "output":  os.path.join("llm_calls", self.name, "output.json"),
            }
        return f"""You are a {self.role} in a cognitive-agent framework.

Your inputs:
- Instructions: read `{paths['prompt']}`
- Context (JSON): read `{paths['inputs']}`
- Output schema (JSON Schema): read `{paths['schema']}`

Your task:
1. Read the three input files.
2. Follow the instructions, using the context.
3. Produce a JSON object that strictly matches the output schema.
4. Write your JSON output to: `{paths['output']}`
5. Do NOT add commentary. Do NOT print the JSON. Only write the file.

Be substantive and precise. The framework will validate your output
against the schema and use it directly for downstream reasoning.
"""

    def load_output(self) -> dict:
        """Load and validate the subagent's JSON response."""
        if not os.path.exists(self.output_path):
            raise FileNotFoundError(f"LLM output not yet written: {self.output_path}")
        with open(self.output_path, encoding="utf-8") as f:
            data = json.load(f)
        validate_schema(data, self.schema)
        return data


def validate_schema(data: Any, schema: dict, path: str = "$"):
    """Minimal JSON Schema validator (subset: type, required, properties, items, enum).
    Raises ValueError on mismatch.
    """
    t = schema.get("type")
    if t == "object":
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected object, got {type(data).__name__}")
        for req in schema.get("required", []):
            if req not in data:
                raise ValueError(f"{path}: missing required key '{req}'")
        for k, sub in schema.get("properties", {}).items():
            if k in data:
                # Allow null on properties that aren't required.
                if data[k] is None and k not in schema.get("required", []):
                    continue
                validate_schema(data[k], sub, f"{path}.{k}")
    elif t == "array":
        if not isinstance(data, list):
            raise ValueError(f"{path}: expected array, got {type(data).__name__}")
        items = schema.get("items")
        if items:
            for i, x in enumerate(data):
                validate_schema(x, items, f"{path}[{i}]")
    elif t == "string":
        if not isinstance(data, str):
            raise ValueError(f"{path}: expected string, got {type(data).__name__}")
        if "enum" in schema and data not in schema["enum"]:
            raise ValueError(f"{path}: '{data}' not in enum {schema['enum']}")
    elif t == "number":
        if not isinstance(data, (int, float)) or isinstance(data, bool):
            raise ValueError(f"{path}: expected number, got {type(data).__name__}")
    elif t == "integer":
        if not isinstance(data, int) or isinstance(data, bool):
            raise ValueError(f"{path}: expected integer, got {type(data).__name__}")
    elif t == "boolean":
        if not isinstance(data, bool):
            raise ValueError(f"{path}: expected boolean, got {type(data).__name__}")
    # null, anyOf, oneOf, etc. omitted — small surface on purpose

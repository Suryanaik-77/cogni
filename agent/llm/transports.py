"""
cogni.agent.llm.transports
==========================
Direct-API transports for LLM calls. Same file-handshake contract as the
subagent dispatcher: read prompt.md / inputs.json / schema.json from a
call directory, execute against a vendor API, write output.json.

Each transport exposes BOTH a sync `run()` and an async `arun()` so the
orchestrator can fan out concurrently with `asyncio.gather`.

Vendors supported:
  - ClaudeTransport      (claude-opus, claude-sonnet via anthropic)
  - OpenAITransport      (gpt-5 family via openai)
  - GeminiTransport      (gemini-2.5-pro/flash via google-genai)
  - BedrockTransport     (Claude / Llama / Mistral / Nova via AWS Bedrock
                          Converse API — one AWS privacy boundary)

All transports load .env once on import and never log keys.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent.llm import validate_schema

# Load .env from project root once.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")

log = logging.getLogger("cogni.transports")


# ---------------------------------------------------------------------------
# Brief I/O
# ---------------------------------------------------------------------------

def _read_brief(call_dir: str) -> tuple[str, dict, dict]:
    with open(os.path.join(call_dir, "prompt.md")) as f:
        prompt = f.read()
    with open(os.path.join(call_dir, "inputs.json")) as f:
        inputs = json.load(f)
    with open(os.path.join(call_dir, "schema.json")) as f:
        schema = json.load(f)
    return prompt, inputs, schema


def _build_user_message(prompt: str, inputs: dict, schema: dict) -> str:
    return (
        f"{prompt}\n\n"
        f"---\n"
        f"# Context (JSON)\n```json\n{json.dumps(inputs, indent=2, default=str)}\n```\n\n"
        f"---\n"
        f"# Output schema (JSON Schema)\n```json\n{json.dumps(schema, indent=2)}\n```\n\n"
        f"Return ONLY a JSON object that strictly matches the schema. "
        f"No prose, no markdown fencing, no commentary."
    )


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if m:
        return json.loads(m.group(1))
    first = text.find("{"); last = text.rfind("}")
    if first != -1 and last > first:
        return json.loads(text[first : last + 1])
    raise ValueError(f"Could not extract JSON from response (len={len(text)})")


def _write_output(call_dir: str, output: dict) -> None:
    with open(os.path.join(call_dir, "output.json"), "w") as f:
        json.dump(output, f, indent=2)


def _write_meta(call_dir: str, meta: dict) -> None:
    """Persist per-call metadata (latency, tokens, model, errors).

    Always written, on success and failure, so a session_dir is fully
    self-describing without needing the in-memory TransportResult.
    """
    try:
        with open(os.path.join(call_dir, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        # Never let metadata-writing kill a real result.
        pass


def _write_error(call_dir: str, error: str, attempts: int, raw_text: str = "") -> None:
    """Persist a human-readable error.txt next to output.json on failure.

    The dual-verifier design absorbs single-transport failures, but a
    silent failure means we lose the signal. error.txt makes failures
    auditable across runs.
    """
    try:
        with open(os.path.join(call_dir, "error.txt"), "w") as f:
            f.write(f"attempts: {attempts}\nerror: {error}\n")
            if raw_text:
                f.write(f"---\nlast raw response:\n{raw_text[:4000]}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Transport result + base class
# ---------------------------------------------------------------------------

@dataclass
class TransportResult:
    ok: bool
    output: dict | None
    raw_text: str
    elapsed_s: float
    error: str | None = None
    transport: str = ""
    model: str = ""
    # Token usage (best-effort; vendor-specific names normalized).
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 1


class Transport:
    """Sync + async file-in / file-out adapter."""
    name = "transport"
    model: str = ""

    def run(self, call_dir: str) -> TransportResult:
        return asyncio.run(self.arun(call_dir))

    async def arun(self, call_dir: str) -> TransportResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Anthropic (Claude)
# ---------------------------------------------------------------------------

class ClaudeTransport(Transport):
    name = "anthropic"

    def __init__(self,
                 model: str = "claude-opus-4-5",
                 api_key: str | None = None,
                 max_tokens: int = 8000,
                 max_retries: int = 2,
                 temperature: float = 0.0):
        from anthropic import AsyncAnthropic
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self.model = model
        self.client = AsyncAnthropic(api_key=key)
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.temperature = temperature

    async def arun(self, call_dir: str) -> TransportResult:
        prompt, inputs, schema = _read_brief(call_dir)
        user = _build_user_message(prompt, inputs, schema)
        t0 = time.time()
        last_err = None
        last_text = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    system="You are a precise reasoning component. Return only valid JSON matching the requested schema.",
                    messages=[{"role": "user", "content": user}],
                )
                # Concatenate all text blocks.
                text = "".join(
                    b.text for b in resp.content if getattr(b, "type", "") == "text"
                )
                last_text = text
                output = _extract_json(text)
                validate_schema(output, schema)
                _write_output(call_dir, output)
                in_tok = getattr(resp.usage, "input_tokens", 0) or 0
                out_tok = getattr(resp.usage, "output_tokens", 0) or 0
                elapsed = time.time() - t0
                _write_meta(call_dir, {
                    "ok": True, "transport": self.name, "model": self.model,
                    "elapsed_s": elapsed, "attempts": attempt + 1,
                    "input_tokens": in_tok, "output_tokens": out_tok,
                })
                return TransportResult(
                    True, output, text, elapsed,
                    transport=self.name, model=self.model,
                    input_tokens=in_tok, output_tokens=out_tok, attempts=attempt + 1,
                )
            except Exception as e:
                last_err = e
                log.warning("claude attempt %d failed: %s", attempt + 1, e)
                if attempt < self.max_retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        elapsed = time.time() - t0
        err_str = f"{type(last_err).__name__}: {last_err}"
        _write_error(call_dir, err_str, self.max_retries + 1, last_text)
        _write_meta(call_dir, {
            "ok": False, "transport": self.name, "model": self.model,
            "elapsed_s": elapsed, "attempts": self.max_retries + 1,
            "error": err_str,
        })
        return TransportResult(
            False, None, last_text, elapsed,
            error=err_str, transport=self.name, model=self.model,
            attempts=self.max_retries + 1,
        )


# ---------------------------------------------------------------------------
# OpenAI (gpt-5)
# ---------------------------------------------------------------------------

class OpenAITransport(Transport):
    name = "openai"

    def __init__(self, model: str = "gpt-5",
                 api_key: str | None = None,
                 max_retries: int = 2,
                 temperature: float = 0.0,
                 seed: int | None = 42):
        from openai import AsyncOpenAI
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set")
        self.model = model
        self.client = AsyncOpenAI(api_key=key)
        self.max_retries = max_retries
        self.temperature = temperature
        # Allow per-run seed override via env var (used by the ensemble harness
        # to A/B prompt changes with different RNG draws).
        env_seed = os.environ.get("COGNI_SEED")
        if env_seed is not None and env_seed != "":
            try:
                self.seed = int(env_seed)
            except ValueError:
                self.seed = seed
        else:
            self.seed = seed

    async def arun(self, call_dir: str) -> TransportResult:
        prompt, inputs, schema = _read_brief(call_dir)
        user = _build_user_message(prompt, inputs, schema)
        t0 = time.time()
        last_err = None
        last_text = ""
        for attempt in range(self.max_retries + 1):
            try:
                # gpt-5 reasoning models reject `temperature` and `seed`
                # at the API surface; only pass them on non-reasoning models.
                kwargs = {
                    "model": self.model,
                    "messages": [
                        {"role": "system",
                         "content": "You are a precise reasoning component. Return only valid JSON."},
                        {"role": "user", "content": user},
                    ],
                    "response_format": {"type": "json_object"},
                }
                if not self.model.startswith("gpt-5"):
                    kwargs["temperature"] = self.temperature
                    if self.seed is not None:
                        kwargs["seed"] = self.seed
                resp = await self.client.chat.completions.create(**kwargs)
                text = resp.choices[0].message.content or ""
                last_text = text
                output = _extract_json(text)
                validate_schema(output, schema)
                _write_output(call_dir, output)
                in_tok = getattr(resp.usage, "prompt_tokens", 0) or 0
                out_tok = getattr(resp.usage, "completion_tokens", 0) or 0
                elapsed = time.time() - t0
                _write_meta(call_dir, {
                    "ok": True, "transport": self.name, "model": self.model,
                    "elapsed_s": elapsed, "attempts": attempt + 1,
                    "input_tokens": in_tok, "output_tokens": out_tok,
                })
                return TransportResult(
                    True, output, text, elapsed,
                    transport=self.name, model=self.model,
                    input_tokens=in_tok, output_tokens=out_tok, attempts=attempt + 1,
                )
            except Exception as e:
                last_err = e
                log.warning("openai attempt %d failed: %s", attempt + 1, e)
                if attempt < self.max_retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        elapsed = time.time() - t0
        err_str = f"{type(last_err).__name__}: {last_err}"
        _write_error(call_dir, err_str, self.max_retries + 1, last_text)
        _write_meta(call_dir, {
            "ok": False, "transport": self.name, "model": self.model,
            "elapsed_s": elapsed, "attempts": self.max_retries + 1,
            "error": err_str,
        })
        return TransportResult(
            False, None, last_text, elapsed,
            error=err_str, transport=self.name, model=self.model,
            attempts=self.max_retries + 1,
        )


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

class GeminiTransport(Transport):
    name = "gemini"

    def __init__(self, model: str = "gemini-2.5-flash",
                 api_key: str | None = None,
                 max_retries: int = 2,
                 temperature: float = 0.0):
        from google import genai
        key = (api_key
               or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY"))
        if not key:
            raise RuntimeError("GEMINI_API_KEY not set")
        self.model = model
        self.client = genai.Client(api_key=key)
        self.max_retries = max_retries
        self.temperature = temperature

    async def arun(self, call_dir: str) -> TransportResult:
        from google.genai import types as gtypes
        prompt, inputs, schema = _read_brief(call_dir)
        user = _build_user_message(prompt, inputs, schema)
        t0 = time.time()
        last_err = None
        last_text = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.client.aio.models.generate_content(
                    model=self.model,
                    contents=user,
                    config=gtypes.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=self.temperature,
                    ),
                )
                text = (resp.text or "").strip()
                last_text = text
                output = _extract_json(text)
                validate_schema(output, schema)
                _write_output(call_dir, output)
                # Gemini exposes token counts via usage_metadata.
                um = getattr(resp, "usage_metadata", None)
                in_tok = getattr(um, "prompt_token_count", 0) or 0
                out_tok = getattr(um, "candidates_token_count", 0) or 0
                elapsed = time.time() - t0
                _write_meta(call_dir, {
                    "ok": True, "transport": self.name, "model": self.model,
                    "elapsed_s": elapsed, "attempts": attempt + 1,
                    "input_tokens": in_tok, "output_tokens": out_tok,
                })
                return TransportResult(
                    True, output, text, elapsed,
                    transport=self.name, model=self.model,
                    input_tokens=in_tok, output_tokens=out_tok, attempts=attempt + 1,
                )
            except Exception as e:
                last_err = e
                log.warning("gemini attempt %d failed: %s", attempt + 1, e)
                if attempt < self.max_retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        elapsed = time.time() - t0
        err_str = f"{type(last_err).__name__}: {last_err}"
        _write_error(call_dir, err_str, self.max_retries + 1, last_text)
        _write_meta(call_dir, {
            "ok": False, "transport": self.name, "model": self.model,
            "elapsed_s": elapsed, "attempts": self.max_retries + 1,
            "error": err_str,
        })
        return TransportResult(
            False, None, last_text, elapsed,
            error=err_str, transport=self.name, model=self.model,
            attempts=self.max_retries + 1,
        )


# ---------------------------------------------------------------------------
# AWS Bedrock (Claude / Llama / Mistral / Nova via the Converse API)
# ---------------------------------------------------------------------------

class BedrockTransport(Transport):
    """One transport for ALL Bedrock-hosted models, via the Converse API.

    The Converse API normalizes request/response shape across model families
    (Anthropic, Meta, Mistral, Amazon), so a single class covers Claude
    (predictor) + Llama + Mistral/Nova (verifiers) with no per-vendor code.

    boto3 is synchronous, so each call is off-loaded to a thread to preserve
    the orchestrator's asyncio fan-out.

    Auth: standard AWS credential chain (env vars / ~/.aws/credentials / IAM
    role) + region — NO api key in .env. Prompts and outputs stay inside your
    AWS account; AWS does not train on them. Set AWS_REGION (or pass region)
    to a region where your chosen models are enabled.

    Reasoning models (e.g. DeepSeek-R1) emit a separate `reasoningContent`
    block; we read only `text` blocks, so chain-of-thought is dropped before
    JSON extraction. (The four models we ship with all support Converse
    `system` + `temperature`.)
    """
    name = "bedrock"

    def __init__(self, model: str,
                 region: str | None = None,
                 max_tokens: int = 8000,
                 max_retries: int = 2,
                 temperature: float = 0.0):
        import boto3
        self.model = model
        self.region = (region
                       or os.environ.get("AWS_REGION")
                       or os.environ.get("AWS_DEFAULT_REGION")
                       or "us-east-1")
        self._client = boto3.client("bedrock-runtime", region_name=self.region)
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.temperature = temperature

    def _invoke_sync(self, user: str) -> tuple[str, int, int]:
        resp = self._client.converse(
            modelId=self.model,
            messages=[{"role": "user", "content": [{"text": user}]}],
            system=[{"text": "You are a precise reasoning component. "
                             "Return only valid JSON matching the requested schema."}],
            inferenceConfig={"maxTokens": self.max_tokens,
                             "temperature": self.temperature},
        )
        blocks = resp["output"]["message"]["content"]
        # Only answer text; ignore reasoningContent (R1-style chain-of-thought).
        text = "".join(b["text"] for b in blocks if "text" in b)
        usage = resp.get("usage", {})
        return text, int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0))

    async def arun(self, call_dir: str) -> TransportResult:
        prompt, inputs, schema = _read_brief(call_dir)
        user = _build_user_message(prompt, inputs, schema)
        t0 = time.time()
        last_err = None
        last_text = ""
        for attempt in range(self.max_retries + 1):
            try:
                text, in_tok, out_tok = await asyncio.to_thread(self._invoke_sync, user)
                last_text = text
                output = _extract_json(text)
                validate_schema(output, schema)
                _write_output(call_dir, output)
                elapsed = time.time() - t0
                _write_meta(call_dir, {
                    "ok": True, "transport": self.name, "model": self.model,
                    "elapsed_s": elapsed, "attempts": attempt + 1,
                    "input_tokens": in_tok, "output_tokens": out_tok,
                })
                return TransportResult(
                    True, output, text, elapsed,
                    transport=self.name, model=self.model,
                    input_tokens=in_tok, output_tokens=out_tok, attempts=attempt + 1,
                )
            except Exception as e:
                last_err = e
                log.warning("bedrock(%s) attempt %d failed: %s", self.model, attempt + 1, e)
                if attempt < self.max_retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        elapsed = time.time() - t0
        err_str = f"{type(last_err).__name__}: {last_err}"
        _write_error(call_dir, err_str, self.max_retries + 1, last_text)
        _write_meta(call_dir, {
            "ok": False, "transport": self.name, "model": self.model,
            "elapsed_s": elapsed, "attempts": self.max_retries + 1,
            "error": err_str,
        })
        return TransportResult(
            False, None, last_text, elapsed,
            error=err_str, transport=self.name, model=self.model,
            attempts=self.max_retries + 1,
        )


# ---------------------------------------------------------------------------
# Routing: subagent-style model id -> transport instance
# ---------------------------------------------------------------------------

# Bedrock model ids are REGION-SPECIFIC. The defaults below use US
# cross-region inference profiles; override per role via env to match your
# region's Bedrock console (Model access -> the id shown there). Switch
# verifier 2 from Mistral to Nova by setting COGNI_BEDROCK_VERIFY2_MODEL.
_BEDROCK_PREDICT_MODEL = os.environ.get(
    "COGNI_BEDROCK_PREDICT_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
_BEDROCK_VERIFY1_MODEL = os.environ.get(
    "COGNI_BEDROCK_VERIFY1_MODEL", "us.meta.llama3-3-70b-instruct-v1:0")
_BEDROCK_VERIFY2_MODEL = os.environ.get(
    # Alt (cheaper, native-Converse JSON): "us.amazon.nova-pro-v1:0"
    "COGNI_BEDROCK_VERIFY2_MODEL", "mistral.mistral-large-2407-v1:0")

_MODEL_ROUTING: dict[str, tuple[type, str]] = {
    # Anthropic
    "claude_opus_4_7":   (ClaudeTransport, "claude-opus-4-5"),
    "claude_sonnet_4_6": (ClaudeTransport, "claude-sonnet-4-5"),
    # OpenAI
    "gpt_5_4":           (OpenAITransport, "gpt-5"),
    "gpt_5_4_mini":      (OpenAITransport, "gpt-5-mini"),
    # Google
    "gemini_3_1_pro":    (GeminiTransport, "gemini-2.5-flash"),  # Pro requires paid; fall back to Flash
    "gemini_3_flash":    (GeminiTransport, "gemini-2.5-flash"),
    # AWS Bedrock — one Converse transport, model id resolved per role.
    # Activated by COGNI_BEDROCK=1 (see agent/llm/__init__.py).
    "bedrock_claude":    (BedrockTransport, _BEDROCK_PREDICT_MODEL),  # predictor/reflector
    "bedrock_llama":     (BedrockTransport, _BEDROCK_VERIFY1_MODEL),  # verifier 1
    "bedrock_mistral":   (BedrockTransport, _BEDROCK_VERIFY2_MODEL),  # verifier 2 (or Nova via env)
}


# Cache transport instances so we don't re-init clients per call.
_TRANSPORT_CACHE: dict[str, Transport] = {}


def transport_for_model(model_id: str) -> Transport | None:
    """Return a Transport for a subagent-style model id, or None if the
    model should stay on the subagent path. Instances are cached per
    (cls, vendor_model)."""
    spec = _MODEL_ROUTING.get(model_id)
    if not spec:
        return None
    cls, vendor_model = spec
    cache_key = f"{cls.__name__}:{vendor_model}"
    inst = _TRANSPORT_CACHE.get(cache_key)
    if inst is None:
        inst = cls(model=vendor_model)
        _TRANSPORT_CACHE[cache_key] = inst
    return inst


# ---------------------------------------------------------------------------
# Async fan-out helpers
# ---------------------------------------------------------------------------

async def run_briefs_concurrently(
    briefs: list[dict],
    *,
    concurrency: int = 8,
    on_progress=None,
) -> list[TransportResult]:
    """Fan out a batch of briefs across the right transports in parallel.

    Each brief must have:
      - 'name'   : a stable identifier (used for logs / progress)
      - 'model'  : a subagent-style model id ('claude_opus_4_7' etc.)
      - 'prompt' : absolute path to prompt.md (used to derive call_dir)

    Skips any brief whose call_dir already has output.json (idempotent).
    Returns one TransportResult per brief, in input order.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _one(idx: int, brief: dict) -> TransportResult:
        call_dir = os.path.dirname(brief["prompt"])
        out = os.path.join(call_dir, "output.json")
        if os.path.exists(out):
            if on_progress:
                on_progress(idx, brief, "cached", 0.0)
            return TransportResult(
                True, json.load(open(out)), "", 0.0,
                error=None, transport="cache", model=brief.get("model", ""),
            )
        tport = transport_for_model(brief["model"])
        if tport is None:
            if on_progress:
                on_progress(idx, brief, "no-route", 0.0)
            return TransportResult(
                False, None, "", 0.0,
                error=f"No transport for model {brief['model']}",
                transport="none", model=brief.get("model", ""),
            )
        async with sem:
            t0 = time.time()
            res = await tport.arun(call_dir)
            if on_progress:
                tag = "ok" if res.ok else "FAIL"
                on_progress(idx, brief, tag, time.time() - t0)
            return res

    return await asyncio.gather(*[_one(i, b) for i, b in enumerate(briefs)])

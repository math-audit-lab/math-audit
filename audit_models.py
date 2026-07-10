from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModelCapability:
    api_model_id: str
    display_name: str
    supported_reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str
    input_price_usd_per_1m: float
    cached_input_price_usd_per_1m: Optional[float]
    output_price_usd_per_1m: float
    recommended_default: bool = False
    guidance: str = ""


LEGACY_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")

MODEL_CAPABILITIES: dict[str, ModelCapability] = {
    "gpt-5.6-sol": ModelCapability(
        api_model_id="gpt-5.6-sol",
        display_name="GPT-5.6 Sol — recommended",
        supported_reasoning_efforts=("none", "low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="xhigh",
        input_price_usd_per_1m=5.00,
        cached_input_price_usd_per_1m=0.50,
        output_price_usd_per_1m=30.00,
        recommended_default=True,
        guidance="Recommended default for serious research-level mathematical audits.",
    ),
    "gpt-5.5": ModelCapability(
        api_model_id="gpt-5.5",
        display_name="GPT-5.5 — previous default",
        supported_reasoning_efforts=("none", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="xhigh",
        input_price_usd_per_1m=5.00,
        cached_input_price_usd_per_1m=0.50,
        output_price_usd_per_1m=30.00,
        guidance="Previous default; useful for comparison and compatibility.",
    ),
    "gpt-5.5-pro": ModelCapability(
        api_model_id="gpt-5.5-pro",
        display_name="GPT-5.5 Pro — expensive",
        supported_reasoning_efforts=("high",),
        default_reasoning_effort="high",
        input_price_usd_per_1m=30.00,
        cached_input_price_usd_per_1m=None,
        output_price_usd_per_1m=180.00,
        guidance="Much more expensive; not the default.",
    ),
    "gpt-5.4": ModelCapability(
        api_model_id="gpt-5.4",
        display_name="GPT-5.4",
        supported_reasoning_efforts=LEGACY_REASONING_EFFORTS,
        default_reasoning_effort="high",
        input_price_usd_per_1m=2.50,
        cached_input_price_usd_per_1m=0.25,
        output_price_usd_per_1m=15.00,
    ),
    "gpt-5.4-mini": ModelCapability(
        api_model_id="gpt-5.4-mini",
        display_name="GPT-5.4 Mini",
        supported_reasoning_efforts=LEGACY_REASONING_EFFORTS,
        default_reasoning_effort="high",
        input_price_usd_per_1m=0.25,
        cached_input_price_usd_per_1m=0.025,
        output_price_usd_per_1m=2.00,
    ),
    "gpt-5.2": ModelCapability(
        api_model_id="gpt-5.2",
        display_name="GPT-5.2",
        supported_reasoning_efforts=LEGACY_REASONING_EFFORTS,
        default_reasoning_effort="high",
        input_price_usd_per_1m=1.25,
        cached_input_price_usd_per_1m=0.125,
        output_price_usd_per_1m=10.00,
    ),
    "gpt-5.4-pro": ModelCapability(
        api_model_id="gpt-5.4-pro",
        display_name="GPT-5.4 Pro",
        supported_reasoning_efforts=("medium", "high", "xhigh"),
        default_reasoning_effort="high",
        input_price_usd_per_1m=30.00,
        cached_input_price_usd_per_1m=None,
        output_price_usd_per_1m=180.00,
    ),
}

MODEL_CHOICES = ("gpt-5.6-sol", "gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-5.4-mini", "gpt-5.2")
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = MODEL_CAPABILITIES[DEFAULT_MODEL].default_reasoning_effort

PRICING_USD_PER_1M: dict[str, dict[str, Optional[float]]] = {
    "gpt-5.6-sol": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.5-pro": {"input": 30.00, "cached_input": None, "output": 180.00},
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-pro": {"input": 30.00, "cached_input": None, "output": 180.00},
    "gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
    "gpt-5-nano": {"input": 0.05, "cached_input": 0.005, "output": 0.40},
}

LONG_CONTEXT_INPUT_TOKEN_THRESHOLD = 270_000
LONG_CONTEXT_PRICING_USD_PER_1M: dict[str, dict[str, Optional[float]]] = {
    "gpt-5.5": {"input": 10.00, "cached_input": 1.00, "output": 45.00},
    "gpt-5.5-pro": {"input": 60.00, "cached_input": None, "output": 270.00},
    "gpt-5.4": {"input": 5.00, "cached_input": 0.50, "output": 22.50},
    "gpt-5.4-pro": {"input": 60.00, "cached_input": None, "output": 270.00},
}

GPT56_SOL_CACHE_WRITE_LIMITATION = (
    "GPT-5.6 Sol cache-write pricing is not separately estimated unless the API usage payload "
    "exposes cache-write token counts reliably."
)

_DISPLAY_NAME_TO_MODEL = {capability.display_name.lower(): model_id for model_id, capability in MODEL_CAPABILITIES.items()}
_DISPLAY_NAME_TO_MODEL.update({model_id.lower(): model_id for model_id in MODEL_CAPABILITIES})


def canonical_model_id(model: Optional[str] = None) -> str:
    clean = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return _DISPLAY_NAME_TO_MODEL.get(clean.lower(), clean)


def model_family(model: Optional[str] = None) -> str:
    clean = canonical_model_id(model)
    known = set(MODEL_CAPABILITIES) | set(PRICING_USD_PER_1M) | set(LONG_CONTEXT_PRICING_USD_PER_1M)
    for candidate in sorted(known, key=len, reverse=True):
        if clean == candidate or clean.startswith(candidate + "-"):
            return candidate
    return clean


def model_choices() -> list[str]:
    return list(MODEL_CHOICES)


def model_display_name(model: Optional[str] = None) -> str:
    model_id = canonical_model_id(model)
    capability = MODEL_CAPABILITIES.get(model_id)
    return capability.display_name if capability else model_id


def model_display_choices() -> list[str]:
    return [model_display_name(model_id) for model_id in MODEL_CHOICES]


def model_guidance(model: Optional[str] = None) -> str:
    capability = MODEL_CAPABILITIES.get(model_family(model))
    return capability.guidance if capability else ""


def supported_reasoning_efforts_for_model(model: Optional[str] = None) -> list[str]:
    capability = MODEL_CAPABILITIES.get(model_family(model))
    if capability:
        return list(capability.supported_reasoning_efforts)
    return list(LEGACY_REASONING_EFFORTS)


def default_reasoning_effort_for_model(model: Optional[str] = None) -> str:
    capability = MODEL_CAPABILITIES.get(model_family(model))
    if capability:
        return capability.default_reasoning_effort
    return DEFAULT_REASONING_EFFORT


def normalize_model_and_reasoning_effort(
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> tuple[str, str]:
    clean_model = canonical_model_id(model)
    supported = supported_reasoning_efforts_for_model(clean_model)
    default_effort = default_reasoning_effort_for_model(clean_model)
    clean_effort = str(reasoning_effort or "").strip().lower()
    if clean_effort not in supported:
        clean_effort = default_effort
    return clean_model, clean_effort


def reasoning_effort_guidance_for_model(model: Optional[str] = None) -> dict[str, str]:
    if model_family(model) == "gpt-5.6-sol":
        return {
            "none": "fastest; generally unsuitable for deep mathematical auditing",
            "low": "low reasoning cost",
            "medium": "balanced",
            "high": "deeper analysis",
            "xhigh": "recommended default for research-level auditing",
            "max": "hardest quality-first audits; highest latency and token use",
        }
    return {}


def pricing_key_for_model(model: Optional[str] = None) -> str:
    return model_family(model)


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_REASONING_EFFORT",
    "GPT56_SOL_CACHE_WRITE_LIMITATION",
    "LEGACY_REASONING_EFFORTS",
    "LONG_CONTEXT_INPUT_TOKEN_THRESHOLD",
    "LONG_CONTEXT_PRICING_USD_PER_1M",
    "MODEL_CAPABILITIES",
    "MODEL_CHOICES",
    "ModelCapability",
    "PRICING_USD_PER_1M",
    "canonical_model_id",
    "default_reasoning_effort_for_model",
    "model_choices",
    "model_display_choices",
    "model_display_name",
    "model_family",
    "model_guidance",
    "normalize_model_and_reasoning_effort",
    "pricing_key_for_model",
    "reasoning_effort_guidance_for_model",
    "supported_reasoning_efforts_for_model",
]

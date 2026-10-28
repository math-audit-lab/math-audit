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

LEGACY_MODEL_CAPABILITIES: dict[str, ModelCapability] = {
    "gpt-5.6-sol": ModelCapability(
        api_model_id="gpt-5.6-sol",
        display_name="GPT-5.6 Sol — recommended",
        supported_reasoning_efforts=("none", "low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="xhigh",
        input_price_usd_per_1m=5.00,
        cached_input_price_usd_per_1m=0.50,
        output_price_usd_per_1m=30.00,
        recommended_default=False,
        guidance="Historical model; inspect saved audits or start a fresh audit with a current model.",
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

# Current API specifications verified 2026-10-28. Historical capabilities and rates
# below remain available for reading old audit records, never for new API work.
PRICING_VERIFIED_DATE = "2026-10-28"
CURRENT_MODEL_CAPABILITIES: dict[str, ModelCapability] = {
    "gpt-6.1-sol": ModelCapability(
        "gpt-6.1-sol", "GPT-6.1 Sol — Recommended",
        ("low", "medium", "high", "xhigh", "max"), "xhigh", 2.0, 0.10, 10.0,
        recommended_default=True,
        guidance="Recommended cost/capability balance for repeated chunk audits; not yet comparatively evaluated on MATH_AUDIT.",
    ),
    "gpt-6-astra": ModelCapability(
        "gpt-6-astra", "GPT-6 Astra — Highest quality",
        ("low", "medium", "high", "xhigh", "max"), "xhigh", 10.0, 1.0, 50.0,
        guidance="OpenAI's strongest model; highest-quality intended role, not a validated MATH_AUDIT ranking. Sol is the recommended default for repeated audits.",
    ),
    "gpt-6.1-terra": ModelCapability(
        "gpt-6.1-terra", "GPT-6.1 Terra — Lower cost",
        ("none", "low", "medium", "high", "xhigh", "max"), "high", 1.0, 0.05, 5.0,
        guidance="Lower-cost alternative; mathematical audit quality still requires comparative evaluation.",
    ),
    "gpt-6-luna": ModelCapability(
        "gpt-6-luna", "GPT-6 Luna — Exploratory / lowest cost",
        ("none", "low", "medium", "high", "xhigh", "max"), "medium", 0.10, 0.01, 0.50,
        guidance="Economical exploratory auditing; findings remain provisional until verification and adjudication.",
    ),
}
MODEL_CAPABILITIES = {**LEGACY_MODEL_CAPABILITIES, **CURRENT_MODEL_CAPABILITIES}
MODEL_CHOICES = tuple(CURRENT_MODEL_CAPABILITIES)
DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_REASONING_EFFORT = MODEL_CAPABILITIES[DEFAULT_MODEL].default_reasoning_effort
DEFAULT_REASONING_MODE = "standard"
REASONING_MODES = ("standard", "pro")
LEGACY_SESSION_WARNING = (
    "Legacy or unknown model session: original model, effort, prompts and costs are preserved. "
    "API continuation is disabled; start a separate new audit with a current model."
)

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

LEGACY_LONG_CONTEXT_INPUT_TOKEN_THRESHOLD = 270_000
LONG_CONTEXT_INPUT_TOKEN_THRESHOLD = 272_000
LONG_CONTEXT_PRICING_USD_PER_1M: dict[str, dict[str, Optional[float]]] = {
    "gpt-5.5": {"input": 10.00, "cached_input": 1.00, "output": 45.00},
    "gpt-5.5-pro": {"input": 60.00, "cached_input": None, "output": 270.00},
    "gpt-5.4": {"input": 5.00, "cached_input": 0.50, "output": 22.50},
    "gpt-5.4-pro": {"input": 60.00, "cached_input": None, "output": 270.00},
}

for _model_id, _cap in CURRENT_MODEL_CAPABILITIES.items():
    PRICING_USD_PER_1M[_model_id] = {
        "input": _cap.input_price_usd_per_1m,
        "cached_input": _cap.cached_input_price_usd_per_1m,
        "cache_write_30m": 1.25 * _cap.input_price_usd_per_1m,
        "cache_write_12h": 2.5 * _cap.input_price_usd_per_1m,
        "output": _cap.output_price_usd_per_1m,
    }
    LONG_CONTEXT_PRICING_USD_PER_1M[_model_id] = {
        key: value * (1.5 if key == "output" else 2.0)
        for key, value in PRICING_USD_PER_1M[_model_id].items()
        if value is not None
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
    if model_family(model) not in CURRENT_MODEL_CAPABILITIES:
        return LEGACY_SESSION_WARNING
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
    if model_family(model) in CURRENT_MODEL_CAPABILITIES or model_family(model) == "gpt-5.6-sol":
        return {
            "none": "fastest; generally unsuitable for deep mathematical auditing",
            "low": "low reasoning cost",
            "medium": "balanced",
            "high": "deeper analysis",
            "xhigh": "recommended default for research-level auditing",
            "max": "hardest quality-first audits; highest latency and token use",
        }
    return {}


def is_current_model(model: Optional[str]) -> bool:
    return bool(model) and canonical_model_id(model) in CURRENT_MODEL_CAPABILITIES


def validate_new_audit_settings(
    model: str, reasoning_effort: str, mode: str = DEFAULT_REASONING_MODE,
) -> tuple[str, str, str]:
    """Validate API settings without silently replacing any requested value.

    The GUI may normalize an explicit model selection before calling this helper.
    Stored historical settings must never pass through GUI normalization on load.
    """
    model_id = canonical_model_id(model)
    if not model or model_id not in CURRENT_MODEL_CAPABILITIES:
        raise ValueError(f"{LEGACY_SESSION_WARNING} Requested model: {model!r}.")
    capability = CURRENT_MODEL_CAPABILITIES[model_id]
    if reasoning_effort not in capability.supported_reasoning_efforts:
        raise ValueError(
            f"Unsupported reasoning effort {reasoning_effort!r} for {model_id}; "
            f"choose from {', '.join(capability.supported_reasoning_efforts)}."
        )
    if mode not in REASONING_MODES:
        raise ValueError(f"Unsupported reasoning mode {mode!r}; choose standard or pro.")
    return model_id, reasoning_effort, mode


def request_reasoning(
    model: str, reasoning_effort: str, mode: str = DEFAULT_REASONING_MODE,
) -> dict[str, str]:
    _, effort, selected_mode = validate_new_audit_settings(model, reasoning_effort, mode)
    return {"effort": effort, "mode": selected_mode}


def long_context_threshold_for_model(model: str) -> int:
    # Keep the frozen historical calculator's threshold for legacy comparisons.
    return (LONG_CONTEXT_INPUT_TOKEN_THRESHOLD if model_family(model) in CURRENT_MODEL_CAPABILITIES
            else LEGACY_LONG_CONTEXT_INPUT_TOKEN_THRESHOLD)


def pricing_key_for_model(model: Optional[str] = None) -> str:
    return model_family(model)


__all__ = [
    "CURRENT_MODEL_CAPABILITIES",
    "DEFAULT_REASONING_MODE",
    "LEGACY_MODEL_CAPABILITIES",
    "LEGACY_SESSION_WARNING",
    "PRICING_VERIFIED_DATE",
    "REASONING_MODES",
    "is_current_model",
    "long_context_threshold_for_model",
    "request_reasoning",
    "validate_new_audit_settings",
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

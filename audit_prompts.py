from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


PROMPT_PROFILES_PATH = Path(__file__).resolve().with_name("audit_prompt_profiles.json")

SHIPPED_AUDIT_SYSTEM_PROMPT = r"""You are a rigorous mathematical proof auditor.

You will receive one chunk of a mathematics paper at a time. The PySide6 GUI is the primary user-facing frontend for this audit system; the Jupyter notebook is secondary debug, maintenance, and experimentation tooling only. Your output must remain suitable for the GUI, saved JSON, Markdown reports, and LaTeX-generated reports.

STRICT FORMAT RULES
- When writing mathematical prose inside string fields, use inline math as $...$ and display math as $$...$$.
- Do not use \( ... \) or \[ ... \].
- Do not include Markdown headings, bullet markers, or fenced code blocks inside JSON string values unless a field explicitly expects structured list content.
- Do not include any LaTeX preamble.
- Prefer plain text references such as equation labels, theorem labels, page/chunk locations, or visible PDF numbering over raw \eqref outside math.
- If you provide a LaTeX patch, put only copy-pasteable LaTeX code into the latex_patch field.
- Keep prose readable to a mathematician reading the audit output in the GUI, saved JSON, Markdown reports, or LaTeX-generated reports.
- Outside the latex_patch field, avoid raw LaTeX structural commands such as \section, \subsection, \begin{itemize}, \item, \maketitle, or other document-level markup inside prose fields.

SEVERITY LABEL RULES
- Every issue must use exactly one severity label from this closed set:
  - low
  - medium
  - high
  - critical
- Do not invent alternative severity labels such as minor, major, severe, highest, or similar.
- Use:
  - low for minor issues with limited mathematical impact
  - medium for real issues that matter but are not central to the main claims
  - high for serious issues affecting correctness, rigor, dependencies, or important arguments
  - critical for issues that substantially undermine a central claim, theorem, or major conclusion

AUDIT POLICY
- Do not summarize casually.
- State the assumptions, notation, dependencies, and regime conditions needed for the chunk.
- Use ledger_updates to maintain compact running context for later chunks.
- In ledger_updates.assumptions, record only new or corrected notation, definitions, standing assumptions, and parameter regimes from the current chunk that future chunks may need.
- In ledger_updates.notes, record only new or corrected theorem/lemma dependencies, unresolved notation/reference ambiguities, and corrections or conflicts with earlier context.
- Do not repeat the entire previous ledger; ledger_updates should include only new or corrected information from the current chunk.
- Distinguish clearly between:
  - what the paper itself states,
  - what the audit can conclude from the chunk and saved context,
  - and what remains uncertain or needs further checking.
- Distinguish fully justified steps, plausible but insufficiently justified steps, and actual errors.
- Flag hidden assumptions, indexing problems, asymptotic slips, domain issues, dependency gaps, reference/numbering issues, and notation inconsistencies.
- If the chunk is too large for a reliable audit, set chunk_too_large=true and propose exact split points in chunk_split_suggestions.
- Prefer human-readable mathematical prose over raw LaTeX commands outside math mode.
- Focus on mathematical correctness, logical dependency, and the impact of any issue on later claims.
- Treat minor editorial or typographical matters separately from substantive mathematical concerns.
- Be robust to both TeX-aware and PDF-only audits. When TeX, AUX, or source labels are unavailable, avoid overclaiming reference precision; use approximate page/chunk locations or visible PDF numbering and say when a location or label is uncertain.
- Do not infer a theorem number, equation number, dependency, or source label unless the provided chunk/context supports it.
- If you provide local Python verification scripts, each python_checks item must include:
  - purpose: a short title for the check
  - description: a self-contained explanation of the mathematical claim being tested, the test strategy, and any sample parameters or cases used
  - expected_outcome: what output or condition should be interpreted as success
  - code: standalone runnable local Python source only, with no Markdown fences, JSON separators, or surrounding list/object delimiters
- Every verification script must print exactly one final machine-readable line beginning with MATH_AUDIT_VERIFICATION_RESULT_JSON= followed by a JSON object with schema_version=1, check_kind, outcome, summary, counterexamples, failed_cases, tested_range, target, and linked_issue_ids.
- Allowed outcome values are counterexample_found, claim_failed, no_counterexample_found, check_satisfied, diagnostic_only, inconclusive, and not_reported.
- A finite search with no failures must use no_counterexample_found and describe the tested scope; it must not claim to prove an unrestricted theorem.
- A script may exit with return code 0 after finding a counterexample because return code 0 means only that Python execution completed. Report the negative mathematical outcome in the sentinel.
- Write each verification description so that it still makes sense in the GUI and generated reports even if the reader never inspects the code itself."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_profiles() -> dict[str, Any]:
    return {
        "default_prompt": "",
        "model_overrides": {},
        "updated_at": None,
    }


def load_prompt_profiles(path: str | Path = PROMPT_PROFILES_PATH) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return _empty_profiles()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_profiles()
    if not isinstance(data, dict):
        return _empty_profiles()
    profiles = _empty_profiles()
    default_prompt = data.get("default_prompt")
    if isinstance(default_prompt, str):
        profiles["default_prompt"] = default_prompt
    overrides = data.get("model_overrides")
    if isinstance(overrides, dict):
        profiles["model_overrides"] = {
            str(model): str(prompt)
            for model, prompt in overrides.items()
            if str(model).strip() and isinstance(prompt, str)
        }
    profiles["updated_at"] = data.get("updated_at")
    return profiles


def save_prompt_profiles(profiles: dict[str, Any], path: str | Path = PROMPT_PROFILES_PATH) -> dict[str, Any]:
    path = Path(path)
    normalized = _empty_profiles()
    if isinstance(profiles, dict):
        default_prompt = profiles.get("default_prompt")
        if isinstance(default_prompt, str):
            normalized["default_prompt"] = default_prompt
        overrides = profiles.get("model_overrides")
        if isinstance(overrides, dict):
            normalized["model_overrides"] = {
                str(model): str(prompt)
                for model, prompt in overrides.items()
                if str(model).strip() and isinstance(prompt, str)
            }
    normalized["updated_at"] = _utc_now()
    path.write_text(json.dumps(normalized, indent=2, ensure_ascii=False), encoding="utf-8")
    return normalized


def effective_audit_system_prompt(model: Optional[str] = None) -> str:
    prompt, _source = effective_audit_system_prompt_with_source(model)
    return prompt


def effective_audit_system_prompt_with_source(model: Optional[str] = None) -> tuple[str, str]:
    profiles = load_prompt_profiles()
    clean_model = str(model or "").strip()
    overrides = profiles.get("model_overrides") or {}
    if clean_model and isinstance(overrides, dict):
        override = overrides.get(clean_model)
        if isinstance(override, str) and override.strip():
            return override, f"model_override:{clean_model}"
    default_prompt = profiles.get("default_prompt")
    if isinstance(default_prompt, str) and default_prompt.strip():
        return default_prompt, "custom_default"
    return SHIPPED_AUDIT_SYSTEM_PROMPT, "shipped_default"


def prompt_snapshot_metadata(prompt: str, source: Optional[str], model: Optional[str]) -> dict[str, Any]:
    text = str(prompt or "")
    return {
        "source": str(source or "unknown"),
        "model": str(model or ""),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "char_count": len(text),
        "snapshot_at": _utc_now(),
    }


__all__ = [
    "PROMPT_PROFILES_PATH",
    "SHIPPED_AUDIT_SYSTEM_PROMPT",
    "effective_audit_system_prompt",
    "effective_audit_system_prompt_with_source",
    "load_prompt_profiles",
    "prompt_snapshot_metadata",
    "save_prompt_profiles",
]

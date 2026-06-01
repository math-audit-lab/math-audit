from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from audit_chunking import load_pdf_pages, normalize_whitespace, strip_tex_comments
from audit_state import (
    format_duration,
    load_issues,
    load_json,
    load_ledger,
    load_manifest,
    load_session_from_pdf,
    load_status,
    load_usage,
    save_json,
    save_session,
    utc_now,
)
from audit_verification import (
    _load_verification_results,
    _truncate_text,
    _verification_summary_counts,
    load_verification_state,
)
from audit_runtime import (
    AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL,
    FRESH_CONTEXT_GENERIC_QUERY_TERMS,
    PDF_TEXT_ONLY_RETRY_NOTE,
    _coerce_audit_payload,
    _ensure_timing_state,
    _normalize_audit_context_mode,
    _normalize_python_check_entry,
    _normalize_reference_mention_style,
    _normalize_report_reference_style,
    _read_chunk_records,
    _repair_json_escape_artifacts,
    _contains_latex_unsupported_unicode,
    _report_math_delimiters_look_unsafe,
    _strip_unsafe_control_chars,
    _verification_inventory_warning,
    build_fresh_audit_context_for_chunk,
    build_verification_report as runtime_build_verification_report,
    format_list_for_markdown,
    normalize_math_delimiters,
    normalize_report_latex_unicode_math,
    repair_report_latex_math_command_artifacts,
    report_latex_paragraph,
    sanitize_latex_unsupported_unicode,
    sanitize_ascii_punctuation,
    _verbatim_block,
)


REFERENCE_MENTION_STYLES = {"auto", "compiled_pdf_numbers", "source_labels"}
_PDF_REFERENCE_PAGE_CACHE: dict[str, list[str]] = {}
_VISIBLE_NUMBERED_OBJECT_RE = re.compile(
    r"\b(Theorem|Lemma|Proposition|Corollary|Definition|Remark|Section|Figure|Table)\s+(\d+(?:\.\d+)*)\b"
)
_EQUATION_NUMBER_ONLY_RE = re.compile(r"^\((\d+(?:\.\d+)*)\)$")
_EQUATION_NUMBER_LINE_END_RE = re.compile(r"\((\d+(?:\.\d+)*)\)\s*$")
CONTINUOUS_RUNNING_CONTEXT_MAX_CHARS = 3200
CONTINUOUS_RUNNING_CONTEXT_PROFILE = "continuous_compact"
FRESH_CONTEXT_RETRIEVAL_PROFILE = "fresh_context_retrieval"
_CONTINUOUS_CONTEXT_RECENT_ISSUE_WINDOW = 4
_CONTINUOUS_CONTEXT_ISSUE_MIN_SCORE = 2

_TYPO_POSITIVE_TAGS = {
    "typo",
    "editorial",
    "copyedit",
    "copy-edit",
    "copyediting",
    "grammar",
    "spelling",
    "punctuation",
    "cross-reference",
    "placeholder",
}
_TYPO_NEGATIVE_TAGS = {
    "mathematical-correctness",
    "proof-gap",
    "uniformity",
    "domain",
    "convergence",
    "asymptotics",
    "indexing",
    "hidden-assumption",
    "well-posedness",
    "omitted-justification",
    "analyticity",
    "formal-power-series",
    "definition",
    "range",
    "parameter-range",
    "variance",
    "boundary",
    "quantifiers",
    "big-o",
    "exponents",
    "asymptotic-expansion",
}
_TYPO_KEYWORDS = (
    "typo",
    "typographical",
    "spelling",
    "grammar",
    "copyedit",
    "copy editing",
    "copyediting",
    "misprint",
    "misspell",
    "punctuation",
    "placeholder",
    "placeholders",
    "incomplete marker",
)
_TYPO_LITERAL_FRAGMENT_RE = re.compile(r"`([^`\n]{2,120})`")

_NOTABLE_PROOF_REFERENCE_SECTION_TITLE = "Notable incorrect or circular references"
_NOTABLE_PROOF_REFERENCE_MAX_ISSUES = 10
_NOTABLE_PROOF_REFERENCE_TAGS = {
    "circular-citation",
    "identity-being-proved",
    "incorrect-reference",
    "mislabeling",
    "mislabeled",
    "mislabelled",
    "reference-error",
    "self-citation",
    "wrong-reference",
}
_NOTABLE_PROOF_REFERENCE_STRONG_KEYWORDS = (
    "cites the identity being proved",
    "citing the identity being proved",
    "cite the identity being proved",
    "identity being proved",
    "equation being proved",
    "formula being proved",
    "result being proved",
    "currently being proved",
    "circular citation",
    "circular reference",
    "self-citation",
    "self citation",
    "incorrect cross-reference",
    "incorrect cross reference",
    "missing cross-reference",
    "missing cross reference",
    "wrong cross-reference",
    "wrong cross reference",
    "wrong reference",
    "incorrect reference",
    "misleading reference",
    "mislabeled reference",
    "mislabelled reference",
    "reference number mismatch",
    "reference label mismatch",
    "label mismatch",
    "wrong equation",
    "wrong formula",
    "wrong theorem",
    "wrong lemma",
    "wrong proposition",
    "wrong definition",
    "wrong section",
)
_NOTABLE_PROOF_REFERENCE_TARGET_KEYWORDS = (
    "citation",
    "cite",
    "cites",
    "citing",
    "cross-reference",
    "cross reference",
    "definition reference",
    "equation reference",
    "formula reference",
    "identity reference",
    "lemma reference",
    "proposition reference",
    "reference",
    "section reference",
    "theorem reference",
)
_NOTABLE_PROOF_REFERENCE_ERROR_KEYWORDS = (
    "being proved",
    "circular",
    "incorrect",
    "misleading",
    "mislabeling",
    "mislabeled",
    "mislabelled",
    "mismatch",
    "prevents checking",
    "self-citation",
    "self citation",
    "wrong",
)

SAFE_REPORT_PACKAGES = {
    "tikz",
    "xspace",
    "xparse",
    "amsmath",
    "amssymb",
    "mathtools",
    "amsthm",
    "bm",
    "bbm",
    "mathrsfs",
    "stmaryrd",
    "graphicx",
    "etoolbox",
    "array",
    "calc",
    "ifthen",
    "xcolor",
    "dsfont",
    "yhmath",
    "accents",
}
BASE_REPORT_PACKAGES = {
    "geometry",
    "fontenc",
    "inputenc",
    "lmodern",
    "microtype",
    "amsmath",
    "amssymb",
    "mathtools",
    "hyperref",
    "enumitem",
    "longtable",
    "booktabs",
    "xcolor",
    "fancyvrb",
}
_DANGEROUS_MATH_COMMAND_RE = re.compile(
    r"\\(?:"
    r"usepackage|documentclass|begin|end|input|include|newcommand|renewcommand|providecommand|def|"
    r"write18|openout|catcode|usetikzlibrary|ref|eqref|autoref|cref|Cref|cite|label|require|"
    r"tag|notag|nonumber|numberwithin|"
    r"section|subsection|subsubsection|paragraph|subparagraph|appendix|chapter|part|"
    r"maketitle|title|author|date|thanks|email|address|keywords|subjclass|abstract|and|with|eqand|eqwith|"
    r"text|mbox|"
    r"caption|includegraphics|graphicspath|bibliography|bibliographystyle|"
    r"MakeUppercase|uppercasenonmath|textcolor|colorbox|fcolorbox|"
    r"ltr|cl|lcl|fr|lfr|lpa|Lpa|LLpa|llpa|dd|ve|tr|eqtext|lbb|lbe|leb|lee"
    r")\b"
)
_ARGUMENT_TAKING_REPORT_MATH_MACRO_RE = re.compile(
    r"\\(?:Stirling|stirling)\b(?!\s*\{[^{}]*\}\s*\{[^{}]*\})"
)


def _repair_report_escape_artifacts(text: str) -> str:
    text = _repair_json_escape_artifacts(text)
    return text.replace("\r", r"\r").replace("\t", " ")


def _report_math_text_looks_unsafe(text: str) -> bool:
    text = "" if text is None else str(text)
    normalized = _normalize_report_latex_unicode_math(text)
    return bool(
        _DANGEROUS_MATH_COMMAND_RE.search(text)
        or _ARGUMENT_TAKING_REPORT_MATH_MACRO_RE.search(text)
        or text.count(r"\left") != text.count(r"\right")
        or _report_math_delimiters_look_unsafe(text)
        or _contains_latex_unsupported_unicode(normalized)
    )


def _report_latex_text_looks_globally_unsafe(text: str) -> bool:
    """Detect paragraph-level hazards before math spans are rendered individually."""
    text = "" if text is None else str(text)
    return bool(
        _DANGEROUS_MATH_COMMAND_RE.search(text)
        or _ARGUMENT_TAKING_REPORT_MATH_MACRO_RE.search(text)
        or text.count(r"\left") != text.count(r"\right")
        or _report_math_delimiters_look_unsafe(text)
    )


def _dedupe_preserve_order(seq: list[str]) -> list[str]:
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _extract_chunk_labels(text: str) -> list[str]:
    text = text or ""
    patterns = [
        r"\\label\{([^}]+)\}",
        r"\\(?:eqref|ref|autoref|cref|Cref)\{([^}]+)\}",
    ]
    labels: list[str] = []
    for pat in patterns:
        labels.extend(re.findall(pat, text))
    return _dedupe_preserve_order([x.strip() for x in labels if x and x.strip()])


def _infer_kind_from_label(label: str) -> str:
    lab = (label or "").lower()
    if lab.startswith(("eq:", "equation:", "equ:")):
        return "equation"
    if lab.startswith(("thm:", "theorem:")):
        return "theorem"
    if lab.startswith(("lem:", "lemma:")):
        return "lemma"
    if lab.startswith(("prop:", "proposition:")):
        return "proposition"
    if lab.startswith(("cor:", "corollary:")):
        return "corollary"
    if lab.startswith(("sec:", "section:")):
        return "section"
    if lab.startswith(("fig:", "figure:")):
        return "figure"
    if lab.startswith(("tab:", "table:")):
        return "table"
    return "item"


def _display_for_kind(kind: str, num: str, label: str) -> str:
    k = (kind or "").strip().lower() or _infer_kind_from_label(label)
    n = (num or "").strip()
    if not n:
        return ""
    if k == "equation":
        return f"equation ({n})"
    if k in {"theorem", "lemma", "proposition", "corollary", "section", "figure", "table"}:
        return f"{k.title()} {n}"
    return n


def _aux_read_braced_group(text: str, start: int) -> tuple[str, int]:
    if start >= len(text) or text[start] != "{":
        raise ValueError("Expected '{' while parsing AUX.")
    depth = 0
    chars = []
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "{":
            if depth > 0:
                chars.append(ch)
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced braces while parsing AUX.")
            if depth == 0:
                return "".join(chars), i + 1
            chars.append(ch)
        else:
            chars.append(ch)
        i += 1
    raise ValueError("Unterminated brace group while parsing AUX.")


def _aux_top_level_groups(text: str) -> list[str]:
    groups = []
    i = 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text) or text[i] != "{":
            break
        group, i = _aux_read_braced_group(text, i)
        groups.append(group)
    return groups


def _infer_kind_from_aux_anchor(anchor: str, label: str) -> str:
    prefix = (anchor or "").strip().split(".", 1)[0].lower()
    aliases = {
        "equation": "equation",
        "section": "section",
        "subsection": "section",
        "subsubsection": "section",
        "chapter": "section",
        "appendix": "section",
        "theorem": "theorem",
        "thm": "theorem",
        "lemma": "lemma",
        "lem": "lemma",
        "proposition": "proposition",
        "prop": "proposition",
        "corollary": "corollary",
        "cor": "corollary",
        "figure": "figure",
        "fig": "figure",
        "table": "table",
        "tab": "table",
    }
    return aliases.get(prefix, _infer_kind_from_label(label))


def _load_aux_label_map(aux_path: str | Path) -> dict[str, dict[str, str]]:
    aux_path = Path(aux_path)
    try:
        text = aux_path.read_text(encoding="utf-8")
    except Exception:
        text = aux_path.read_text(encoding="latin-1")

    label_map: dict[str, dict[str, str]] = {}
    needle = r"\newlabel"
    i = 0
    while True:
        idx = text.find(needle, i)
        if idx < 0:
            break
        j = idx + len(needle)
        while j < len(text) and text[j].isspace():
            j += 1
        if j >= len(text) or text[j] != "{":
            i = j
            continue
        label, j = _aux_read_braced_group(text, j)
        while j < len(text) and text[j].isspace():
            j += 1
        if j >= len(text) or text[j] != "{":
            i = j
            continue
        payload, j = _aux_read_braced_group(text, j)
        i = j

        label = (label or "").strip()
        if not label or "@cref" in label.lower():
            continue

        groups = _aux_top_level_groups(payload)
        if not groups:
            continue
        number = (groups[0] or "").strip()
        page = (groups[1] or "").strip() if len(groups) > 1 else ""
        anchor = (groups[3] or "").strip() if len(groups) > 3 else ""
        kind = _infer_kind_from_aux_anchor(anchor, label)
        entry = {
            "number": number,
            "kind": kind,
            "display": _display_for_kind(kind, number, label),
        }
        if page:
            entry["page"] = page
        if anchor:
            entry["anchor"] = anchor
        label_map[label] = entry
    return label_map


def _load_pdf_pages_for_reference_hints(pdf_path: str | Path) -> list[str]:
    key = str(Path(pdf_path).expanduser().resolve())
    pages = _PDF_REFERENCE_PAGE_CACHE.get(key)
    if pages is None:
        pages = load_pdf_pages(key)
        _PDF_REFERENCE_PAGE_CACHE[key] = pages
    return pages


def _extract_visible_pdf_reference_hints_from_pages(
    page_texts: list[str],
    start_page: int,
    max_items: int = 24,
) -> list[str]:
    rows: list[str] = []
    seen: set[tuple[str, str, int]] = set()
    for offset, raw_page in enumerate(page_texts, start=0):
        page_no = start_page + offset
        page_text = raw_page or ""
        for m in _VISIBLE_NUMBERED_OBJECT_RE.finditer(page_text):
            kind = m.group(1).strip().lower()
            number = m.group(2).strip()
            display = _display_for_kind(kind, number, f"{kind}:{number}") or f"{m.group(1)} {number}"
            key = (kind, number, page_no)
            if key in seen:
                continue
            seen.add(key)
            rows.append(f"- page {page_no}: visible {display}")
            if len(rows) >= max_items:
                return rows
        for line in page_text.splitlines():
            line = (line or "").strip()
            if not line:
                continue
            m = _EQUATION_NUMBER_ONLY_RE.fullmatch(line)
            if m:
                number = m.group(1)
                key = ("equation", number, page_no)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(f"- page {page_no}: standalone displayed equation number equation ({number})")
                if len(rows) >= max_items:
                    return rows
                continue
            m = _EQUATION_NUMBER_LINE_END_RE.search(line)
            if not m:
                continue
            number = m.group(1)
            key = ("equation", number, page_no)
            if key in seen:
                continue
            seen.add(key)
            snippet = normalize_whitespace(line[:m.start()].strip())
            if snippet:
                rows.append(
                    f"- page {page_no}: line ending with equation ({number}) near '{_truncate_text(snippet, limit=90)}'"
                )
            else:
                rows.append(f"- page {page_no}: line ending with equation ({number})")
            if len(rows) >= max_items:
                return rows
    return rows


def _pdf_reference_context_for_chunk(session: dict[str, Any], chunk: dict[str, Any], max_items: int = 24) -> str:
    try:
        pages = _load_pdf_pages_for_reference_hints(session["pdf_path"])
    except Exception:
        return "No reliable visible numbering hints were extracted from the PDF for this chunk."
    page_start = max(1, int(chunk.get("page_start") or 1))
    page_end = max(page_start, int(chunk.get("page_end") or page_start))
    page_texts = pages[page_start - 1 : page_end]
    rows = _extract_visible_pdf_reference_hints_from_pages(page_texts, start_page=page_start, max_items=max_items)
    if not rows:
        return "No reliable visible numbering hints were extracted from the PDF pages for this chunk."
    return "\n".join(rows)


def _reference_label_rows(labels: list[str], label_map: dict[str, dict[str, str]]) -> list[str]:
    rows = []
    for lab in labels:
        info = label_map.get(lab) or {}
        kind = (info.get("kind") or _infer_kind_from_label(lab)).strip() or "item"
        rows.append(f"- {lab} -> cite as 'the {kind} labeled {lab}'")
    return rows


def _reference_number_rows(labels: list[str], label_map: dict[str, dict[str, str]]) -> list[str]:
    rows = []
    for lab in labels:
        info = label_map.get(lab) or {}
        disp = (info.get("display") or "").strip()
        kind = (info.get("kind") or _infer_kind_from_label(lab)).strip() or "item"
        num = (info.get("number") or "").strip()
        if disp and num:
            rows.append(f"- {lab} -> {disp}")
        elif disp:
            rows.append(f"- {lab} -> {disp} (compiled number unavailable; cite by label if needed)")
        else:
            rows.append(f"- {lab} -> compiled number unavailable; cite as 'the {kind} labeled {lab}'")
    return rows


def ensure_reference_map(session: dict[str, Any]) -> dict[str, Any]:
    root = Path(session["workdir"])
    ref_path = root / "state" / "reference_map.json"
    tex_path = session.get("tex_path")
    aux_path = Path(tex_path).with_suffix(".aux") if tex_path else None
    aux_exists = bool(aux_path and aux_path.exists())

    cached: dict[str, Any] = {}
    if ref_path.exists():
        try:
            cached_obj = load_json(ref_path)
            if isinstance(cached_obj, dict):
                cached = dict(cached_obj)
        except Exception as e:
            cached = {"warning": f"Could not read cached reference_map.json: {e}"}

    if aux_exists and aux_path is not None:
        try:
            label_map = _load_aux_label_map(aux_path)
            ref_state = {
                "label_map": label_map,
                "source_aux_path": str(aux_path),
                "map_source": "aux" if label_map else "aux_empty",
                "updated_at": utc_now(),
            }
            if not label_map:
                ref_state["warning"] = f"AUX file {aux_path.name} was parsed but no labels were recovered."
            save_json(ref_path, ref_state)
            return ref_state
        except Exception as e:
            cached_map = cached.get("label_map") if isinstance(cached.get("label_map"), dict) else {}
            ref_state = {
                "label_map": cached_map or {},
                "source_aux_path": str(aux_path),
                "map_source": "cached_after_aux_error" if cached_map else "aux_error",
                "updated_at": utc_now(),
                "warning": (
                    f"Failed to parse AUX reference map from {aux_path.name}: {e}. "
                    + ("Using cached reference map fallback." if cached_map else "No cached reference map fallback is available.")
                ),
            }
            save_json(ref_path, ref_state)
            return ref_state

    if isinstance(cached.get("label_map"), dict):
        cached.setdefault(
            "source_aux_path",
            str(aux_path) if aux_path and aux_path.exists() else cached.get("source_aux_path"),
        )
        cached.setdefault("map_source", "cached" if cached.get("label_map") else "none")
        cached["updated_at"] = utc_now()
        save_json(ref_path, cached)
        return cached

    ref_state = {
        "label_map": {},
        "source_aux_path": str(aux_path) if aux_path and aux_path.exists() else None,
        "map_source": "none",
        "updated_at": utc_now(),
    }
    save_json(ref_path, ref_state)
    return ref_state


def _reference_map_has_valid_aux_numbers(ref_state: Any) -> bool:
    if not isinstance(ref_state, dict):
        return False
    label_map = ref_state.get("label_map")
    if not isinstance(label_map, dict) or not label_map:
        return False
    map_source = str(ref_state.get("map_source") or "").strip().lower()
    source_aux_path = ref_state.get("source_aux_path")
    return map_source == "aux" or (not map_source and bool(source_aux_path))


def _effective_reference_mention_style(
    session: dict[str, Any],
    ref_state: Optional[dict[str, Any]] = None,
) -> str:
    style = _normalize_reference_mention_style(session.get("reference_mention_style", "auto"))
    if style != "auto":
        return style
    ref_state = ref_state if isinstance(ref_state, dict) else ensure_reference_map(session)
    if _reference_map_has_valid_aux_numbers(ref_state):
        return "compiled_pdf_numbers"
    return "auto"


def _reference_prompt_status_note(ref_state: dict[str, Any]) -> str:
    label_map = ref_state.get("label_map", {}) if isinstance(ref_state, dict) else {}
    map_source = str(ref_state.get("map_source") or "none") if isinstance(ref_state, dict) else "none"
    warning = normalize_whitespace(ref_state.get("warning", "")) if isinstance(ref_state, dict) else ""
    if _reference_map_has_valid_aux_numbers(ref_state):
        count = len(label_map)
        return (
            "Authoritative compiled numbering is available from the paper's AUX file "
            f"({count} labels recovered). When the guidance below maps a label to a compiled number, "
            "use that exact compiled PDF number and ignore any different local number printed in the pasted TeX."
        )
    if label_map:
        note = (
            "A freshly parsed AUX numbering map is not available for this run. "
            f"Reference map source: {map_source}. Use compiled numbers only when the guidance below explicitly provides them; "
            "otherwise cite by label or use descriptive page-local wording."
        )
    else:
        note = (
            "No valid AUX-derived compiled numbering map is available for this run. "
            f"Reference map source: {map_source}. Do not copy source-local equation/theorem/section numbers from the pasted TeX; "
            "cite by label when available, otherwise use descriptive page-local wording."
        )
    if warning:
        note += f" Note: {warning}"
    return note


def _reference_context_for_chunk_strict(
    session: dict[str, Any],
    chunk: dict[str, Any],
    max_items: int = 80,
    ref_state: Optional[dict[str, Any]] = None,
) -> str:
    ref_state = ref_state if isinstance(ref_state, dict) else ensure_reference_map(session)
    style = _effective_reference_mention_style(session, ref_state=ref_state)
    label_map = ref_state.get("label_map", {}) if isinstance(ref_state, dict) else {}
    labels = _extract_chunk_labels(chunk.get("chunk_text", ""))
    label_rows = _reference_label_rows(labels, label_map) if labels else []
    numbered_rows = _reference_number_rows(labels, label_map) if labels else []
    pdf_context = _pdf_reference_context_for_chunk(session, chunk, max_items=max(8, min(24, max_items // 3)))
    pdf_available = not pdf_context.startswith("No reliable visible numbering hints")

    if style == "source_labels":
        if label_rows:
            rows = label_rows[:max_items]
            if pdf_available:
                rows.extend(
                    [
                        "",
                        "Visible PDF numbering hints for fallback only (use only when a source label is unavailable for the object):",
                        pdf_context,
                    ]
                )
            return "\n".join(rows).strip()
        if pdf_available:
            return (
                "No source labels were recovered for this chunk. Use the visible PDF numbering hints below when they clearly match the object; "
                "otherwise use descriptive page-local wording without inventing labels.\n" + pdf_context
            )
        return "No source labels or reliable visible PDF numbering hints were recovered for this chunk."

    if label_map and labels:
        return "\n".join(numbered_rows[:max_items]).strip()

    if style == "compiled_pdf_numbers":
        sections = []
        if pdf_available:
            sections.append(
                "Visible PDF numbering hints for this chunk (heuristic; use only when they clearly match the current object):\n"
                + pdf_context
            )
        if label_rows:
            sections.append(
                "Source labels recovered for this chunk. Use them only if the compiled/PDF-visible number remains unclear:\n"
                + "\n".join(label_rows[:max_items])
            )
        if sections:
            return "\n\n".join(sections).strip()
        return (
            "No AUX-derived numbering map, source labels, or reliable visible PDF numbering hints were recovered for this chunk. "
            "Do not invent numbers; use descriptive page-local wording instead."
        )

    if label_rows and pdf_available:
        return (
            "No AUX-derived compiled numbering was recovered for this chunk.\n\n"
            "Source labels recovered for this chunk:\n"
            + "\n".join(label_rows[:max_items])
            + "\n\nVisible PDF numbering hints (use them when they clearly identify the same object):\n"
            + pdf_context
        )
    if label_rows:
        return "No AUX-derived compiled numbering was recovered for this chunk.\n" + "\n".join(label_rows[:max_items])
    if pdf_available:
        return (
            "No explicit source labels were recovered for this chunk. Use the visible PDF numbering hints below when they clearly match the object; "
            "otherwise use descriptive page-local wording.\n" + pdf_context
        )
    return "No explicit source labels or reliable visible PDF numbering hints were recovered for this chunk."


def _reference_prompt_rule_for_style(style: str) -> str:
    style = _normalize_reference_mention_style(style)
    if style == "compiled_pdf_numbers":
        return (
            "REFERENCE STYLE FOR THIS RUN: compiled_pdf_numbers.\n"
            "When the guidance below maps a label to a compiled number, write that exact compiled PDF number in prose.\n"
            "Never copy a different source-local number from the pasted TeX. If the compiled/PDF guidance is ambiguous, do not invent a number; prefer descriptive page-local wording, and use a source label only when that is the only reliable identifier available."
        )
    if style == "source_labels":
        return (
            "REFERENCE STYLE FOR THIS RUN: source_labels.\n"
            "When a source label is available for an object in this chunk, cite it by label, for example 'the equation labeled eq:foo'.\n"
            "Do not replace a recovered label with a guessed local number from the pasted TeX. If no source label is available for the object (for example in PDF-only chunking), use visible PDF numbering when clear; otherwise use descriptive page-local wording."
        )
    return (
        "REFERENCE STYLE FOR THIS RUN: auto.\n"
        "Prefer compiled PDF numbering whenever the reference guidance below confirms it, and treat AUX-derived mappings as authoritative when present.\n"
        "If only source labels are available for an object in this chunk, cite by label.\n"
        "If neither a reliable number nor a source label is available, use descriptive page-local wording rather than inventing a reference."
    )


def _running_context_items(items: Any, limit: int, item_limit: int = 260) -> list[str]:
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for item in items:
        text = normalize_whitespace(str(item or ""))
        if not text:
            continue
        out.append(_truncate_text(text, limit=item_limit))
        if len(out) >= limit:
            break
    return out


def _chunk_index_value(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _load_record_audit_for_running_context(rec: dict[str, Any]) -> dict[str, Any]:
    path_text = str(rec.get("structured_response_path") or "").strip()
    if not path_text:
        return {}
    try:
        return _coerce_audit_payload(load_json(path_text))
    except Exception:
        return {}


def _running_context_query_terms(chunk: dict[str, Any]) -> set[str]:
    text = " ".join(str(chunk.get(key) or "") for key in ("chunk_text", "label", "boundary"))
    terms = {item.lower() for item in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text)}
    terms.update(item.lower() for item in re.findall(r"\\[A-Za-z]+", text))
    return {
        term
        for term in terms
        if term.lstrip("\\").lower() not in FRESH_CONTEXT_GENERIC_QUERY_TERMS
    }


def _running_context_issue_score(issue: dict[str, Any], query_terms: set[str]) -> int:
    haystack = " ".join(
        str(issue.get(key) or "")
        for key in ("issue_id", "severity", "title", "location", "description", "evidence", "proposed_fix", "chunk_id")
    ).lower()
    return sum(1 for term in query_terms if term and term in haystack)


_LATEX_COMMAND_NAME_RE = re.compile(r"(?<!\\)\\([A-Za-z@]+)\b")
_TEX_MACRO_GLOSSARY_UNSAFE_TOKENS = (
    r"\write18",
    r"\openout",
    r"\input",
    r"\include",
    r"\catcode",
    r"\read",
    r"\documentclass",
    r"\usepackage",
    r"\graphicspath",
    r"\bibliography",
    r"\bibliographystyle",
    r"\begin",
    r"\end",
    r"\newtheorem",
    r"\hypersetup",
    r"\makeatletter",
    r"\makeatother",
    r"\maketitle",
    r"\author",
    r"\title",
    r"\date",
    r"\section",
    r"\subsection",
    "tikzpicture",
)


def _latex_command_names(text: str) -> set[str]:
    return set(_LATEX_COMMAND_NAME_RE.findall(str(text or "")))


def _macro_name_from_definition_start(line: str) -> Optional[str]:
    patterns = [
        r"^\s*\\def\s*\\([A-Za-z@]+)\b",
        r"^\s*\\(?:newcommand|renewcommand|providecommand|DeclareRobustCommand)\*?\s*(?:\{\\([A-Za-z@]+)\}|\\([A-Za-z@]+))",
        r"^\s*\\DeclareMathOperator\*?\s*(?:\{\\([A-Za-z@]+)\}|\\([A-Za-z@]+))",
    ]
    for pattern in patterns:
        match = re.match(pattern, line)
        if match:
            return next((group for group in match.groups() if group), None)
    return None


def _tex_macro_definition_is_safe(block: str, max_definition_chars: int = 700) -> bool:
    text = str(block or "").strip()
    if not text or len(text) > max_definition_chars:
        return False
    return not any(token in text for token in _TEX_MACRO_GLOSSARY_UNSAFE_TOKENS)


def _extract_tex_macro_definitions(tex_path: str | Path | None) -> list[tuple[str, str]]:
    if not tex_path:
        return []
    path = Path(tex_path)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        try:
            text = path.read_text(encoding="latin-1")
        except Exception:
            return []

    preamble = strip_tex_comments(text.split(r"\begin{document}", 1)[0])
    definitions: list[tuple[str, str]] = []
    seen: set[str] = set()
    collecting = False
    current: list[str] = []
    current_name: Optional[str] = None
    balance = 0

    def flush_current() -> None:
        nonlocal collecting, current, current_name, balance
        if current_name and current:
            block = normalize_whitespace("\n".join(current))
            if current_name not in seen and _tex_macro_definition_is_safe(block):
                definitions.append((current_name, block))
                seen.add(current_name)
        collecting = False
        current = []
        current_name = None
        balance = 0

    for line in preamble.splitlines():
        if not collecting:
            name = _macro_name_from_definition_start(line)
            if not name:
                continue
            collecting = True
            current_name = name
            current = [line]
            balance = line.count("{") - line.count("}")
            if balance <= 0:
                flush_current()
            continue

        current.append(line)
        balance += line.count("{") - line.count("}")
        if balance <= 0:
            flush_current()

    if collecting:
        flush_current()
    return definitions


def _build_tex_macro_glossary_for_chunk(
    session: dict[str, Any],
    chunk: dict[str, Any],
    context_text: str = "",
    max_chars: int = 4000,
) -> str:
    source_kind = str(chunk.get("source_kind") or "").lower()
    if not source_kind.startswith("tex"):
        return ""

    definitions = _extract_tex_macro_definitions(session.get("tex_path"))
    if not definitions:
        return ""

    used_names = _latex_command_names(str(chunk.get("chunk_text") or "") + "\n" + str(context_text or ""))
    selected = [(name, definition) for name, definition in definitions if name in used_names]
    if not selected:
        return ""

    lines = [
        "Paper macro glossary for this chunk:",
        "Use this only as source-syntax aid, not as independent mathematical truth.",
    ]
    omitted = False
    for name, definition in selected:
        line = f"- \\{name}: {definition}"
        candidate = "\n".join(lines + [line, "End paper macro glossary."])
        if len(candidate) > max_chars:
            omitted = True
            break
        lines.append(line)
    if omitted:
        lines.append("- Additional relevant macro definitions omitted by glossary size cap.")
    lines.append("End paper macro glossary.")
    return _truncate_text(_strip_unsafe_control_chars("\n".join(lines).strip()), limit=max_chars)


def _build_running_audit_context_for_chunk(
    session: dict[str, Any],
    chunk: dict[str, Any],
    max_chars: int = CONTINUOUS_RUNNING_CONTEXT_MAX_CHARS,
    profile: str = CONTINUOUS_RUNNING_CONTEXT_PROFILE,
) -> str:
    """Build a compact saved-state context block for the next chunk prompt."""

    compact = profile == CONTINUOUS_RUNNING_CONTEXT_PROFILE
    assumption_limit = 5 if compact else 8
    assumption_item_limit = 180 if compact else 260
    note_limit = 3 if compact else 6
    note_item_limit = 180 if compact else 260
    recent_record_limit = 3 if compact else 4
    recent_assumption_limit = 1 if compact else 2
    recent_assumption_item_limit = 140 if compact else 170
    recent_verified_item_limit = 150 if compact else 180
    next_hint_limit = 140 if compact else 180
    priority_issue_limit = 3 if compact else 6

    lines = [
        "Running audit context from earlier chunks:",
        "Use this compact saved-state context conservatively; the current chunk text below is authoritative.",
    ]
    has_context = False

    source_kind = str(chunk.get("source_kind") or "").lower()
    if not session.get("tex_path") or source_kind.startswith("pdf"):
        lines.append(
            "- PDF/reference precision note: TeX, AUX, or source labels may be unavailable or incomplete. "
            "Do not overclaim exact theorem/equation labels; use visible PDF numbering or approximate page/chunk locations when needed."
        )

    try:
        ledger = load_ledger(session)
    except Exception:
        ledger = {}

    assumptions = _running_context_items(
        (ledger or {}).get("assumptions"),
        limit=assumption_limit,
        item_limit=assumption_item_limit,
    )
    notes = _running_context_items((ledger or {}).get("notes"), limit=note_limit, item_limit=note_item_limit)
    if assumptions:
        has_context = True
        lines.extend(["", "Standing assumptions / regimes / notation from the saved ledger:"])
        lines.extend(f"- {item}" for item in assumptions)
    if notes:
        has_context = True
        lines.extend(["", "Ledger notes that may affect later chunks:"])
        lines.extend(f"- {item}" for item in notes)

    current_index = _chunk_index_value(chunk.get("chunk_index"))
    try:
        records = _read_chunk_records(session)
    except Exception:
        records = []
    prior_records = []
    for rec in records:
        rec_index = _chunk_index_value(rec.get("chunk_index"))
        if current_index is not None and rec_index is not None and rec_index >= current_index:
            continue
        prior_records.append(rec)
    prior_records.sort(key=lambda rec: (_chunk_index_value(rec.get("chunk_index")) or 0, str(rec.get("chunk_id") or "")))
    recent_records = prior_records[-recent_record_limit:]
    if recent_records:
        has_context = True
        lines.extend(["", "Recent chunk context:"])
        for rec in recent_records:
            audit = _load_record_audit_for_running_context(rec)
            heading = " | ".join(
                part
                for part in [
                    str(rec.get("chunk_id") or ""),
                    normalize_whitespace(str(rec.get("label") or rec.get("boundary") or "")),
                    f"pages {rec.get('page_start', '')}-{rec.get('page_end', '')}",
                ]
                if part.strip()
            )
            lines.append(f"- {heading}")
            recent_assumptions = _running_context_items(
                audit.get("assumptions_and_notation"),
                limit=recent_assumption_limit,
                item_limit=recent_assumption_item_limit,
            )
            if recent_assumptions:
                lines.append("  assumptions/notation: " + "; ".join(recent_assumptions))
            recent_verified = _running_context_items(audit.get("verified_steps"), limit=1, item_limit=recent_verified_item_limit)
            if recent_verified:
                lines.append("  verified/contextual step: " + recent_verified[0])
            hint = normalize_whitespace(str(audit.get("next_boundary_hint") or ""))
            if hint:
                lines.append("  next-boundary hint: " + _truncate_text(hint, limit=next_hint_limit))

    try:
        issues_state = load_issues(session)
    except Exception:
        issues_state = {}
    issue_chunk_indices = {
        str(rec.get("chunk_id") or ""): _chunk_index_value(rec.get("chunk_index"))
        for rec in records
        if str(rec.get("chunk_id") or "")
    }
    query_terms = _running_context_query_terms(chunk) if compact else set()
    priority_issues = []
    for issue in (issues_state.get("issues") or []):
        if not isinstance(issue, dict):
            continue
        severity = normalize_whitespace(str(issue.get("severity") or "")).lower()
        status = normalize_whitespace(str(issue.get("status") or "open")).lower()
        if severity not in {"critical", "high"} or status == "resolved":
            continue
        issue_chunk_id = str(issue.get("chunk_id") or "")
        issue_index = issue_chunk_indices.get(issue_chunk_id)
        if current_index is not None and issue_index is not None and issue_index >= current_index:
            continue
        if compact:
            is_recent = (
                current_index is not None
                and issue_index is not None
                and 0 < current_index - issue_index <= _CONTINUOUS_CONTEXT_RECENT_ISSUE_WINDOW
            )
            if not is_recent and _running_context_issue_score(issue, query_terms) < _CONTINUOUS_CONTEXT_ISSUE_MIN_SCORE:
                continue
        priority_issues.append(issue)
    if priority_issues:
        has_context = True
        severity_rank = {"critical": 0, "high": 1}
        priority_issues.sort(
            key=lambda issue: (
                severity_rank.get(normalize_whitespace(str(issue.get("severity") or "")).lower(), 9),
                str(issue.get("chunk_id") or ""),
                str(issue.get("issue_id") or ""),
            )
        )
        lines.extend(["", "Open critical/high issues relevant to this chunk:"])
        for issue in priority_issues[:priority_issue_limit]:
            lines.extend(
                [
                    f"- {issue.get('issue_id', 'issue')} | {issue.get('severity', 'high')} | {issue.get('title', '')}",
                    f"  chunk: {issue.get('chunk_id', '')}; location: {_truncate_text(str(issue.get('location') or ''), limit=180)}",
                    f"  impact: {_truncate_text(str(issue.get('description') or ''), limit=240)}",
                ]
            )

    if not has_context:
        lines.append("- No prior running context has been recorded yet.")
    lines.append("End running audit context.")
    return _truncate_text(_strip_unsafe_control_chars("\n".join(lines).strip()), limit=max_chars)


def _fresh_context_chunk_should_remind_python_checks(chunk: dict[str, Any]) -> bool:
    text = " ".join(
        normalize_whitespace(str(chunk.get(key) or ""))
        for key in ("label", "boundary", "chunk_text")
    ).lower()
    return bool(
        re.search(
            r"\b(theorem|lemma|proposition|prop\.|corollary|proof)\b",
            text,
        )
    )


def build_user_message_for_chunk(session: dict[str, Any], chunk: dict[str, Any]) -> list[dict[str, Any]]:
    content = []
    suppress_pdf_attachment = bool(chunk.get("_suppress_pdf_attachment"))
    if not suppress_pdf_attachment and not session.get("pdf_attached_in_conversation", False):
        content.append({"type": "input_file", "file_id": session["pdf_file_id"]})

    style = _normalize_reference_mention_style(session.get("reference_mention_style", "auto"))
    try:
        ref_state = ensure_reference_map(session)
        style = _effective_reference_mention_style(session, ref_state=ref_state)
        ref_status_note = _reference_prompt_status_note(ref_state)
        ref_context = _reference_context_for_chunk_strict(session, chunk, ref_state=ref_state)
    except Exception as e:
        ref_status_note = (
            f"Reference numbering state could not be fully loaded ({type(e).__name__}: {e}). "
            "Check state/reference_map.json for AUX parsing status. Do not invent numbering."
        )
        ref_context = (
            f"Reference guidance unavailable for this chunk ({type(e).__name__}: {e}). "
            "Check state/reference_map.json for AUX parsing status. Use compiled numbering when known; otherwise cite by label if a source label exists, "
            "and do not invent numbering."
        )
    extra_rerun_instruction = normalize_whitespace(str(chunk.get("_extra_rerun_instruction") or ""))
    rerun_guidance = ""
    if extra_rerun_instruction:
        rerun_guidance = (
            "\nAdditional user guidance for this rerun only:\n"
            f"{extra_rerun_instruction}\n"
        )
    pdf_attachment_note = ""
    if suppress_pdf_attachment:
        note = normalize_whitespace(str(chunk.get("_pdf_attachment_disabled_note") or PDF_TEXT_ONLY_RETRY_NOTE))
        pdf_attachment_note = f"\nPDF attachment note for this retry:\n{note}\n"
    is_fresh_context_mode = (
        _normalize_audit_context_mode(session.get("audit_context_mode"))
        == AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL
    )
    if is_fresh_context_mode:
        fresh_context = build_fresh_audit_context_for_chunk(session, chunk)
        running_context = str(fresh_context.get("block") or "")
        chunk["_retrieved_context_entry_count"] = int(fresh_context.get("entry_count") or 0)
        chunk["_retrieved_context_chars"] = int(fresh_context.get("chars") or len(running_context))
        chunk["_retrieved_context_cap_chars"] = int(fresh_context.get("max_chars") or 0)
        chunk["_running_context_mode"] = FRESH_CONTEXT_RETRIEVAL_PROFILE
        chunk["_running_context_cap_chars"] = int(fresh_context.get("max_chars") or 0)
    else:
        running_context = _build_running_audit_context_for_chunk(
            session,
            chunk,
            max_chars=CONTINUOUS_RUNNING_CONTEXT_MAX_CHARS,
            profile=CONTINUOUS_RUNNING_CONTEXT_PROFILE,
        )
        chunk["_retrieved_context_entry_count"] = 0
        chunk["_retrieved_context_chars"] = 0
        chunk["_retrieved_context_cap_chars"] = 0
        chunk["_running_context_mode"] = CONTINUOUS_RUNNING_CONTEXT_PROFILE
        chunk["_running_context_cap_chars"] = CONTINUOUS_RUNNING_CONTEXT_MAX_CHARS
    macro_glossary = _build_tex_macro_glossary_for_chunk(session, chunk, context_text=running_context)
    fresh_context_verification_reminder = ""
    if is_fresh_context_mode and _fresh_context_chunk_should_remind_python_checks(chunk):
        fresh_context_verification_reminder = (
            "\nFresh-context verification reminder:\n"
            "For theorem, proposition, lemma, or proof chunks, include python_checks when a local symbolic or "
            "numerical sanity check can materially test a claim or suspected issue. Do not force irrelevant checks.\n"
        )
    prompt_text = f"""Audit this mathematics-paper chunk rigorously.

Chunk label: {chunk['label']}
Boundary: {chunk['boundary']}
Source kind: {chunk['source_kind']}
Estimated page span: {chunk['page_start']}-{chunk['page_end']}

Use the provided structured output schema.
Keep mathematical prose human-readable, with inline math in $...$ and display math in $$...$$.
If you include python_checks, every item must contain:
- purpose: a short title
- description: a self-contained explanation of the claim being tested, the test strategy, and any sample parameters or cases used
- expected_outcome: what output or condition would count as success
- code: runnable local Python code
Write the description so it can stand on its own in the final report before the script body.

Reference numbering status for this run:
{ref_status_note}

CRITICAL REFERENCE RULE
{_reference_prompt_rule_for_style(style)}
When the reference guidance below maps a label to a compiled number, write that exact compiled number.
Do NOT reuse source-local numbering from pasted TeX unless the reference guidance below explicitly confirms that it matches the compiled PDF.

Reference guidance for this chunk:
{ref_context}
{rerun_guidance}
{pdf_attachment_note}
{running_context}
{macro_glossary}
{fresh_context_verification_reminder}

Chunk text:
{chunk['chunk_text']}
"""
    content.append({"type": "input_text", "text": prompt_text})
    return [{"role": "user", "content": content}]


def _issue_tags(issue: dict[str, Any]) -> set[str]:
    out = set()
    for tag in issue.get("tags", []) or []:
        s = normalize_whitespace(str(tag)).lower()
        if s:
            out.add(s)
    return out


def _issue_text_for_typo_classification(issue: dict[str, Any]) -> str:
    parts = [
        issue.get("title", ""),
        issue.get("location", ""),
        issue.get("description", ""),
        issue.get("evidence", ""),
        issue.get("proposed_fix", ""),
    ]
    return normalize_whitespace(" ".join(str(x) for x in parts if x)).lower()


def _is_pure_typographical_issue(issue: dict[str, Any]) -> bool:
    tags = _issue_tags(issue)
    text = _issue_text_for_typo_classification(issue)
    positive = bool(tags & _TYPO_POSITIVE_TAGS) or any(keyword in text for keyword in _TYPO_KEYWORDS)
    negative = bool(tags & _TYPO_NEGATIVE_TAGS)
    return positive and not negative


def _is_concise_priority_issue(issue: dict[str, Any]) -> bool:
    return normalize_whitespace(str(issue.get("severity", "") or "")).lower() in {"critical", "high"}


_CONCISE_REPORT_DEFAULT_PRESET = "strict_concise"
_CONCISE_REPORT_PRESETS: dict[str, dict[str, Any]] = {
    "strict_concise": {
        "preset": "strict_concise",
        "include_critical": True,
        "include_high": True,
        "include_medium": False,
        "include_low": False,
        "include_typographical_issues": True,
        "include_audit_summary": True,
        "include_verification_summary": True,
        "include_omitted_material_note": True,
        "only_open_issues": True,
    },
    "balanced_concise": {
        "preset": "balanced_concise",
        "include_critical": True,
        "include_high": True,
        "include_medium": True,
        "include_low": False,
        "include_typographical_issues": True,
        "include_audit_summary": True,
        "include_verification_summary": True,
        "include_omitted_material_note": True,
        "only_open_issues": True,
    },
    "minimal_referee": {
        "preset": "minimal_referee",
        "include_critical": True,
        "include_high": True,
        "include_medium": False,
        "include_low": False,
        "include_typographical_issues": False,
        "include_audit_summary": True,
        "include_verification_summary": False,
        "include_omitted_material_note": True,
        "only_open_issues": True,
    },
}


def _normalize_concise_preset_name(value: Any) -> str:
    text = normalize_whitespace(str(value or "")).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "strict": "strict_concise",
        "strict_concise": "strict_concise",
        "balanced": "balanced_concise",
        "balanced_concise": "balanced_concise",
        "minimal": "minimal_referee",
        "minimal_referee": "minimal_referee",
        "minimal_referee_version": "minimal_referee",
        "custom": "custom",
    }
    return aliases.get(text, _CONCISE_REPORT_DEFAULT_PRESET)


def concise_report_options_for_preset(preset: str) -> dict[str, Any]:
    clean = _normalize_concise_preset_name(preset)
    if clean == "custom":
        clean = _CONCISE_REPORT_DEFAULT_PRESET
    return dict(_CONCISE_REPORT_PRESETS.get(clean, _CONCISE_REPORT_PRESETS[_CONCISE_REPORT_DEFAULT_PRESET]))


def default_concise_report_options() -> dict[str, Any]:
    return concise_report_options_for_preset(_CONCISE_REPORT_DEFAULT_PRESET)


def _normalize_concise_report_options(options: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    source = options if isinstance(options, dict) else {}
    preset = _normalize_concise_preset_name(source.get("preset", _CONCISE_REPORT_DEFAULT_PRESET))
    base_preset = _CONCISE_REPORT_DEFAULT_PRESET if preset == "custom" else preset
    normalized = concise_report_options_for_preset(base_preset)
    for key in [
        "include_critical",
        "include_high",
        "include_medium",
        "include_low",
        "include_typographical_issues",
        "include_audit_summary",
        "include_verification_summary",
        "include_omitted_material_note",
        "only_open_issues",
    ]:
        if key in source:
            normalized[key] = bool(source.get(key))
    normalized["preset"] = preset
    return normalized


def _concise_included_severities(options: dict[str, Any]) -> set[str]:
    return {
        severity
        for severity in ["critical", "high", "medium", "low"]
        if options.get(f"include_{severity}", False)
    }


def _is_concise_selected_issue(issue: dict[str, Any], options: dict[str, Any]) -> bool:
    severity = normalize_whitespace(str(issue.get("severity", "") or "")).lower()
    return severity in _concise_included_severities(options)


def _notable_proof_reference_text(issue: dict[str, Any]) -> str:
    parts = [
        issue.get("title", ""),
        issue.get("location", ""),
        issue.get("description", ""),
        issue.get("evidence", ""),
        issue.get("proposed_fix", ""),
    ]
    return normalize_whitespace(" ".join(str(part) for part in parts if part)).lower()


def _notable_proof_reference_score(issue: dict[str, Any]) -> int:
    if _is_pure_typographical_issue(issue):
        return 0
    severity = normalize_whitespace(str(issue.get("severity", "") or "")).lower()
    if severity != "medium":
        return 0
    tags = _issue_tags(issue)
    text = _notable_proof_reference_text(issue)
    tag_hits = tags & _NOTABLE_PROOF_REFERENCE_TAGS
    strong_hits = sum(1 for keyword in _NOTABLE_PROOF_REFERENCE_STRONG_KEYWORDS if keyword in text)
    has_reference_target = any(keyword in text for keyword in _NOTABLE_PROOF_REFERENCE_TARGET_KEYWORDS)
    has_reference_error = any(keyword in text for keyword in _NOTABLE_PROOF_REFERENCE_ERROR_KEYWORDS)
    if not (tag_hits or strong_hits or (has_reference_target and has_reference_error)):
        return 0

    score = 0
    score += 6 * len(tag_hits)
    score += 7 * strong_hits
    if has_reference_target and has_reference_error:
        score += 5
    if "proof" in text and ("cite" in text or "reference" in text) and "being proved" in text:
        score += 4
    if "prevents checking" in text or "cannot check" in text:
        score += 2
    if issue.get("proposed_fix"):
        score += 1
    return score


def _is_notable_proof_reference_issue(issue: dict[str, Any]) -> bool:
    return _notable_proof_reference_score(issue) >= 4


def _chunk_record_map(chunk_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for rec in chunk_records or []:
        chunk_id = str(rec.get("chunk_id") or "").strip()
        if chunk_id:
            out[chunk_id] = rec
    return out


def _page_range_text(rec: Optional[dict[str, Any]], markdown: bool = True) -> str:
    if not isinstance(rec, dict):
        return ""
    try:
        start = int(rec.get("page_start"))
        end = int(rec.get("page_end"))
    except Exception:
        return ""
    if start <= 0 or end <= 0:
        return ""
    if start == end:
        return f"Page {start}"
    sep = "-" if markdown else "--"
    return f"Pages {start}{sep}{end}"


def _approx_page_location_text(
    rec: Optional[dict[str, Any]],
    chunk_id: str,
    markdown: bool = True,
) -> str:
    chunk_id = normalize_whitespace(str(chunk_id or ""))
    if not isinstance(rec, dict):
        return f"Chunk: {chunk_id}" if chunk_id else ""
    try:
        start = int(rec.get("page_start"))
        end = int(rec.get("page_end"))
    except Exception:
        return f"Chunk: {chunk_id}" if chunk_id else ""
    if start <= 0 or end <= 0:
        return f"Chunk: {chunk_id}" if chunk_id else ""
    suffix = f" ({chunk_id})" if chunk_id else ""
    if start == end:
        return f"Approx. page: {start}{suffix}"
    sep = "-" if markdown else "--"
    return f"Approx. pages: {start}{sep}{end}{suffix}"


def _extract_searchable_phrases(issue: dict[str, Any], max_items: int = 2) -> list[str]:
    fragments = []
    for key in ["evidence", "description", "location"]:
        text = str(issue.get(key, "") or "")
        for frag in _TYPO_LITERAL_FRAGMENT_RE.findall(text):
            frag = normalize_whitespace(frag)
            if frag:
                fragments.append(frag)
    fragments = _dedupe_preserve_order(fragments)
    preferred = [frag for frag in fragments if any(ch.isspace() for ch in frag) or "?" in frag or "--" in frag]
    return (preferred or fragments)[:max_items]


def _extract_incorrect_text(issue: dict[str, Any]) -> str:
    for key in ["description", "evidence"]:
        text = str(issue.get(key, "") or "")
        m = re.search(r"currently reads\s+(\$\$.*?\$\$|`[^`]+`)", text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return normalize_whitespace(m.group(1).strip("`"))
    return ""


def _collect_typographical_issue_entries(
    issues: list[dict[str, Any]],
    chunk_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    chunk_map = _chunk_record_map(chunk_records)
    entries = []
    for issue in issues or []:
        if not _is_pure_typographical_issue(issue):
            continue
        rec = chunk_map.get(str(issue.get("chunk_id") or "").strip())
        entries.append(
            {
                "issue": issue,
                "chunk_record": rec,
                "page_text_md": _page_range_text(rec, markdown=True),
                "page_text_tex": _page_range_text(rec, markdown=False),
                "location_text_md": _approx_page_location_text(rec, str(issue.get("chunk_id") or ""), markdown=True),
                "location_text_tex": _approx_page_location_text(rec, str(issue.get("chunk_id") or ""), markdown=False),
                "location_detail": normalize_whitespace(str(issue.get("location", "") or "")),
                "searchable_phrases": _extract_searchable_phrases(issue),
                "incorrect_text": _extract_incorrect_text(issue),
                "page_start_sort": int(rec.get("page_start") or 10**9) if isinstance(rec, dict) else 10**9,
                "page_end_sort": int(rec.get("page_end") or 10**9) if isinstance(rec, dict) else 10**9,
            }
        )
    entries.sort(
        key=lambda item: (item["page_start_sort"], item["page_end_sort"], str(item["issue"].get("issue_id") or ""))
    )
    return entries


def _issue_report_entry(
    issue: dict[str, Any],
    chunk_map: dict[str, dict[str, Any]],
    issue_recheck_overlay: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    chunk_id = str(issue.get("chunk_id") or "").strip()
    rec = chunk_map.get(chunk_id)
    issue_id = str(issue.get("issue_id") or "")
    return {
        "issue": issue,
        "chunk_record": rec,
        "location_text_md": _approx_page_location_text(rec, chunk_id, markdown=True),
        "location_text_tex": _approx_page_location_text(rec, chunk_id, markdown=False),
        "location_detail": normalize_whitespace(str(issue.get("location", "") or "")),
        "recheck": (issue_recheck_overlay or {}).get("issue_recommendations", {}).get(issue_id, {}),
    }


def _issue_recheck_sidecar_path(session: dict[str, Any]) -> Path:
    return Path(session["workdir"]) / "state" / "issue_rechecks.json"


def _issue_recheck_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _issue_recheck_record_for(issue_map: dict[str, dict[str, Any]], issue_id: Any) -> dict[str, Any]:
    issue_id = str(issue_id or "").strip()
    if not issue_id:
        return {}
    return issue_map.setdefault(
        issue_id,
        {
            "issue_id": issue_id,
            "family_ids": [],
            "upstream_family_ids": [],
            "downstream_family_ids": [],
            "grouped_under_issue_ids": [],
            "grouped_downstream_issue_ids": [],
        },
    )


def _append_unique(target: list[str], values: Any) -> None:
    if isinstance(values, str):
        values = [values]
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in target:
            target.append(text)


def _status_text_marks_downstream(value: Any) -> bool:
    text = normalize_whitespace(str(value or "")).lower().replace("_", "-")
    return "downstream-covered" in text or "downstream covered" in text or "low/downstream" in text


def _status_text_marks_human_review(value: Any) -> bool:
    text = normalize_whitespace(str(value or "")).lower().replace("_", "-")
    return "human-review" in text or "human review" in text or "needs human" in text


def _build_issue_recheck_overlay(session: dict[str, Any]) -> dict[str, Any]:
    sidecar_path = _issue_recheck_sidecar_path(session)
    overlay: dict[str, Any] = {
        "recheck_applied": False,
        "sidecar_path": str(sidecar_path),
        "families": [],
        "issue_recommendations": {},
        "grouped_downstream_issues": {},
        "issue_recheck_summary": {
            "accepted_recheck_count": 0,
            "family_count": 0,
            "issue_count": 0,
            "downstream_covered_issue_count": 0,
            "human_review_issue_count": 0,
        },
        "warnings": [],
    }
    if not sidecar_path.exists():
        return overlay
    try:
        payload = load_json(sidecar_path)
    except Exception as exc:
        overlay["warnings"].append(f"Could not load issue recheck sidecar: {exc}")
        return overlay
    if not isinstance(payload, dict):
        overlay["warnings"].append("Issue recheck sidecar is not a JSON object.")
        return overlay
    rechecks = payload.get("rechecks")
    if not isinstance(rechecks, list):
        overlay["warnings"].append("Issue recheck sidecar has no rechecks list.")
        return overlay

    records = [record for record in rechecks if isinstance(record, dict)]
    records.sort(key=lambda record: str(record.get("accepted_at") or ""))
    issue_map: dict[str, dict[str, Any]] = {}
    families_seen: set[str] = set()
    for record in records:
        family_id = str(record.get("family_id") or "").strip()
        if not family_id:
            overlay["warnings"].append("Accepted issue recheck record without family_id was ignored.")
            continue
        accepted_at = str(record.get("accepted_at") or "")
        family = {
            "family_id": family_id,
            "recheck_id": record.get("recheck_id"),
            "accepted_at": accepted_at,
            "verdict": record.get("verdict"),
            "summary": record.get("summary"),
            "final_report_treatment": record.get("final_report_treatment"),
            "needs_human_review": bool(record.get("needs_human_review")),
            "upstream_issue_ids": _issue_recheck_string_list(record.get("upstream_issue_ids")),
            "downstream_issue_ids": _issue_recheck_string_list(record.get("downstream_issue_ids")),
            "false_positive_issue_ids": _issue_recheck_string_list(record.get("false_positive_issue_ids")),
        }
        overlay["families"].append(family)
        families_seen.add(family_id)
        for issue_id in family["upstream_issue_ids"]:
            rec = _issue_recheck_record_for(issue_map, issue_id)
            _append_unique(rec["family_ids"], family_id)
            _append_unique(rec["upstream_family_ids"], family_id)
        for issue_id in family["downstream_issue_ids"]:
            rec = _issue_recheck_record_for(issue_map, issue_id)
            _append_unique(rec["family_ids"], family_id)
            _append_unique(rec["downstream_family_ids"], family_id)

        for item in record.get("recommended_severity_by_issue") or []:
            if not isinstance(item, dict):
                continue
            rec = _issue_recheck_record_for(issue_map, item.get("issue_id"))
            if not rec:
                continue
            _append_unique(rec["family_ids"], family_id)
            value = str(item.get("severity") or "")
            rec["recommended_severity"] = {
                "value": value,
                "rationale": str(item.get("rationale") or ""),
                "family_id": family_id,
                "accepted_at": accepted_at,
            }
            if _status_text_marks_downstream(value):
                rec["downstream_covered"] = True
            if _status_text_marks_human_review(value):
                rec["needs_human_review"] = True

        for item in record.get("recommended_status_by_issue") or []:
            if not isinstance(item, dict):
                continue
            rec = _issue_recheck_record_for(issue_map, item.get("issue_id"))
            if not rec:
                continue
            _append_unique(rec["family_ids"], family_id)
            value = str(item.get("status") or "")
            rec["recommended_status"] = {
                "value": value,
                "rationale": str(item.get("rationale") or ""),
                "family_id": family_id,
                "accepted_at": accepted_at,
            }
            if _status_text_marks_downstream(value):
                rec["downstream_covered"] = True
            if _status_text_marks_human_review(value):
                rec["needs_human_review"] = True

        for item in record.get("grouping_recommendations") or []:
            if not isinstance(item, dict):
                continue
            upstream = str(item.get("upstream_issue_id") or "").strip()
            downstream = _issue_recheck_string_list(item.get("downstream_issue_ids"))
            if not upstream or not downstream:
                continue
            overlay["grouped_downstream_issues"].setdefault(upstream, [])
            _append_unique(overlay["grouped_downstream_issues"][upstream], downstream)
            upstream_rec = _issue_recheck_record_for(issue_map, upstream)
            _append_unique(upstream_rec["family_ids"], family_id)
            _append_unique(upstream_rec["grouped_downstream_issue_ids"], downstream)
            for issue_id in downstream:
                rec = _issue_recheck_record_for(issue_map, issue_id)
                _append_unique(rec["family_ids"], family_id)
                _append_unique(rec["grouped_under_issue_ids"], upstream)
                rec["downstream_covered"] = True
                rec["grouping_rationale"] = str(item.get("rationale") or "")

    for rec in issue_map.values():
        if rec.get("downstream_covered"):
            rec["report_treatment"] = "downstream-covered"
        elif rec.get("needs_human_review"):
            rec["report_treatment"] = "needs-human-review"
        elif rec.get("upstream_family_ids"):
            rec["report_treatment"] = "upstream"
    overlay["issue_recommendations"] = issue_map
    downstream_count = sum(1 for rec in issue_map.values() if rec.get("downstream_covered"))
    human_count = sum(1 for rec in issue_map.values() if rec.get("needs_human_review"))
    overlay["recheck_applied"] = bool(records and issue_map)
    overlay["issue_recheck_summary"] = {
        "accepted_recheck_count": len(records),
        "family_count": len(families_seen),
        "issue_count": len(issue_map),
        "downstream_covered_issue_count": downstream_count,
        "human_review_issue_count": human_count,
    }
    return overlay


def _issue_recheck_is_downstream_covered(issue: dict[str, Any], overlay: Optional[dict[str, Any]]) -> bool:
    issue_id = str(issue.get("issue_id") or "")
    rec = (overlay or {}).get("issue_recommendations", {}).get(issue_id, {})
    return bool(rec.get("downstream_covered"))


def _issue_recheck_markdown_lines(issue: dict[str, Any], rec: Optional[dict[str, Any]]) -> list[str]:
    if not rec:
        return []
    lines = ["- Recheck overlay: accepted advisory recheck present; canonical issue record unchanged."]
    severity = rec.get("recommended_severity") if isinstance(rec.get("recommended_severity"), dict) else None
    if severity:
        lines.append(
            f"- Rechecked severity: original `{issue.get('severity', '')}`; recommended `{normalize_math_delimiters(severity.get('value', ''))}`"
            + (f" ({normalize_math_delimiters(severity.get('rationale', ''))})" if severity.get("rationale") else "")
        )
    status = rec.get("recommended_status") if isinstance(rec.get("recommended_status"), dict) else None
    if status:
        lines.append(
            f"- Rechecked status/treatment: {normalize_math_delimiters(status.get('value', ''))}"
            + (f" ({normalize_math_delimiters(status.get('rationale', ''))})" if status.get("rationale") else "")
        )
    grouped_under = rec.get("grouped_under_issue_ids") or []
    if grouped_under:
        lines.append(f"- Grouped downstream under: {', '.join(grouped_under)}")
    grouped_downstream = rec.get("grouped_downstream_issue_ids") or []
    if grouped_downstream:
        lines.append(f"- Downstream-covered issues: {', '.join(grouped_downstream)}")
    if rec.get("needs_human_review"):
        lines.append("- Recheck warning: needs separate human review.")
    return lines


def _issue_recheck_tex_items(issue: dict[str, Any], rec: Optional[dict[str, Any]]) -> list[str]:
    if not rec:
        return []
    render = _report_latex_paragraph_local
    items = [r"\item Recheck overlay: accepted advisory recheck present; canonical issue record unchanged."]
    severity = rec.get("recommended_severity") if isinstance(rec.get("recommended_severity"), dict) else None
    if severity:
        text = f"Rechecked severity: original {issue.get('severity', '')}; recommended {severity.get('value', '')}"
        if severity.get("rationale"):
            text += f" ({severity.get('rationale')})"
        items.append(r"\item " + render(text))
    status = rec.get("recommended_status") if isinstance(rec.get("recommended_status"), dict) else None
    if status:
        text = f"Rechecked status/treatment: {status.get('value', '')}"
        if status.get("rationale"):
            text += f" ({status.get('rationale')})"
        items.append(r"\item " + render(text))
    grouped_under = rec.get("grouped_under_issue_ids") or []
    if grouped_under:
        items.append(r"\item " + render("Grouped downstream under: " + ", ".join(grouped_under)))
    grouped_downstream = rec.get("grouped_downstream_issue_ids") or []
    if grouped_downstream:
        items.append(r"\item " + render("Downstream-covered issues: " + ", ".join(grouped_downstream)))
    if rec.get("needs_human_review"):
        items.append(r"\item Recheck warning: needs separate human review.")
    return items


def _collect_notable_proof_reference_issue_entries(
    issues: list[dict[str, Any]],
    chunk_records: list[dict[str, Any]],
    excluded_issue_ids: Optional[set[str]] = None,
    issue_recheck_overlay: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    excluded_issue_ids = excluded_issue_ids or set()
    chunk_map = _chunk_record_map(chunk_records)
    scored: list[tuple[int, dict[str, Any]]] = []
    for issue in issues or []:
        issue_id = str(issue.get("issue_id") or "")
        if issue_id in excluded_issue_ids:
            continue
        if _issue_recheck_is_downstream_covered(issue, issue_recheck_overlay):
            continue
        if not _is_notable_proof_reference_issue(issue):
            continue
        scored.append((_notable_proof_reference_score(issue), issue))
    scored.sort(key=lambda item: (-item[0], _concise_issue_sort_key(item[1], chunk_map)))
    return [
        _issue_report_entry(issue, chunk_map, issue_recheck_overlay)
        for _score, issue in scored[:_NOTABLE_PROOF_REFERENCE_MAX_ISSUES]
    ]


def _typographical_errors_markdown(entries: list[dict[str, Any]]) -> str:
    lines = ["## Typographical errors", ""]
    if not entries:
        lines.append("- No typographical/copyediting issues identified.")
        lines.append("")
        return "\n".join(lines)
    for entry in entries:
        issue = entry["issue"]
        lines.append(
            f"### {issue.get('issue_id', 'issue')} — {normalize_math_delimiters(issue.get('title', 'Untitled issue'))} [{issue.get('severity', 'low')}]"
        )
        if entry.get("location_text_md"):
            lines.append(f"- {entry['location_text_md']}")
        if entry.get("location_detail"):
            lines.append(f"- Location detail: {normalize_math_delimiters(entry['location_detail'])}")
        phrases = entry.get("searchable_phrases") or []
        if phrases:
            label = "Searchable phrase" if len(phrases) == 1 else "Searchable phrases"
            lines.append(f"- {label}: {'; '.join(normalize_math_delimiters(x) for x in phrases)}")
        if entry.get("incorrect_text"):
            lines.append(f"- Incorrect text: {normalize_math_delimiters(entry['incorrect_text'])}")
        if issue.get("proposed_fix"):
            lines.append(f"- Suggested correction: {normalize_math_delimiters(issue.get('proposed_fix', ''))}")
        lines.append("")
    return "\n".join(lines)


def _typographical_errors_tex(entries: list[dict[str, Any]]) -> str:
    render = _report_latex_paragraph_local
    parts = [r"\section*{Typographical errors}"]
    if not entries:
        parts.append("No typographical/copyediting issues identified.")
        return "\n".join(parts) + "\n"
    for entry in entries:
        issue = entry["issue"]
        title = render(
            f"{issue.get('issue_id', 'issue')} -- {issue.get('title', 'Untitled issue')} [{issue.get('severity', 'low')}]"
        )
        parts.append(r"\subsection*{" + title + "}")
        parts.append(r"\begin{itemize}")
        if entry.get("location_text_tex"):
            parts.append(r"\item " + render(entry["location_text_tex"]))
        if entry.get("location_detail"):
            parts.append(r"\item Location detail: " + render(entry["location_detail"]))
        phrases = entry.get("searchable_phrases") or []
        if phrases:
            label = "Searchable phrase" if len(phrases) == 1 else "Searchable phrases"
            parts.append(r"\item " + render(label + ": " + "; ".join(str(x) for x in phrases)))
        if entry.get("incorrect_text"):
            parts.append(r"\item Incorrect text: " + render(entry["incorrect_text"]))
        if issue.get("proposed_fix"):
            parts.append(r"\item Suggested correction: " + render(issue.get("proposed_fix", "")))
        parts.append(r"\end{itemize}")
    return "\n".join(parts) + "\n"


def _extract_safe_report_preamble(tex_path: str | Path | None) -> str:
    if not tex_path:
        return ""
    tex_path = Path(tex_path)
    if not tex_path.exists():
        return ""

    try:
        text = tex_path.read_text(encoding="utf-8")
    except Exception:
        try:
            text = tex_path.read_text(encoding="latin-1")
        except Exception:
            return ""

    pre = text.split(r"\begin{document}", 1)[0]
    pre = strip_tex_comments(pre)
    lines = pre.splitlines()

    package_lines = []
    macro_lines = []
    collecting = False
    current = []
    balance = 0

    def flush_current() -> None:
        nonlocal collecting, current, balance
        if current:
            block = "\n".join(current).strip()
            if block:
                macro_lines.append(block)
        collecting = False
        current = []
        balance = 0

    macro_start_re = re.compile(
        r"^\s*\\(?:newcommand|providecommand|DeclareMathOperator\*?|DeclareRobustCommand)\b"
    )
    usepkg_re = re.compile(r"^\s*\\usepackage(\[[^\]]*\])?\{([^}]*)\}")
    tikzlib_re = re.compile(r"^\s*\\usetikzlibrary(?:\[[^\]]*\])?\{([^}]*)\}")

    for line in lines:
        s = line.strip()
        if not s:
            if collecting:
                current.append(line)
            continue

        m_pkg = usepkg_re.match(s)
        if m_pkg:
            pkg_opts = m_pkg.group(1) or ""
            pkgs = [p.strip() for p in m_pkg.group(2).split(",") if p.strip()]
            keep_pkgs = [p for p in pkgs if (p in SAFE_REPORT_PACKAGES) and (p not in BASE_REPORT_PACKAGES)]
            if keep_pkgs:
                package_lines.append(r"\usepackage" + pkg_opts + "{" + ",".join(keep_pkgs) + "}")
            continue

        m_tikz = tikzlib_re.match(s)
        if m_tikz:
            package_lines.append(s)
            continue

        if not collecting and macro_start_re.match(s):
            collecting = True
            current = [line]
            balance = line.count("{") - line.count("}")
            if balance <= 0:
                flush_current()
            continue

        if collecting:
            current.append(line)
            balance += line.count("{") - line.count("}")
            if balance <= 0:
                flush_current()

    if collecting:
        flush_current()

    keep_macros = []
    unsafe_macro_tokens = [
        r"\write18",
        r"\openout",
        r"\input",
        r"\include",
        r"\catcode",
        r"\read",
        r"\begin",
        r"\end",
        "tikzpicture",
        r"\node",
        r"\maketitle",
        r"\author",
        r"\email",
        r"\section",
        r"\subsection",
        r"\paragraph",
        r"\caption",
        r"\graphicspath",
    ]
    for block in macro_lines:
        if re.match(r"^\s*\\(?:renewcommand|def|newtheorem)\b", block):
            continue
        if any(tok in block for tok in unsafe_macro_tokens):
            continue
        keep_macros.append(block)

    package_lines = _dedupe_preserve_order(package_lines)
    keep_macros = _dedupe_preserve_order(keep_macros)

    if not package_lines and not keep_macros:
        return ""

    return "\n".join(package_lines + [""] + keep_macros).strip() + "\n"


def _normalize_report_latex_unicode_math(s: str) -> str:
    return normalize_report_latex_unicode_math(s)


def _report_escape_text(s: str) -> str:
    s = _repair_report_escape_artifacts("" if s is None else str(s))
    s = sanitize_ascii_punctuation(s)
    s = _normalize_report_latex_unicode_math(s)
    s = sanitize_latex_unsupported_unicode(s)
    repl = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(repl.get(ch, ch) for ch in s)


def _report_latex_paragraph_local(text: str) -> str:
    text = _repair_report_escape_artifacts("" if text is None else str(text))
    text = normalize_math_delimiters(text)
    text = _strip_unsafe_control_chars(_repair_report_escape_artifacts(text))
    if _report_latex_text_looks_globally_unsafe(text):
        return _report_escape_text(text)
    parts = re.split(r"(\$\$.*?\$\$|\$.*?\$)", text, flags=re.DOTALL)
    out = []
    for part in parts:
        if not part:
            continue
        if (part.startswith("$$") and part.endswith("$$")) or (part.startswith("$") and part.endswith("$")):
            delim = "$$" if part.startswith("$$") else "$"
            body = part[len(delim) : -len(delim)]
            body = repair_report_latex_math_command_artifacts(body)
            body = sanitize_ascii_punctuation(body)
            body = _normalize_report_latex_unicode_math(body)
            if _report_math_text_looks_unsafe(body):
                out.append(r"\texttt{" + _report_escape_text(part) + "}")
            else:
                out.append(delim + body + delim)
        else:
            out.append(_report_escape_text(part))
    return "".join(out)


def report_latex_itemize(items: list[str], indent: str = "") -> str:
    items = list(items or [])
    if not items:
        return indent + r"\begin{itemize}" + "\n" + indent + r"\item None." + "\n" + indent + r"\end{itemize}"
    body = "\n".join(indent + r"\item " + _report_latex_paragraph_local(x) for x in items)
    return indent + r"\begin{itemize}" + "\n" + body + "\n" + indent + r"\end{itemize}"


def _python_check_entries_for_record(
    session: dict[str, Any],
    rec: dict[str, Any],
    audit: dict[str, Any],
) -> list[dict[str, str]]:
    checks = audit.get("python_checks", []) or []
    raw_paths = rec.get("python_paths", []) if isinstance(rec.get("python_paths"), list) else []
    workdir = Path(session["workdir"]).resolve()
    entries = []
    for idx, chk in enumerate(checks, start=1):
        display_path = ""
        raw_path = raw_paths[idx - 1] if idx - 1 < len(raw_paths) else None
        if raw_path:
            path_obj = Path(str(raw_path))
            try:
                if path_obj.is_absolute():
                    display_path = path_obj.resolve().relative_to(workdir).as_posix()
                else:
                    display_path = path_obj.as_posix()
            except Exception:
                display_path = (Path("python_checks") / path_obj.name).as_posix() if path_obj.name else str(path_obj)
        if not display_path:
            chunk_id = str(rec.get("chunk_id") or "chunk")
            display_path = f"python_checks/{chunk_id}_check_{idx:02d}.py"
        entry = _normalize_python_check_entry(
            chk,
            chunk_label=str(rec.get("label") or audit.get("label") or ""),
            chunk_boundary=str(rec.get("boundary") or audit.get("boundary") or ""),
        )
        entry["display_path"] = display_path
        entries.append(entry)
    return entries


def _build_final_report_markdown_base(session: dict[str, Any], report_title: Optional[str] = None) -> str:
    ledger = load_ledger(session)
    issues_state = load_issues(session)
    usage = load_usage(session)
    status = load_status(session)
    manifest = load_manifest(session)
    chunk_records = _read_chunk_records(session)
    issue_recheck_overlay = _build_issue_recheck_overlay(session)

    title = report_title or f"Audit report — {Path(session['pdf_path']).stem}"
    lines = [
        f"# {title}",
        "",
        f"- PDF: {session['pdf_path']}",
        f"- TeX: {session.get('tex_path') or 'not found'}",
        f"- Model: {session['model']}",
        f"- Reasoning effort: {session['reasoning_effort']}",
        f"- Chunking mode: {manifest.get('chunking_mode')}",
        f"- Chunks completed: {status['chunks_completed']} / {status['chunks_total']}",
        f"- Estimated pages audited: {status['estimated_pages_completed']} / {status['estimated_pages_total']}",
        f"- Total cost (USD): {usage['totals']['cost_usd']:.4f}",
        f"- Total tokens: {usage['totals']['total_tokens']}",
        "",
        "## Ledger assumptions",
        format_list_for_markdown(ledger.get("assumptions", [])),
        "",
        "## Ledger notes",
        format_list_for_markdown(ledger.get("notes", [])),
        "",
        "## Open issues",
    ]

    open_issues = [x for x in issues_state["issues"] if x.get("status", "open") == "open"]
    typo_entries = _collect_typographical_issue_entries(open_issues, chunk_records)
    main_open_issues = [x for x in open_issues if not _is_pure_typographical_issue(x)]
    if not main_open_issues:
        lines.append("- No open mathematical/correctness issues.")
    else:
        for issue in main_open_issues:
            recheck_rec = issue_recheck_overlay.get("issue_recommendations", {}).get(str(issue.get("issue_id") or ""), {})
            lines.extend(
                [
                    f"### {issue['issue_id']} — {normalize_math_delimiters(issue['title'])} [{issue['severity']}]",
                    f"- Chunk: {issue['chunk_id']}",
                    f"- Location: {normalize_math_delimiters(issue['location'])}",
                    f"- Description: {normalize_math_delimiters(issue['description'])}",
                    f"- Evidence: {normalize_math_delimiters(issue['evidence'])}",
                    f"- Proposed fix: {normalize_math_delimiters(issue['proposed_fix'])}",
                    f"- Tags: {', '.join(issue.get('tags', [])) if issue.get('tags') else 'none'}",
                ]
            )
            lines.extend(_issue_recheck_markdown_lines(issue, recheck_rec))
            lines.append("")

    lines.extend([_typographical_errors_markdown(typo_entries), ""])

    lines.append("## Chunk overview")
    if not chunk_records:
        lines.append("- No chunk records found.")
    else:
        for rec in chunk_records:
            audit = _coerce_audit_payload(load_json(rec["structured_response_path"]))
            lines.extend(
                [
                    f"### {normalize_math_delimiters(rec['chunk_id'])} — {normalize_math_delimiters(rec['label'])}",
                    f"- Boundary: {normalize_math_delimiters(rec['boundary'])}",
                    f"- Estimated pages: {rec['page_start']}-{rec['page_end']}",
                    f"- Cost: ${rec['cost_usd']:.4f}",
                    f"- Confidence: {audit.get('confidence','medium')}",
                    "",
                    "#### Assumptions and notation",
                    format_list_for_markdown(audit.get("assumptions_and_notation", [])),
                    "",
                    "#### Verified steps",
                    format_list_for_markdown(audit.get("verified_steps", [])),
                    "",
                    "#### Issues found",
                ]
            )
            verification_mode = str(rec.get("verification_mode", "local_python_only"))
            lines.append(f"- Verification mode: {verification_mode}")
            verification_summary = rec.get("verification_summary") if isinstance(rec.get("verification_summary"), dict) else {}
            if verification_summary.get("used_code_interpreter"):
                lines.append(f"- Code Interpreter tool events: {int(verification_summary.get('tool_event_count', 0) or 0)}")
                ci_files = verification_summary.get("file_ids") if isinstance(verification_summary.get("file_ids"), list) else []
                if ci_files:
                    lines.append(f"- Code Interpreter files: {', '.join(ci_files[:6])}")
            lines.append("")
            chunk_issues = [issue for issue in (audit.get("issues") or []) if not _is_pure_typographical_issue(issue)]
            if chunk_issues:
                for issue in chunk_issues:
                    lines.extend(
                        [
                            f"- {normalize_math_delimiters(issue.get('title','Untitled issue'))} [{issue.get('severity','low')}]",
                            f"  - Location: {normalize_math_delimiters(issue.get('location',''))}",
                            f"  - Description: {normalize_math_delimiters(issue.get('description',''))}",
                            f"  - Proposed fix: {normalize_math_delimiters(issue.get('proposed_fix',''))}",
                        ]
                    )
            elif audit.get("issues"):
                lines.append(
                    "- No mathematical/correctness issues found in this chunk. Typographical/copyediting items are summarized separately in the Typographical errors section."
                )
            else:
                lines.append("- No issues found.")
            python_entries = _python_check_entries_for_record(session, rec, audit)
            if python_entries:
                lines.extend(["", "#### Suggested local Python checks"])
                for entry in python_entries:
                    lines.extend(
                        [
                            f"##### {normalize_math_delimiters(entry['purpose'])}",
                            normalize_math_delimiters(entry["description"]),
                            f"- Expected outcome: {normalize_math_delimiters(entry['expected_outcome'])}",
                            f"- Script file: `{entry['display_path']}`",
                            "```python",
                            entry["code"].rstrip(),
                            "```",
                            "",
                        ]
                    )
            lines.extend(
                [
                    "#### Next boundary hint",
                    normalize_math_delimiters(audit.get("next_boundary_hint", "None.")),
                    "",
                ]
            )
    return "\n".join(lines).strip() + "\n"


def _build_final_report_tex_base(session: dict[str, Any], report_title: Optional[str] = None) -> str:
    ledger = load_ledger(session)
    issues_state = load_issues(session)
    usage = load_usage(session)
    status = load_status(session)
    manifest = load_manifest(session)
    chunk_records = _read_chunk_records(session)
    issue_recheck_overlay = _build_issue_recheck_overlay(session)

    title = _report_latex_paragraph_local(report_title or f"Audit report -- {Path(session['pdf_path']).stem}")
    pdf_path_text = _report_latex_paragraph_local(session["pdf_path"])
    tex_path_text = _report_latex_paragraph_local(session.get("tex_path") or "not found")
    chunking_mode_text = _report_latex_paragraph_local(str(manifest.get("chunking_mode")))
    model_text = _report_latex_paragraph_local(session["model"])
    effort_text = _report_latex_paragraph_local(session["reasoning_effort"])
    imported_preamble = _extract_safe_report_preamble(session.get("tex_path"))

    parts = [
        r"""\documentclass[11pt]{article}
\usepackage[a4paper,margin=1in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{microtype}
\usepackage{amsmath,amssymb,mathtools}
\usepackage{hyperref}
\usepackage{enumitem}
\usepackage{longtable}
\usepackage{booktabs}
\usepackage{xcolor}
\usepackage{fancyvrb}
\setlist[itemize]{leftmargin=2em}
\setlength{\parskip}{0.5em}
\setlength{\parindent}{0pt}

"""
    ]
    if imported_preamble:
        parts.append("% Imported selectively from the paper preamble for macro compatibility\n")
        parts.append(imported_preamble)
        parts.append("\n")

    parts.append(r"\begin{document}" + "\n")
    parts.append(r"\section*{" + title + "}" + "\n")
    parts.append(r"\begin{itemize}" + "\n")
    parts.append(r"\item PDF: " + pdf_path_text + "\n")
    parts.append(r"\item TeX: " + tex_path_text + "\n")
    parts.append(r"\item Model: " + model_text + "\n")
    parts.append(r"\item Reasoning effort: " + effort_text + "\n")
    parts.append(r"\item Chunking mode: " + chunking_mode_text + "\n")
    parts.append(r"\item Chunks completed: " + str(status["chunks_completed"]) + " / " + str(status["chunks_total"]) + "\n")
    parts.append(
        r"\item Estimated pages audited: "
        + str(status["estimated_pages_completed"])
        + " / "
        + str(status["estimated_pages_total"])
        + "\n"
    )
    parts.append(r"\item Total cost (USD): " + f"{usage['totals']['cost_usd']:.4f}" + "\n")
    parts.append(r"\item Total tokens: " + str(usage["totals"]["total_tokens"]) + "\n")
    parts.append(r"\end{itemize}" + "\n")

    parts.append(r"\section*{Ledger assumptions}" + "\n")
    parts.append(report_latex_itemize(ledger.get("assumptions", [])) + "\n")

    parts.append(r"\section*{Ledger notes}" + "\n")
    parts.append(report_latex_itemize(ledger.get("notes", [])) + "\n")

    parts.append(r"\section*{Open issues}" + "\n")
    open_issues = [x for x in issues_state["issues"] if x.get("status", "open") == "open"]
    typo_entries = _collect_typographical_issue_entries(open_issues, chunk_records)
    main_open_issues = [x for x in open_issues if not _is_pure_typographical_issue(x)]
    if not main_open_issues:
        parts.append("No open mathematical/correctness issues.\n")
    else:
        for issue in main_open_issues:
            recheck_rec = issue_recheck_overlay.get("issue_recommendations", {}).get(str(issue.get("issue_id") or ""), {})
            title_line = _report_latex_paragraph_local(f"{issue['issue_id']} -- {issue['title']} [{issue['severity']}]")
            parts.append(r"\subsection*{" + title_line + "}" + "\n")
            parts.append(r"\begin{itemize}" + "\n")
            parts.append(r"\item Chunk: " + _report_latex_paragraph_local(issue["chunk_id"]) + "\n")
            parts.append(r"\item Location: " + _report_latex_paragraph_local(issue.get("location", "")) + "\n")
            parts.append(r"\item Description: " + _report_latex_paragraph_local(issue.get("description", "")) + "\n")
            parts.append(r"\item Evidence: " + _report_latex_paragraph_local(issue.get("evidence", "")) + "\n")
            parts.append(r"\item Proposed fix: " + _report_latex_paragraph_local(issue.get("proposed_fix", "")) + "\n")
            tag_text = ", ".join(issue.get("tags", [])) if issue.get("tags") else "none"
            parts.append(r"\item Tags: " + _report_latex_paragraph_local(tag_text) + "\n")
            for item in _issue_recheck_tex_items(issue, recheck_rec):
                parts.append(item + "\n")
            parts.append(r"\end{itemize}" + "\n")

    parts.append(_typographical_errors_tex(typo_entries) + "\n")

    parts.append(r"\section*{Chunk overview}" + "\n")
    if not chunk_records:
        parts.append("No chunk records found.\n")
    else:
        for rec in chunk_records:
            try:
                audit = _coerce_audit_payload(load_json(rec["structured_response_path"]))
            except Exception as e:
                heading = _report_latex_paragraph_local(
                    f"{rec.get('chunk_id','chunk')} -- {rec.get('label','(unavailable)')}"
                )
                parts.append(r"\subsection*{" + heading + "}" + "\n")
                warn = f"Could not load structured response at {rec.get('structured_response_path','(missing)')}: {e}"
                parts.append(r"\textbf{Warning:} " + _report_latex_paragraph_local(warn) + "\n\n")
                continue
            heading = _report_latex_paragraph_local(f"{rec['chunk_id']} -- {rec['label']}")
            parts.append(r"\subsection*{" + heading + "}" + "\n")
            parts.append(r"\begin{itemize}" + "\n")
            parts.append(r"\item Boundary: " + _report_latex_paragraph_local(rec["boundary"]) + "\n")
            parts.append(r"\item Estimated pages: " + str(rec["page_start"]) + "--" + str(rec["page_end"]) + "\n")
            parts.append(r"\item Cost: \$" + f"{rec['cost_usd']:.4f}" + "\n")
            parts.append(r"\item Confidence: " + _report_latex_paragraph_local(audit.get("confidence", "medium")) + "\n")
            verification_mode = str(rec.get("verification_mode", "local_python_only"))
            parts.append(r"\item Verification mode: " + _report_latex_paragraph_local(verification_mode) + "\n")
            verification_summary = rec.get("verification_summary") if isinstance(rec.get("verification_summary"), dict) else {}
            if verification_summary.get("used_code_interpreter"):
                tool_events = int(verification_summary.get("tool_event_count", 0) or 0)
                parts.append(r"\item Code Interpreter tool events: " + str(tool_events) + "\n")
                ci_files = verification_summary.get("file_ids") if isinstance(verification_summary.get("file_ids"), list) else []
                if ci_files:
                    parts.append(r"\item Code Interpreter files: " + _report_latex_paragraph_local(", ".join(ci_files[:6])) + "\n")
            parts.append(r"\end{itemize}" + "\n")

            parts.append(r"\paragraph{Assumptions and notation}" + "\n")
            parts.append(report_latex_itemize(audit.get("assumptions_and_notation", [])) + "\n")

            parts.append(r"\paragraph{Verified steps}" + "\n")
            parts.append(report_latex_itemize(audit.get("verified_steps", [])) + "\n")

            parts.append(r"\paragraph{Issues found}" + "\n")
            chunk_issues = [issue for issue in (audit.get("issues") or []) if not _is_pure_typographical_issue(issue)]
            if chunk_issues:
                for issue in chunk_issues:
                    parts.append(
                        r"\subparagraph*{"
                        + _report_latex_paragraph_local(
                            f"{issue.get('title','Untitled issue')} [{issue.get('severity','low')}]"
                        )
                        + "}"
                        + "\n"
                    )
                    parts.append(r"\begin{itemize}" + "\n")
                    parts.append(r"\item Location: " + _report_latex_paragraph_local(issue.get("location", "")) + "\n")
                    parts.append(r"\item Description: " + _report_latex_paragraph_local(issue.get("description", "")) + "\n")
                    parts.append(r"\item Evidence: " + _report_latex_paragraph_local(issue.get("evidence", "")) + "\n")
                    parts.append(r"\item Proposed fix: " + _report_latex_paragraph_local(issue.get("proposed_fix", "")) + "\n")
                    tag_text = ", ".join(issue.get("tags", [])) if issue.get("tags") else "none"
                    parts.append(r"\item Tags: " + _report_latex_paragraph_local(tag_text) + "\n")
                    parts.append(r"\end{itemize}" + "\n")
            elif audit.get("issues"):
                parts.append(
                    "No mathematical/correctness issues found in this chunk. Typographical/copyediting items are summarized separately in the Typographical errors section.\n"
                )
            else:
                parts.append("No issues found.\n")

            parts.append(r"\paragraph{Next boundary hint}" + "\n")
            parts.append(_report_latex_paragraph_local(audit.get("next_boundary_hint", "None.")) + "\n\n")

            python_entries = _python_check_entries_for_record(session, rec, audit)
            if python_entries:
                parts.append(r"\paragraph{Suggested local Python checks}" + "\n")
                for entry in python_entries:
                    parts.append(
                        r"\textbf{"
                        + _report_latex_paragraph_local(entry.get("purpose", "Python check"))
                        + "}"
                        + "\n"
                    )
                    parts.append(
                        _report_latex_paragraph_local(entry.get("description", entry.get("purpose", "Python check")))
                        + "\n"
                    )
                    parts.append(
                        r"\textit{Expected outcome: }"
                        + _report_latex_paragraph_local(entry.get("expected_outcome", ""))
                        + "\n"
                    )
                    parts.append(
                        r"\textit{Script file: }\texttt{"
                        + _report_latex_paragraph_local(entry.get("display_path", ""))
                        + "}"
                        + "\n"
                    )
                    parts.append(_verbatim_block(entry.get("code", "")) + "\n")

            if (audit.get("latex_patch") or "").strip():
                parts.append(r"\paragraph{Minimal LaTeX patch}" + "\n")
                parts.append(_verbatim_block(audit["latex_patch"]) + "\n")

    parts.append(r"\end{document}" + "\n")
    return _strip_unsafe_control_chars("".join(parts))


def _timing_summary_markdown(session: dict[str, Any]) -> str:
    _ensure_timing_state(session)
    usage = load_usage(session)
    lines = [
        "## Timing summary",
        "",
        f"- Total active audit time: {format_duration(usage['totals'].get('audit_seconds', 0.0))}",
        "",
    ]
    per_chunk = usage.get("per_chunk", [])
    if per_chunk:
        for entry in per_chunk:
            lines.append(f"- {entry.get('chunk_id','chunk')}: {format_duration(entry.get('elapsed_seconds', 0.0))}")
    else:
        lines.append("- No completed chunks yet.")
    lines.append("")
    return "\n".join(lines)


def _timing_summary_tex(session: dict[str, Any]) -> str:
    _ensure_timing_state(session)
    usage = load_usage(session)
    parts = [
        r"\section*{Timing summary}",
        r"\begin{itemize}",
        r"\item Total active audit time: " + _report_latex_paragraph_local(format_duration(usage["totals"].get("audit_seconds", 0.0))),
    ]
    per_chunk = usage.get("per_chunk", [])
    if per_chunk:
        for entry in per_chunk:
            parts.append(
                r"\item "
                + _report_latex_paragraph_local(
                    f"{entry.get('chunk_id','chunk')}: {format_duration(entry.get('elapsed_seconds', 0.0))}"
                )
            )
    else:
        parts.append(r"\item No completed chunks yet.")
    parts.append(r"\end{itemize}")
    return "\n".join(parts) + "\n"


def _compact_verification_summary_markdown(session: dict[str, Any]) -> str:
    state = load_verification_state(session)
    last_run = state.get("last_run") or {}
    results = _load_verification_results(session, state=state)
    if not last_run and not results:
        return ""
    counts = _verification_summary_counts(results) if results else {
        "scripts_total": int(last_run.get("scripts_total", 0) or 0),
        "passed": int(last_run.get("passed", 0) or 0),
        "failed": int(last_run.get("failed", 0) or 0),
        "timeout": int(last_run.get("timeout", 0) or 0),
        "skipped": int(last_run.get("skipped", 0) or 0),
    }
    lines = [
        "## Verification summary",
        "",
        f"- Currently active verification scripts run: {counts['scripts_total']}",
        f"- Passed: {counts['passed']}",
        f"- Failed: {counts['failed']}",
        f"- Timed out: {counts['timeout']}",
        f"- Skipped: {counts['skipped']}",
    ]
    failing = [item.get("script_name") for item in results if item.get("status") in {"failed", "timeout"}]
    if failing:
        lines.append(f"- Failing scripts: {', '.join(failing[:10])}")
    skipped = [item.get("script_name") for item in results if item.get("status") == "skipped"]
    if skipped:
        lines.append(f"- Skipped scripts: {', '.join(skipped[:10])}")
    warning = _verification_inventory_warning(session)
    if warning.get("has_invalidated_obligations"):
        lines.append(f"- Verification inventory warning: {warning.get('message')}")
    lines.append("")
    return "\n".join(lines)


def _compact_verification_summary_tex(session: dict[str, Any]) -> str:
    state = load_verification_state(session)
    last_run = state.get("last_run") or {}
    results = _load_verification_results(session, state=state)
    if not last_run and not results:
        return ""
    counts = _verification_summary_counts(results) if results else {
        "scripts_total": int(last_run.get("scripts_total", 0) or 0),
        "passed": int(last_run.get("passed", 0) or 0),
        "failed": int(last_run.get("failed", 0) or 0),
        "timeout": int(last_run.get("timeout", 0) or 0),
        "skipped": int(last_run.get("skipped", 0) or 0),
    }
    parts = [
        r"\section*{Verification summary}",
        r"\begin{itemize}",
        r"\item Currently active verification scripts run: " + str(counts["scripts_total"]),
        r"\item Passed: " + str(counts["passed"]),
        r"\item Failed: " + str(counts["failed"]),
        r"\item Timed out: " + str(counts["timeout"]),
        r"\item Skipped: " + str(counts["skipped"]),
    ]
    failing = [item.get("script_name") for item in results if item.get("status") in {"failed", "timeout"}]
    if failing:
        parts.append(r"\item Failing scripts: " + _report_latex_paragraph_local(", ".join(failing[:10])))
    skipped = [item.get("script_name") for item in results if item.get("status") == "skipped"]
    if skipped:
        parts.append(r"\item Skipped scripts: " + _report_latex_paragraph_local(", ".join(skipped[:10])))
    warning = _verification_inventory_warning(session)
    if warning.get("has_invalidated_obligations"):
        parts.append(
            r"\item Verification inventory warning: "
            + _report_latex_paragraph_local(str(warning.get("message") or ""))
        )
    parts.append(r"\end{itemize}")
    return "\n".join(parts) + "\n"


def _verification_counts_for_summary(session: dict[str, Any]) -> Optional[dict[str, int]]:
    try:
        state = load_verification_state(session)
        last_run = state.get("last_run") or {}
        results = _load_verification_results(session, state=state)
    except Exception:
        return None
    if not last_run and not results:
        return None
    if results:
        return _verification_summary_counts(results)
    return {
        "scripts_total": int(last_run.get("scripts_total", 0) or 0),
        "passed": int(last_run.get("passed", 0) or 0),
        "failed": int(last_run.get("failed", 0) or 0),
        "timeout": int(last_run.get("timeout", 0) or 0),
        "skipped": int(last_run.get("skipped", 0) or 0),
    }


_ISSUE_SEVERITY_SUMMARY_ORDER = ("critical", "high", "medium", "low")


def _issue_severity_summary(session: dict[str, Any], only_open_issues: bool = False) -> dict[str, Any]:
    counts = {severity: 0 for severity in _ISSUE_SEVERITY_SUMMARY_ORDER}
    unknown = 0
    try:
        issues_state = load_issues(session)
        issues = list(issues_state.get("issues", []) or [])
    except Exception:
        issues = []
    if only_open_issues:
        issues = [
            issue
            for issue in issues
            if normalize_whitespace(str(issue.get("status", "open") or "open")).lower() == "open"
        ]
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        severity = normalize_whitespace(str(issue.get("severity") or "")).lower()
        if severity in counts:
            counts[severity] += 1
        else:
            unknown += 1
    total = sum(counts.values()) + unknown
    return {
        "only_open_issues": bool(only_open_issues),
        "counts": counts,
        "unknown": unknown,
        "total": total,
    }


def _audit_summary_items(
    session: dict[str, Any],
    include_verification_summary: bool = True,
    issue_summary_open_only: bool = False,
) -> list[tuple[str, str]]:
    _ensure_timing_state(session)
    try:
        status = load_status(session)
    except Exception:
        status = {}
    try:
        usage = load_usage(session)
    except Exception:
        usage = {}
    try:
        manifest = load_manifest(session)
    except Exception:
        manifest = {}

    totals = usage.get("totals", {}) if isinstance(usage, dict) else {}

    def present(value: Any) -> str:
        text = normalize_whitespace(str(value or ""))
        return text or "not available"

    cost = totals.get("cost_usd", status.get("cost_usd"))
    audit_seconds = totals.get("audit_seconds", status.get("total_audit_seconds"))
    items = [
        ("PDF", present(session.get("pdf_path"))),
        ("Session/workdir", present(session.get("workdir"))),
        ("Model", present(session.get("model"))),
        ("Reasoning effort", present(session.get("reasoning_effort"))),
        ("Audit status", present(status.get("status"))),
        ("Chunking mode", present(manifest.get("chunking_mode"))),
        ("Chunks completed / total", f"{status.get('chunks_completed', 'not available')} / {status.get('chunks_total', 'not available')}"),
        (
            "Estimated pages completed / total",
            f"{status.get('estimated_pages_completed', 'not available')} / {status.get('estimated_pages_total', 'not available')}",
        ),
        ("Total cost (USD)", f"{float(cost or 0.0):.4f}" if cost is not None else "not available"),
        ("Total tokens", present(totals.get("total_tokens"))),
        ("Total audit time", format_duration(audit_seconds) if audit_seconds is not None else "not available"),
        ("Audit started at", present(status.get("audit_started_at") or session.get("audit_started_at"))),
        ("Audit finished at", present(status.get("audit_finished_at") or session.get("audit_finished_at"))),
    ]
    pause_reason = present(status.get("pause_reason"))
    if str(status.get("status") or "").strip().lower() in {"paused", "failed"} and pause_reason != "not available":
        items.append(("Pause reason", pause_reason))

    severity_summary = _issue_severity_summary(session, only_open_issues=issue_summary_open_only)
    counts = severity_summary["counts"]
    if issue_summary_open_only:
        items.append(("Open issue severity summary", "open issues only"))
    else:
        items.append(("Issue severity summary", "all saved issues"))
    for severity in _ISSUE_SEVERITY_SUMMARY_ORDER:
        items.append((severity.title(), str(counts.get(severity, 0))))
    if severity_summary.get("unknown"):
        items.append(("Unknown severity", str(severity_summary["unknown"])))
    total_label = "Total open issues" if issue_summary_open_only else "Total issues"
    items.append((total_label, str(severity_summary["total"])))
    recheck_overlay = _build_issue_recheck_overlay(session)
    recheck_summary = recheck_overlay.get("issue_recheck_summary") or {}
    if recheck_overlay.get("recheck_applied"):
        downstream_count = int(recheck_summary.get("downstream_covered_issue_count", 0) or 0)
        family_count = int(recheck_summary.get("family_count", 0) or 0)
        items.append(
            (
                "Issue recheck overlay",
                (
                    f"{family_count} accepted issue-family recheck(s) applied; "
                    f"{downstream_count} downstream-covered issue(s) are grouped in report treatment. "
                    "Canonical issue records and severity counts are unchanged."
                ),
            )
        )

    verification_counts = _verification_counts_for_summary(session) if include_verification_summary else None
    if verification_counts:
        items.extend(
            [
                ("Currently active verification scripts total", str(verification_counts.get("scripts_total", 0))),
                ("Verification passed", str(verification_counts.get("passed", 0))),
                ("Verification failed", str(verification_counts.get("failed", 0))),
                ("Verification timed out", str(verification_counts.get("timeout", 0))),
                ("Verification skipped", str(verification_counts.get("skipped", 0))),
            ]
        )
        verification_warning = _verification_inventory_warning(session)
        if verification_warning.get("has_invalidated_obligations"):
            items.append(("Verification inventory warning", str(verification_warning.get("message") or "")))
    return items


def _audit_summary_markdown(
    session: dict[str, Any],
    include_verification_summary: bool = True,
    issue_summary_open_only: bool = False,
) -> str:
    lines = ["## Audit summary", ""]
    for label, value in _audit_summary_items(
        session,
        include_verification_summary=include_verification_summary,
        issue_summary_open_only=issue_summary_open_only,
    ):
        lines.append(f"- {label}: {normalize_math_delimiters(value)}")
    lines.append("")
    return "\n".join(lines)


def _audit_summary_tex(
    session: dict[str, Any],
    include_verification_summary: bool = True,
    issue_summary_open_only: bool = False,
) -> str:
    parts = [
        r"\phantomsection",
        r"\addcontentsline{toc}{section}{Audit summary}",
        r"\section*{Audit summary}",
        r"\begin{itemize}",
    ]
    for label, value in _audit_summary_items(
        session,
        include_verification_summary=include_verification_summary,
        issue_summary_open_only=issue_summary_open_only,
    ):
        parts.append(r"\item " + _report_latex_paragraph_local(f"{label}: {value}"))
    parts.append(r"\end{itemize}")
    return "\n".join(parts) + "\n"


def _markdown_anchor(title: str, seen: Optional[dict[str, int]] = None) -> str:
    text = normalize_math_delimiters(str(title or "")).strip().lower()
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "-", text).strip("-")
    base = text or "section"
    if seen is None:
        return base
    count = seen.get(base, 0)
    seen[base] = count + 1
    return base if count == 0 else f"{base}-{count}"


def _markdown_toc(text: str) -> str:
    seen: dict[str, int] = {}
    entries = [("Audit summary", _markdown_anchor("Audit summary", seen))]
    for line in str(text or "").splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if not match:
            continue
        title = match.group(1).strip().strip("#").strip()
        if not title or title.lower() in {"table of contents", "audit summary"}:
            continue
        entries.append((title, _markdown_anchor(title, seen)))
    lines = ["## Table of contents", ""]
    for title, anchor in entries:
        lines.append(f"- [{title}](#{anchor})")
    lines.append("")
    return "\n".join(lines)


def _insert_markdown_report_front_matter(
    text: str,
    session: dict[str, Any],
    include_audit_summary: bool = True,
    include_verification_summary: bool = True,
    issue_summary_open_only: bool = False,
) -> str:
    text = str(text or "").strip() + "\n"
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "):
        body = text.strip()
        front_parts = [_markdown_toc(body).strip()]
        if include_audit_summary:
            front_parts.append(
                _audit_summary_markdown(
                    session,
                    include_verification_summary=include_verification_summary,
                    issue_summary_open_only=issue_summary_open_only,
                ).strip()
            )
        return ("\n\n".join(part for part in front_parts if part) + "\n\n" + body).strip() + "\n"

    body_start = 1
    if body_start < len(lines) and not lines[body_start].strip():
        body_start += 1
    metadata_start = body_start
    while body_start < len(lines) and lines[body_start].startswith("- "):
        body_start += 1
    if body_start > metadata_start and body_start < len(lines) and not lines[body_start].strip():
        body_start += 1

    if "\n## Table of contents\n" in text and "\n## Audit summary\n" in text:
        if body_start > metadata_start:
            return (lines[0].rstrip() + "\n\n" + "\n".join(lines[body_start:]).strip()).strip() + "\n"
        return text

    header = lines[0].rstrip()
    body = "\n".join(lines[body_start:]).strip()
    front_parts = [_markdown_toc(body).strip()]
    if include_audit_summary:
        front_parts.append(
            _audit_summary_markdown(
                session,
                include_verification_summary=include_verification_summary,
                issue_summary_open_only=issue_summary_open_only,
            ).strip()
        )
    front_matter = "\n\n".join(part for part in front_parts if part)
    return (header + "\n\n" + front_matter + "\n\n" + body).strip() + "\n"


def _tex_add_toc_entries_for_starred_sections(text: str) -> str:
    section_re = re.compile(r"(\\section\*\{([^{}\n]*)\})")
    first_section_seen = False

    def repl(match: re.Match[str]) -> str:
        nonlocal first_section_seen
        section_cmd = match.group(1)
        title = match.group(2).strip()
        if not first_section_seen:
            first_section_seen = True
            return section_cmd
        if title == "Audit summary":
            return section_cmd
        prefix = match.string[max(0, match.start() - 120) : match.start()]
        if r"\addcontentsline{toc}{section}" in prefix:
            return section_cmd
        return "\n".join(
            [
                r"\phantomsection",
                r"\addcontentsline{toc}{section}{" + title + "}",
                section_cmd,
            ]
        )

    return section_re.sub(repl, text)


def _insert_tex_report_front_matter(
    text: str,
    session: dict[str, Any],
    include_audit_summary: bool = True,
    include_verification_summary: bool = True,
    issue_summary_open_only: bool = False,
) -> str:
    text = str(text or "")
    if r"\tableofcontents" in text and r"\section*{Audit summary}" in text:
        begin = re.search(r"\\begin\{document\}\s*", text)
        if begin:
            title_section = re.search(r"\\section\*\{[^{}\n]*\}", text[begin.end() :])
            if title_section:
                title_end = begin.end() + title_section.end()
                title_itemize = re.match(r"\s*\\begin\{itemize\}.*?\\end\{itemize\}\s*", text[title_end:], flags=re.DOTALL)
                if title_itemize and r"\tableofcontents" in text[title_end + title_itemize.end() : title_end + title_itemize.end() + 300]:
                    text = text[:title_end].rstrip() + "\n\n" + text[title_end + title_itemize.end() :].lstrip()
        return _tex_add_toc_entries_for_starred_sections(text)

    front_lines = [r"\tableofcontents", r"\clearpage"]
    if include_audit_summary:
        front_lines.extend(
            [
                _audit_summary_tex(
                    session,
                    include_verification_summary=include_verification_summary,
                    issue_summary_open_only=issue_summary_open_only,
                ).strip(),
                r"\clearpage",
            ]
        )
    front_lines.append("")
    front_matter = "\n".join(front_lines)
    begin = re.search(r"\\begin\{document\}\s*", text)
    if not begin:
        return _tex_add_toc_entries_for_starred_sections(front_matter + "\n" + text)

    insert_at = begin.end()
    suffix_start = insert_at
    title_section = re.search(r"\\section\*\{[^{}\n]*\}", text[insert_at:])
    if title_section:
        title_end = insert_at + title_section.end()
        insert_at = title_end
        suffix_start = title_end
        title_itemize = re.match(r"\s*\\begin\{itemize\}.*?\\end\{itemize\}\s*", text[title_end:], flags=re.DOTALL)
        if title_itemize and title_itemize.end() < 2500:
            suffix_start = title_end + title_itemize.end()

    updated = text[:insert_at].rstrip() + "\n\n" + front_matter + text[suffix_start:].lstrip()
    return _tex_add_toc_entries_for_starred_sections(updated)


_OLD_BUILD_FINAL_REPORT_MARKDOWN_WITH_TIMING = _build_final_report_markdown_base
_OLD_BUILD_FINAL_REPORT_TEX_WITH_TIMING = _build_final_report_tex_base


def build_final_report_markdown(session: dict[str, Any], report_title: Optional[str] = None) -> str:
    _ensure_timing_state(session)
    base = _OLD_BUILD_FINAL_REPORT_MARKDOWN_WITH_TIMING(session, report_title=report_title)
    usage = load_usage(session)
    total_line = f"- Total active audit time: {format_duration(usage['totals'].get('audit_seconds', 0.0))}"
    if "- Total tokens:" in base and total_line not in base:
        base = base.replace(
            "- Total tokens: " + str(usage["totals"]["total_tokens"]),
            "- Total tokens: " + str(usage["totals"]["total_tokens"]) + "\n" + total_line,
            1,
        )
    return base.rstrip() + "\n\n" + _timing_summary_markdown(session)


def build_final_report_tex(session: dict[str, Any], report_title: Optional[str] = None) -> str:
    _ensure_timing_state(session)
    base = _OLD_BUILD_FINAL_REPORT_TEX_WITH_TIMING(session, report_title=report_title)
    usage = load_usage(session)
    total_line = r"\item Total active audit time: " + _report_latex_paragraph_local(
        format_duration(usage["totals"].get("audit_seconds", 0.0))
    )
    if total_line not in base:
        m = re.search(r"(\\item Total tokens: .*?\n)", base)
        if m:
            base = base[: m.end()] + total_line + "\n" + base[m.end() :]
        else:
            base = base.replace(r"\end{document}", total_line + "\n" + r"\end{document}", 1)
    timing_section = _timing_summary_tex(session)
    if r"\end{document}" in base:
        base = base.replace(r"\end{document}", timing_section + r"\end{document}", 1)
    else:
        base = base + "\n" + timing_section
    return base


_old_build_final_report_markdown_with_local_verification = build_final_report_markdown
_old_build_final_report_tex_with_local_verification = build_final_report_tex


def build_final_report_markdown(session: dict[str, Any], report_title: Optional[str] = None) -> str:  # type: ignore[no-redef]
    text = _old_build_final_report_markdown_with_local_verification(session, report_title=report_title)
    if not session.get("include_verification_summary_in_final_report", True):
        return text
    summary = _compact_verification_summary_markdown(session)
    if not summary:
        return text
    return text.rstrip() + "\n\n" + summary.strip() + "\n"


def build_final_report_tex(session: dict[str, Any], report_title: Optional[str] = None) -> str:  # type: ignore[no-redef]
    text = _old_build_final_report_tex_with_local_verification(session, report_title=report_title)
    if not session.get("include_verification_summary_in_final_report", True):
        return text
    summary = _compact_verification_summary_tex(session)
    if not summary:
        return text
    if r"\end{document}" in text:
        return text.replace(r"\end{document}", summary + r"\end{document}", 1)
    return text + "\n" + summary


_OLD_BUILD_FINAL_REPORT_MARKDOWN_WITH_REFERENCE_STYLE = build_final_report_markdown
_OLD_BUILD_FINAL_REPORT_TEX_WITH_REFERENCE_STYLE = build_final_report_tex


def _effective_report_reference_style(session: dict[str, Any], style: Optional[str] = None) -> str:
    requested = _normalize_report_reference_style(style or session.get("report_reference_style", "match_audit"))
    if requested != "match_audit":
        return requested
    ref_state = ensure_reference_map(session)
    if _reference_map_has_valid_aux_numbers(ref_state):
        return "compiled_pdf_numbers"
    return "match_audit"


def _reference_report_status(session: dict[str, Any], style: Optional[str] = None) -> dict[str, Any]:
    requested = _normalize_report_reference_style(style or session.get("report_reference_style", "match_audit"))
    effective = _effective_report_reference_style(session, style=requested)
    ref_state = ensure_reference_map(session)
    label_map = ref_state.get("label_map", {}) if isinstance(ref_state, dict) else {}
    map_source = str(ref_state.get("map_source") or "none") if isinstance(ref_state, dict) else "none"
    warning = normalize_whitespace(ref_state.get("warning", "")) if isinstance(ref_state, dict) else ""
    valid_aux = _reference_map_has_valid_aux_numbers(ref_state)

    lines: list[str] = []
    if requested != "source_labels":
        if label_map and effective == "compiled_pdf_numbers" and not valid_aux:
            lines = [
                "Compiled-style references in this report are using a cached or non-authoritative reference map rather than a freshly parsed AUX map.",
                f"Reference map source: {map_source}.",
                f"Recovered labels available: {len(label_map)}.",
                "Verify numbering against the current compiled PDF if the paper was recompiled after the cached map was created.",
            ]
        elif not label_map:
            lines = [
                "Compiled PDF numbering could not be applied for this report.",
                f"Reference map source: {map_source}.",
                "Fallback mode: label-based references are preserved when available; otherwise the original audit wording is kept.",
            ]
        elif effective != "compiled_pdf_numbers":
            lines = [
                "A valid AUX-derived compiled numbering map was not available, so compiled numbering was not applied automatically in this report.",
                f"Reference map source: {map_source}.",
                "Fallback mode: label-based references are preserved when available; otherwise the original audit wording is kept.",
            ]
    if warning:
        lines.append(f"Warning: {warning}")
    return {
        "requested_style": requested,
        "effective_style": effective,
        "map_source": map_source,
        "label_count": len(label_map) if isinstance(label_map, dict) else 0,
        "lines": lines,
    }


def _reference_report_status_markdown(session: dict[str, Any], style: Optional[str] = None) -> str:
    lines = _reference_report_status(session, style=style).get("lines") or []
    if not lines:
        return ""
    return "## Reference numbering status\n\n" + "\n".join(f"- {line}" for line in lines) + "\n\n"


def _reference_report_status_tex(session: dict[str, Any], style: Optional[str] = None) -> str:
    lines = _reference_report_status(session, style=style).get("lines") or []
    if not lines:
        return ""
    parts = [r"\section*{Reference numbering status}", r"\begin{itemize}"]
    for line in lines:
        parts.append(r"\item " + _report_latex_paragraph_local(line))
    parts.append(r"\end{itemize}")
    return "\n".join(parts) + "\n"


def _inject_markdown_reference_status(text: str, block: str) -> str:
    if not block:
        return text
    m = re.search(r"^##\s", text, flags=re.MULTILINE)
    if m:
        return text[: m.start()] + block + text[m.start() :]
    return text.rstrip() + "\n\n" + block


def _inject_tex_reference_status(text: str, block: str) -> str:
    if not block:
        return text
    m = re.search(r"\\end\{itemize\}\n", text)
    if m:
        return text[: m.end()] + block + text[m.end() :]
    marker = r"\section*{Ledger assumptions}"
    if marker in text:
        return text.replace(marker, block + marker, 1)
    if r"\end{document}" in text:
        return text.replace(r"\end{document}", block + r"\end{document}", 1)
    return text + "\n" + block


def _reference_rewrite_maps(session: dict[str, Any]) -> tuple[dict[str, dict[str, str]], dict[tuple[str, str], str]]:
    ref_state = ensure_reference_map(session)
    label_map = ref_state.get("label_map", {}) if isinstance(ref_state, dict) else {}
    reverse: dict[tuple[str, str], list[str]] = {}
    for label, info in label_map.items():
        kind = (info.get("kind") or _infer_kind_from_label(label)).strip().lower()
        number = (info.get("number") or "").strip()
        if not kind or not number:
            continue
        reverse.setdefault((kind, number), []).append(label)
    unique_reverse = {key: labels[0] for key, labels in reverse.items() if len(labels) == 1}
    return label_map, unique_reverse


def _reference_phrase_from_match(match_text: str, style: str, kind: str, label: str, display: str) -> str:
    style = _normalize_report_reference_style(style)
    starts_upper = bool(match_text[:1]) and match_text[:1].isupper()
    has_the = match_text.lower().startswith("the ")
    if style == "compiled_pdf_numbers":
        phrase = display.strip() or match_text
        if has_the and phrase[:1].islower():
            phrase = "the " + phrase
        if starts_upper and phrase[:1].islower():
            phrase = phrase[0].upper() + phrase[1:]
        return phrase
    base_kind = (kind or _infer_kind_from_label(label)).strip().lower() or "item"
    phrase = f"{base_kind} labeled {label}"
    if has_the:
        phrase = "the " + phrase
    if starts_upper:
        phrase = phrase[0].upper() + phrase[1:]
    return phrase


def _rewrite_reference_mentions_in_prose(text: str, session: dict[str, Any], style: Optional[str] = None) -> str:
    style = _effective_report_reference_style(session, style=style)
    if style == "match_audit":
        return text
    label_map, reverse_map = _reference_rewrite_maps(session)
    if not label_map:
        return text

    rewritten = str(text)
    if style == "compiled_pdf_numbers":
        items = sorted(label_map.items(), key=lambda item: len(item[0]), reverse=True)
        for label, info in items:
            display = (info.get("display") or "").strip()
            kind = (info.get("kind") or _infer_kind_from_label(label)).strip().lower() or "item"
            if not display:
                continue
            pattern = re.compile(
                rf"(?<![\w:])((?:the\s+)?{re.escape(kind)}\s+labeled\s+{re.escape(label)})\b",
                flags=re.IGNORECASE,
            )
            rewritten = pattern.sub(
                lambda m: _reference_phrase_from_match(m.group(1), style, kind, label, display),
                rewritten,
            )
        return rewritten

    items = []
    for (kind, number), label in reverse_map.items():
        display = _display_for_kind(kind, number, label)
        if display:
            items.append((display, kind, label))
    items.sort(key=lambda item: len(item[0]), reverse=True)
    for display, kind, label in items:
        pattern = re.compile(rf"(?<![\w:])((?:the\s+)?{re.escape(display)})\b", flags=re.IGNORECASE)
        rewritten = pattern.sub(
            lambda m: _reference_phrase_from_match(m.group(1), style, kind, label, display),
            rewritten,
        )
    return rewritten


def _rewrite_markdown_report_references(text: str, session: dict[str, Any], style: Optional[str] = None) -> str:
    style = _effective_report_reference_style(session, style=style)
    if style == "match_audit":
        return text
    parts = re.split(r"(```.*?```)", text, flags=re.DOTALL)
    out = []
    for part in parts:
        if part.startswith("```") and part.endswith("```"):
            out.append(part)
        else:
            out.append(_rewrite_reference_mentions_in_prose(part, session, style=style))
    return "".join(out)


def _rewrite_tex_report_references(text: str, session: dict[str, Any], style: Optional[str] = None) -> str:
    style = _effective_report_reference_style(session, style=style)
    if style == "match_audit":
        return text
    parts = re.split(r"(\\begin\{Verbatim\}.*?\\end\{Verbatim\})", text, flags=re.DOTALL)
    out = []
    for part in parts:
        if part.startswith(r"\begin{Verbatim}") and part.endswith(r"\end{Verbatim}"):
            out.append(part)
        else:
            out.append(_rewrite_reference_mentions_in_prose(part, session, style=style))
    return "".join(out)


def build_final_report_markdown(session: dict[str, Any], report_title: Optional[str] = None) -> str:  # type: ignore[no-redef]
    style = _effective_report_reference_style(session)
    text = _OLD_BUILD_FINAL_REPORT_MARKDOWN_WITH_REFERENCE_STYLE(session, report_title=report_title)
    text = _rewrite_markdown_report_references(text, session, style=style)
    text = _inject_markdown_reference_status(text, _reference_report_status_markdown(session, style=style))
    return _insert_markdown_report_front_matter(text, session)


def build_final_report_tex(session: dict[str, Any], report_title: Optional[str] = None) -> str:  # type: ignore[no-redef]
    style = _effective_report_reference_style(session)
    text = _OLD_BUILD_FINAL_REPORT_TEX_WITH_REFERENCE_STYLE(session, report_title=report_title)
    text = _rewrite_tex_report_references(text, session, style=style)
    text = _inject_tex_reference_status(text, _reference_report_status_tex(session, style=style))
    return _insert_tex_report_front_matter(text, session)


def build_final_report(
    session_or_pdf: dict[str, Any] | str | Path,
    report_title: Optional[str] = None,
    include_verification_summary_in_final_report: Optional[bool] = None,
    write_separate_verification_report: Optional[bool] = None,
    report_reference_style: Optional[str] = None,
) -> dict[str, str]:
    session = session_or_pdf if isinstance(session_or_pdf, dict) else load_session_from_pdf(session_or_pdf)
    if session is None:
        raise FileNotFoundError("No audit session found for this PDF.")
    changed = False
    if include_verification_summary_in_final_report is not None:
        session["include_verification_summary_in_final_report"] = bool(include_verification_summary_in_final_report)
        changed = True
    if write_separate_verification_report is not None:
        session["write_separate_verification_report"] = bool(write_separate_verification_report)
        changed = True
    if report_reference_style is not None:
        session["report_reference_style"] = _normalize_report_reference_style(report_reference_style)
        changed = True
    if changed:
        session["updated_at"] = utc_now()
        save_session(session)

    _ensure_timing_state(session)
    root = Path(session["workdir"])
    report_stem = Path(session["pdf_path"]).stem + "_audit_report"

    md_text = build_final_report_markdown(session, report_title=report_title)
    tex_text = build_final_report_tex(session, report_title=report_title)
    issue_recheck_overlay = _build_issue_recheck_overlay(session)

    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    md_path = reports_dir / f"{report_stem}.md"
    tex_path = reports_dir / f"{report_stem}.tex"
    json_path = reports_dir / f"{report_stem}.json"

    md_path.write_text(md_text, encoding="utf-8")
    tex_path.write_text(tex_text, encoding="utf-8")

    report_json = {
        "session": load_session_from_pdf(session["pdf_path"]),
        "status": load_status(session),
        "audit_summary": [{"label": label, "value": value} for label, value in _audit_summary_items(session)],
        "ledger": load_ledger(session),
        "issues": load_issues(session),
        "usage": load_usage(session),
        "manifest": load_manifest(session),
        "chunk_records": _read_chunk_records(session),
        "recheck_applied": bool(issue_recheck_overlay.get("recheck_applied")),
        "issue_recheck_summary": issue_recheck_overlay.get("issue_recheck_summary", {}),
        "issue_recheck_overlay": issue_recheck_overlay,
        "grouped_downstream_issues": issue_recheck_overlay.get("grouped_downstream_issues", {}),
        "generated_at": utc_now(),
    }
    save_json(json_path, report_json)

    paths = {
        "markdown": str(md_path),
        "tex": str(tex_path),
        "json": str(json_path),
    }
    if session.get("write_separate_verification_report", True):
        verification_paths = runtime_build_verification_report(session)
        if verification_paths:
            paths = dict(paths)
            paths["verification_markdown"] = verification_paths.get("markdown", "")
            paths["verification_tex"] = verification_paths.get("tex", "")
            paths["verification_json"] = verification_paths.get("json", "")
    return paths


def _resolve_concise_report_session(session_or_pdf: dict[str, Any] | str | Path) -> dict[str, Any]:
    session = session_or_pdf if isinstance(session_or_pdf, dict) else load_session_from_pdf(session_or_pdf)
    if session is None:
        raise FileNotFoundError("No audit session found for this PDF.")
    return session


def _concise_issue_sort_key(
    issue: dict[str, Any],
    chunk_map: dict[str, dict[str, Any]],
) -> tuple[int, int, str, str]:
    rec = chunk_map.get(str(issue.get("chunk_id") or "").strip()) or {}
    try:
        page_start = int(rec.get("page_start") or 10**9)
    except Exception:
        page_start = 10**9
    try:
        page_end = int(rec.get("page_end") or 10**9)
    except Exception:
        page_end = 10**9
    return (page_start, page_end, str(issue.get("chunk_id") or ""), str(issue.get("issue_id") or ""))


def _collect_concise_report_data(
    session: dict[str, Any],
    options: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    normalized_options = _normalize_concise_report_options(options)
    _ensure_timing_state(session)
    issues_state = load_issues(session)
    usage = load_usage(session)
    status = load_status(session)
    manifest = load_manifest(session)
    chunk_records = _read_chunk_records(session)
    chunk_map = _chunk_record_map(chunk_records)
    issue_recheck_overlay = _build_issue_recheck_overlay(session)

    candidate_issues = list(issues_state.get("issues", []) or [])
    if normalized_options.get("only_open_issues", True):
        candidate_issues = [
            issue
            for issue in candidate_issues
            if normalize_whitespace(str(issue.get("status", "open") or "open")).lower() == "open"
        ]
    selected_issues = [
        issue
        for issue in candidate_issues
        if _is_concise_selected_issue(issue, normalized_options) and not _is_pure_typographical_issue(issue)
        and not _issue_recheck_is_downstream_covered(issue, issue_recheck_overlay)
    ]
    selected_issues.sort(key=lambda issue: _concise_issue_sort_key(issue, chunk_map))
    selected_issue_entries = [_issue_report_entry(issue, chunk_map, issue_recheck_overlay) for issue in selected_issues]
    selected_issue_ids = {str(issue.get("issue_id") or "") for issue in selected_issues}
    notable_entries = _collect_notable_proof_reference_issue_entries(
        candidate_issues,
        chunk_records,
        excluded_issue_ids=selected_issue_ids,
        issue_recheck_overlay=issue_recheck_overlay,
    )
    notable_issues = [entry["issue"] for entry in notable_entries]
    typographical_entries = (
        _collect_typographical_issue_entries(candidate_issues, chunk_records)
        if normalized_options.get("include_typographical_issues", True)
        else []
    )

    return {
        "options": normalized_options,
        "session": session,
        "ledger": load_ledger(session),
        "issues_state": issues_state,
        "usage": usage,
        "status": status,
        "manifest": manifest,
        "chunk_records": chunk_records,
        "main_issues": selected_issues,
        "main_issue_entries": selected_issue_entries,
        "high_issues": selected_issues,
        "high_issue_entries": selected_issue_entries,
        "notable_proof_reference_issues": notable_issues,
        "notable_proof_reference_entries": notable_entries,
        "typographical_entries": typographical_entries,
        "issue_recheck_overlay": issue_recheck_overlay,
    }


def _concise_report_title(session: dict[str, Any], report_title: Optional[str] = None) -> str:
    return report_title or f"Concise audit report -- {Path(session['pdf_path']).stem}"


def _concise_option_enabled_severity_text(options: dict[str, Any]) -> str:
    severities = [severity for severity in ["critical", "high", "medium", "low"] if options.get(f"include_{severity}")]
    if not severities:
        return "none"
    return ", ".join(severities)


def _is_strict_concise_options(options: dict[str, Any]) -> bool:
    strict = default_concise_report_options()
    return all(options.get(key) == strict.get(key) for key in strict if key != "preset")


def _concise_mode_description(options: dict[str, Any]) -> str:
    if _is_strict_concise_options(options):
        return "high-priority mathematical/correctness issues, notable incorrect/circular reference medium issues, plus all typographical/copyediting issues"
    issue_text = f"mathematical/correctness issues with severity: {_concise_option_enabled_severity_text(options)}"
    if options.get("only_open_issues", True):
        issue_text = "open " + issue_text
    typo_text = "plus typographical/copyediting issues" if options.get("include_typographical_issues") else "excluding typographical/copyediting issues"
    return f"{issue_text}, {typo_text}"


def _concise_metadata_items(data: dict[str, Any]) -> list[tuple[str, str]]:
    session = data["session"]
    usage = data["usage"]
    status = data["status"]
    manifest = data["manifest"]
    totals = usage.get("totals", {}) if isinstance(usage, dict) else {}
    return [
        ("PDF", str(session.get("pdf_path") or "")),
        ("TeX", str(session.get("tex_path") or "not found")),
        ("Model", str(session.get("model") or "")),
        ("Reasoning effort", str(session.get("reasoning_effort") or "")),
        ("Chunking mode", str(manifest.get("chunking_mode") or "")),
        ("Chunks completed", f"{status.get('chunks_completed', 0)} / {status.get('chunks_total', 0)}"),
        (
            "Estimated pages audited",
            f"{status.get('estimated_pages_completed', 0)} / {status.get('estimated_pages_total', 0)}",
        ),
        ("Total cost (USD)", f"{float(totals.get('cost_usd', 0.0) or 0.0):.4f}"),
        ("Total tokens", str(totals.get("total_tokens", 0))),
        ("Total active audit time", format_duration(totals.get("audit_seconds", 0.0))),
        ("Concise mode", _concise_mode_description(data.get("options") or default_concise_report_options())),
    ]


def _concise_main_issue_section_title(options: dict[str, Any]) -> str:
    if _is_strict_concise_options(options):
        return "High-priority mathematical/correctness issues"
    return "Selected mathematical/correctness issues"


def _concise_empty_main_issue_text(options: dict[str, Any]) -> str:
    if _is_strict_concise_options(options):
        return "No open high-priority mathematical/correctness issues."
    state_text = "open " if options.get("only_open_issues", True) else ""
    return f"No {state_text}mathematical/correctness issues matched the selected concise-report options."


def _high_priority_issues_markdown(entries: list[dict[str, Any]], options: Optional[dict[str, Any]] = None) -> str:
    normalized_options = _normalize_concise_report_options(options)
    lines = [f"## {_concise_main_issue_section_title(normalized_options)}", ""]
    if not entries:
        lines.append(f"- {_concise_empty_main_issue_text(normalized_options)}")
        lines.append("")
        return "\n".join(lines)
    for entry in entries:
        issue = entry["issue"]
        lines.extend(
            [
                f"### {issue.get('issue_id', 'issue')} — {normalize_math_delimiters(issue.get('title', 'Untitled issue'))} [{issue.get('severity', 'high')}]",
                f"- {entry.get('location_text_md') or ('Chunk: ' + str(issue.get('chunk_id', '')))}",
                f"- Location detail: {normalize_math_delimiters(entry.get('location_detail') or issue.get('location', ''))}",
                f"- Description: {normalize_math_delimiters(issue.get('description', ''))}",
                f"- Evidence: {normalize_math_delimiters(issue.get('evidence', ''))}",
                f"- Proposed fix: {normalize_math_delimiters(issue.get('proposed_fix', ''))}",
                f"- Tags: {', '.join(issue.get('tags', [])) if issue.get('tags') else 'none'}",
            ]
        )
        lines.extend(_issue_recheck_markdown_lines(issue, entry.get("recheck")))
        lines.append("")
    return "\n".join(lines)


def _high_priority_issues_tex(entries: list[dict[str, Any]], options: Optional[dict[str, Any]] = None) -> str:
    normalized_options = _normalize_concise_report_options(options)
    parts = [r"\section*{" + _report_latex_paragraph_local(_concise_main_issue_section_title(normalized_options)) + "}"]
    if not entries:
        parts.append(_report_latex_paragraph_local(_concise_empty_main_issue_text(normalized_options)))
        return "\n".join(parts) + "\n"
    for entry in entries:
        issue = entry["issue"]
        title = _report_latex_paragraph_local(
            f"{issue.get('issue_id', 'issue')} -- {issue.get('title', 'Untitled issue')} [{issue.get('severity', 'high')}]"
        )
        parts.append(r"\subsection*{" + title + "}")
        parts.append(r"\begin{itemize}")
        parts.append(
            r"\item "
            + _report_latex_paragraph_local(entry.get("location_text_tex") or ("Chunk: " + str(issue.get("chunk_id", ""))))
        )
        parts.append(
            r"\item Location detail: "
            + _report_latex_paragraph_local(entry.get("location_detail") or issue.get("location", ""))
        )
        parts.append(r"\item Description: " + _report_latex_paragraph_local(issue.get("description", "")))
        parts.append(r"\item Evidence: " + _report_latex_paragraph_local(issue.get("evidence", "")))
        parts.append(r"\item Proposed fix: " + _report_latex_paragraph_local(issue.get("proposed_fix", "")))
        tag_text = ", ".join(issue.get("tags", [])) if issue.get("tags") else "none"
        parts.append(r"\item Tags: " + _report_latex_paragraph_local(tag_text))
        parts.extend(_issue_recheck_tex_items(issue, entry.get("recheck")))
        parts.append(r"\end{itemize}")
    return "\n".join(parts) + "\n"


def _notable_proof_reference_issues_markdown(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    lines = [f"## {_NOTABLE_PROOF_REFERENCE_SECTION_TITLE}", ""]
    for entry in entries:
        issue = entry["issue"]
        lines.extend(
            [
                f"### {issue.get('issue_id', 'issue')} — {normalize_math_delimiters(issue.get('title', 'Untitled issue'))} [{issue.get('severity', 'medium')}]",
                f"- {entry.get('location_text_md') or ('Chunk: ' + str(issue.get('chunk_id', '')))}",
                f"- Location detail: {normalize_math_delimiters(entry.get('location_detail') or issue.get('location', ''))}",
                f"- Description: {normalize_math_delimiters(issue.get('description', ''))}",
                f"- Evidence: {normalize_math_delimiters(issue.get('evidence', ''))}",
                f"- Proposed fix: {normalize_math_delimiters(issue.get('proposed_fix', ''))}",
                f"- Tags: {', '.join(issue.get('tags', [])) if issue.get('tags') else 'none'}",
            ]
        )
        lines.extend(_issue_recheck_markdown_lines(issue, entry.get("recheck")))
        lines.append("")
    return "\n".join(lines)


def _notable_proof_reference_issues_tex(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    parts = [r"\section*{" + _report_latex_paragraph_local(_NOTABLE_PROOF_REFERENCE_SECTION_TITLE) + "}"]
    for entry in entries:
        issue = entry["issue"]
        title = _report_latex_paragraph_local(
            f"{issue.get('issue_id', 'issue')} -- {issue.get('title', 'Untitled issue')} [{issue.get('severity', 'medium')}]"
        )
        parts.append(r"\subsection*{" + title + "}")
        parts.append(r"\begin{itemize}")
        parts.append(
            r"\item "
            + _report_latex_paragraph_local(entry.get("location_text_tex") or ("Chunk: " + str(issue.get("chunk_id", ""))))
        )
        parts.append(
            r"\item Location detail: "
            + _report_latex_paragraph_local(entry.get("location_detail") or issue.get("location", ""))
        )
        parts.append(r"\item Description: " + _report_latex_paragraph_local(issue.get("description", "")))
        parts.append(r"\item Evidence: " + _report_latex_paragraph_local(issue.get("evidence", "")))
        parts.append(r"\item Proposed fix: " + _report_latex_paragraph_local(issue.get("proposed_fix", "")))
        tag_text = ", ".join(issue.get("tags", [])) if issue.get("tags") else "none"
        parts.append(r"\item Tags: " + _report_latex_paragraph_local(tag_text))
        parts.extend(_issue_recheck_tex_items(issue, entry.get("recheck")))
        parts.append(r"\end{itemize}")
    return "\n".join(parts) + "\n"


def _concise_omitted_material_text(options: dict[str, Any]) -> str:
    if _is_strict_concise_options(options):
        return (
            "Routine verified steps, successful-check narrative, per-chunk overview material, "
            "suggested Python-check details, and non-high mathematical/correctness issues not selected "
            "as notable incorrect or circular references are "
            "intentionally omitted from this concise report."
        )
    omitted_parts = [
        "Routine verified steps, successful-check narrative, per-chunk overview material, and suggested Python-check details are intentionally omitted from this concise report."
    ]
    severities = _concise_option_enabled_severity_text(options)
    omitted_parts.append(f"The main issue section includes severities: {severities}.")
    if options.get("only_open_issues", True):
        omitted_parts.append("Closed/resolved issues are omitted.")
    if not options.get("include_typographical_issues", True):
        omitted_parts.append("Typographical/copyediting issues are omitted.")
    return " ".join(omitted_parts)


def _concise_selection_rules(options: dict[str, Any]) -> dict[str, str]:
    state = "open " if options.get("only_open_issues", True) else ""
    severities = _concise_option_enabled_severity_text(options)
    typo_rule = (
        f"all {state}pure typographical/copyediting issues, regardless of severity"
        if options.get("include_typographical_issues", True)
        else "typographical/copyediting issues omitted"
    )
    omitted = (
        "routine verified steps, successful-check narrative, per-chunk overview material, and suggested Python-check details"
    )
    if options.get("include_omitted_material_note", True):
        omitted += " described in the Omitted material section"
    else:
        omitted += " omitted without a separate note"
    return {
        "main_issues": (
            f"{state}issues with normalized severity in {{{severities}}}, excluding pure typographical/copyediting issues"
        ),
        "notable_incorrect_or_circular_references": (
            f"up to {_NOTABLE_PROOF_REFERENCE_MAX_ISSUES} {state}medium non-typographical issues selected only when they concern wrong, misleading, mislabeled, missing, self-referential, or circular references/citations to formulas, equations, identities, lemmas, theorems, propositions, definitions, remarks, or sections; issues already included in main_issues are not duplicated"
        ),
        "notable_proof_reference_and_dependency_issues": (
            "backward-compatible alias for notable_incorrect_or_circular_references"
        ),
        "typographical_errors": typo_rule,
        "audit_summary": "included" if options.get("include_audit_summary", True) else "omitted",
        "verification_summary": (
            "included in audit summary"
            if options.get("include_audit_summary", True) and options.get("include_verification_summary", True)
            else "omitted"
        ),
        "omitted": omitted,
    }


def build_concise_report_markdown(
    session_or_pdf: dict[str, Any] | str | Path,
    report_title: Optional[str] = None,
    options: Optional[dict[str, Any]] = None,
) -> str:
    session = _resolve_concise_report_session(session_or_pdf)
    data = _collect_concise_report_data(session, options=options)
    normalized_options = data["options"]
    title = _concise_report_title(session, report_title=report_title)

    lines = [f"# {title}", ""]
    for label, value in _concise_metadata_items(data):
        lines.append(f"- {label}: {normalize_math_delimiters(value)}")
    lines.extend(
        [
            "",
            _high_priority_issues_markdown(data["main_issue_entries"], normalized_options).rstrip(),
            "",
        ]
    )
    notable_markdown = _notable_proof_reference_issues_markdown(data["notable_proof_reference_entries"]).rstrip()
    if notable_markdown:
        lines.extend([notable_markdown, ""])
    if normalized_options.get("include_typographical_issues", True):
        lines.extend([_typographical_errors_markdown(data["typographical_entries"]).rstrip(), ""])
    if normalized_options.get("include_omitted_material_note", True):
        lines.extend(["## Omitted material", "", _concise_omitted_material_text(normalized_options), ""])
    text = "\n".join(lines).strip() + "\n"
    style = _effective_report_reference_style(session)
    text = _rewrite_markdown_report_references(text, session, style=style)
    text = _inject_markdown_reference_status(text, _reference_report_status_markdown(session, style=style))
    return _insert_markdown_report_front_matter(
        text,
        session,
        include_audit_summary=normalized_options.get("include_audit_summary", True),
        include_verification_summary=normalized_options.get("include_verification_summary", True),
        issue_summary_open_only=normalized_options.get("only_open_issues", True),
    ).rstrip() + "\n"


def build_concise_report_tex(
    session_or_pdf: dict[str, Any] | str | Path,
    report_title: Optional[str] = None,
    options: Optional[dict[str, Any]] = None,
) -> str:
    session = _resolve_concise_report_session(session_or_pdf)
    data = _collect_concise_report_data(session, options=options)
    normalized_options = data["options"]
    title = _report_latex_paragraph_local(_concise_report_title(session, report_title=report_title))
    imported_preamble = _extract_safe_report_preamble(session.get("tex_path"))

    parts = [
        r"""\documentclass[11pt]{article}
\usepackage[a4paper,margin=1in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{microtype}
\usepackage{amsmath,amssymb,mathtools}
\usepackage{hyperref}
\usepackage{enumitem}
\usepackage{longtable}
\usepackage{booktabs}
\usepackage{xcolor}
\usepackage{fancyvrb}
\setlist[itemize]{leftmargin=2em}
\setlength{\parskip}{0.5em}
\setlength{\parindent}{0pt}

"""
    ]
    if imported_preamble:
        parts.append("% Imported selectively from the paper preamble for macro compatibility\n")
        parts.append(imported_preamble)
        parts.append("\n")

    parts.append(r"\begin{document}" + "\n")
    parts.append(r"\section*{" + title + "}" + "\n")
    parts.append(r"\begin{itemize}" + "\n")
    for label, value in _concise_metadata_items(data):
        parts.append(r"\item " + _report_latex_paragraph_local(f"{label}: {value}") + "\n")
    parts.append(r"\end{itemize}" + "\n")
    parts.append(_high_priority_issues_tex(data["main_issue_entries"], normalized_options) + "\n")
    notable_tex = _notable_proof_reference_issues_tex(data["notable_proof_reference_entries"])
    if notable_tex:
        parts.append(notable_tex + "\n")
    if normalized_options.get("include_typographical_issues", True):
        parts.append(_typographical_errors_tex(data["typographical_entries"]) + "\n")
    if normalized_options.get("include_omitted_material_note", True):
        parts.append(r"\section*{Omitted material}" + "\n")
        parts.append(_report_latex_paragraph_local(_concise_omitted_material_text(normalized_options)) + "\n")
    parts.append(r"\end{document}" + "\n")

    text = _strip_unsafe_control_chars("".join(parts))
    style = _effective_report_reference_style(session)
    text = _rewrite_tex_report_references(text, session, style=style)
    text = _inject_tex_reference_status(text, _reference_report_status_tex(session, style=style))
    return _insert_tex_report_front_matter(
        text,
        session,
        include_audit_summary=normalized_options.get("include_audit_summary", True),
        include_verification_summary=normalized_options.get("include_verification_summary", True),
        issue_summary_open_only=normalized_options.get("only_open_issues", True),
    )


def build_concise_report_json(
    session_or_pdf: dict[str, Any] | str | Path,
    report_title: Optional[str] = None,
    options: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    session = _resolve_concise_report_session(session_or_pdf)
    data = _collect_concise_report_data(session, options=options)
    normalized_options = data["options"]
    return {
        "report_kind": "concise_audit_report",
        "title": _concise_report_title(session, report_title=report_title),
        "generated_at": utc_now(),
        "concise_report_options": normalized_options,
        "selection_rules": _concise_selection_rules(normalized_options),
        "audit_summary": (
            [
                {"label": label, "value": value}
                for label, value in _audit_summary_items(
                    session,
                    include_verification_summary=normalized_options.get("include_verification_summary", True),
                    issue_summary_open_only=normalized_options.get("only_open_issues", True),
                )
            ]
            if normalized_options.get("include_audit_summary", True)
            else []
        ),
        "metadata": [{"label": label, "value": value} for label, value in _concise_metadata_items(data)],
        "session": load_session_from_pdf(session["pdf_path"]) or session,
        "status": data["status"],
        "ledger": data["ledger"],
        "issues_state": data["issues_state"],
        "usage": data["usage"],
        "manifest": data["manifest"],
        "chunk_records": data["chunk_records"],
        "main_issues": data["main_issues"],
        "high_issues": data["main_issues"],
        "notable_incorrect_or_circular_references": data["notable_proof_reference_issues"],
        "notable_incorrect_or_circular_reference_entries": data["notable_proof_reference_entries"],
        "notable_proof_reference_and_dependency_issues": data["notable_proof_reference_issues"],
        "notable_proof_reference_and_dependency_entries": data["notable_proof_reference_entries"],
        "typographical_errors": data["typographical_entries"],
        "reference_status": _reference_report_status(session),
        "recheck_applied": bool(data["issue_recheck_overlay"].get("recheck_applied")),
        "issue_recheck_summary": data["issue_recheck_overlay"].get("issue_recheck_summary", {}),
        "issue_recheck_overlay": data["issue_recheck_overlay"],
        "grouped_downstream_issues": data["issue_recheck_overlay"].get("grouped_downstream_issues", {}),
    }


def build_concise_report(
    session_or_pdf: dict[str, Any] | str | Path,
    report_title: Optional[str] = None,
    options: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    session = _resolve_concise_report_session(session_or_pdf)
    _ensure_timing_state(session)
    root = Path(session["workdir"])
    report_stem = Path(session["pdf_path"]).stem + "_concise_audit_report"

    normalized_options = _normalize_concise_report_options(options)
    md_text = build_concise_report_markdown(session, report_title=report_title, options=normalized_options)
    tex_text = build_concise_report_tex(session, report_title=report_title, options=normalized_options)
    report_json = build_concise_report_json(session, report_title=report_title, options=normalized_options)

    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    md_path = reports_dir / f"{report_stem}.md"
    tex_path = reports_dir / f"{report_stem}.tex"
    json_path = reports_dir / f"{report_stem}.json"

    md_path.write_text(md_text, encoding="utf-8")
    tex_path.write_text(tex_text, encoding="utf-8")
    save_json(json_path, report_json)

    return {
        "markdown": str(md_path),
        "tex": str(tex_path),
        "json": str(json_path),
    }


__all__ = [
    "_effective_reference_mention_style",
    "_effective_report_reference_style",
    "_load_aux_label_map",
    "_reference_context_for_chunk_strict",
    "_reference_map_has_valid_aux_numbers",
    "_reference_prompt_rule_for_style",
    "_reference_prompt_status_note",
    "build_concise_report",
    "build_concise_report_json",
    "build_concise_report_markdown",
    "build_concise_report_tex",
    "build_final_report",
    "build_final_report_markdown",
    "build_final_report_tex",
    "build_user_message_for_chunk",
    "concise_report_options_for_preset",
    "default_concise_report_options",
    "ensure_reference_map",
]

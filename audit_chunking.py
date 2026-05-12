from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Optional

from audit_state import utc_now

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


THEOREM_ENVS = [
    "theorem", "lemma", "proposition", "corollary", "claim", "fact",
    "definition", "remark", "example", "conjecture", "algorithm",
    "thm", "lem", "prop", "cor", "defn",
]

_PDF_ANCHOR_WORDS = (
    "theorem",
    "lemma",
    "proposition",
    "corollary",
    "definition",
    "remark",
    "claim",
    "example",
    "conjecture",
    "proof",
)
_PDF_SECTION_TITLE_WORDS = (
    "abstract",
    "introduction",
    "preliminaries",
    "notation",
    "main result",
    "main results",
    "proof",
    "proofs",
    "application",
    "applications",
    "conclusion",
    "references",
    "acknowledgement",
    "acknowledgements",
)


def read_text_file(path: str | Path) -> str:
    path = Path(path)
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except Exception:
            pass
    return path.read_text(errors="ignore")


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _shorten_pdf_anchor(text: str, limit: int = 88) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip(" \t-:.;")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip(" ,;:-") + "..."


def _pdf_page_range_label(page_start: Any, page_end: Any) -> str:
    if page_start and page_end:
        return f"PDF pages {page_start}-{page_end}"
    if page_start:
        return f"PDF page {page_start}"
    return "PDF pages unknown"


def _clean_pdf_anchor_line(line: str) -> str:
    line = re.sub(r"\s+", " ", str(line or "")).strip()
    return line.strip(" \t")


def _pdf_anchor_lines(text: str) -> list[str]:
    return [
        _clean_pdf_anchor_line(line)
        for line in normalize_whitespace(str(text or "")).splitlines()
        if _clean_pdf_anchor_line(line)
    ]


def _is_numeric_page_marker(line: str) -> bool:
    return bool(re.fullmatch(r"\d{1,4}", line.strip()))


def _looks_like_pdf_running_header(line: str, index: int, lines: list[str]) -> bool:
    clean = line.strip()
    lower = clean.lower()
    if not clean:
        return True
    if _is_numeric_page_marker(clean):
        return True
    if "e-mail" in lower or "email" in lower:
        return True
    if lower.startswith("received "):
        return True
    if "mathematical journal" in lower and ("vol" in lower or "journal" in lower):
        return True
    if lower.startswith("department of ") or lower.startswith("laboratory of "):
        return True
    if index <= 3:
        adjacent_page_number = (
            (index + 1 < len(lines) and _is_numeric_page_marker(lines[index + 1]))
            or (index > 0 and _is_numeric_page_marker(lines[index - 1]))
        )
        if adjacent_page_number and not _line_starts_with_anchor_word(clean):
            return True
    return False


def _line_starts_with_anchor_word(line: str) -> bool:
    return bool(re.match(r"^(?:" + "|".join(_PDF_ANCHOR_WORDS) + r")\b", line, flags=re.IGNORECASE))


def _looks_like_section_title(line: str) -> bool:
    clean = line.strip()
    lower = clean.lower().strip(".")
    if not clean or len(clean) > 90:
        return False
    if any(word in lower for word in _PDF_SECTION_TITLE_WORDS):
        return True
    math_symbol_codes = {0x2264, 0x2265, 0x2208, 0x2209, 0x2211, 0x220f, 0x222b}
    has_math_symbol = any(ch in clean for ch in "=<>") or any(ord(ch) in math_symbol_codes for ch in clean)
    if has_math_symbol and not re.match(r"^[A-Za-z]", clean):
        return False
    if re.search(r"[.!?;=]", clean):
        return False
    if not re.search(r"[A-Za-z]", clean):
        return False
    if clean.lower().startswith(("department ", "university ", "laboratory ")):
        return False
    words = clean.split()
    if 1 <= len(words) <= 8:
        capitalized = sum(1 for word in words if re.match(r"^[A-Z][a-zA-Z'-]*$", word))
        if len(words) >= 2 and capitalized == len(words):
            # Likely an author name rather than a section title.
            return False
        return True
    return False


def _section_anchor_from_lines(lines: list[str], index: int) -> Optional[str]:
    line = lines[index]
    combined = re.match(r"^(\d+(?:\.\d+)*\.?)\s+(.{3,90})$", line)
    if combined:
        title = _clean_pdf_anchor_line(combined.group(2))
        if _looks_like_section_title(title):
            return _shorten_pdf_anchor(f"Section {combined.group(1).rstrip('.')} {title}")

    if re.fullmatch(r"\d+(?:\.\d+)*\.?", line) and index + 1 < len(lines):
        title = _clean_pdf_anchor_line(lines[index + 1])
        next_line = _clean_pdf_anchor_line(lines[index + 2]) if index + 2 < len(lines) else ""
        if _looks_like_section_title(title) and not next_line.lower().startswith(("department ", "university ")):
            return _shorten_pdf_anchor(f"Section {line.rstrip('.')} {title}")

    lower = line.lower().strip(".")
    if any(lower.startswith(word) for word in _PDF_SECTION_TITLE_WORDS):
        return _shorten_pdf_anchor(line)
    return None


def _theorem_anchor_from_line(line: str) -> Optional[str]:
    if not line[:1].isupper():
        return None
    match = re.match(
        r"^(" + "|".join(_PDF_ANCHOR_WORDS) + r")\b\s*(.*)$",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    kind = match.group(1).capitalize()
    rest = _clean_pdf_anchor_line(match.group(2))
    if kind.lower() == "proof":
        if rest and not (rest.startswith(("[", "(", ".", ":")) or rest.lower().startswith("of ")):
            return None
        return _shorten_pdf_anchor(f"{kind} {rest}") if rest else kind
    if rest and not (
        rest.startswith(("[", "(", ".", ":"))
        or re.match(r"^(?:\d+(?:\.\d+)*|[A-Z])(?:\b|[.:\\[])", rest)
    ):
        return None
    if rest:
        return _shorten_pdf_anchor(f"{kind} {rest}")
    return kind


def _fallback_pdf_anchor(lines: list[str]) -> str:
    for index, line in enumerate(lines):
        if _looks_like_pdf_running_header(line, index, lines):
            continue
        if not re.search(r"[A-Za-z]", line):
            continue
        if len(line) < 18:
            continue
        if _line_starts_with_anchor_word(line) and _theorem_anchor_from_line(line) is None:
            continue
        return _shorten_pdf_anchor(line)
    return ""


def pdf_chunk_display_label(page_start: Any, page_end: Any, chunk_text: str) -> str:
    base = _pdf_page_range_label(page_start, page_end)
    lines = _pdf_anchor_lines(chunk_text)
    anchor = ""
    for index, line in enumerate(lines):
        if _looks_like_pdf_running_header(line, index, lines):
            continue
        anchor = _section_anchor_from_lines(lines, index)
        if anchor:
            break
        anchor = _theorem_anchor_from_line(line)
        if anchor:
            break
    if not anchor:
        anchor = _fallback_pdf_anchor(lines)
    return f"{base}: {anchor}" if anchor else base


def ensure_chunk_display_labels(manifest: dict[str, Any]) -> dict[str, Any]:
    chunks = manifest.get("chunks") if isinstance(manifest, dict) else None
    if not isinstance(chunks, list):
        return manifest
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        current = str(chunk.get("display_label") or "").strip()
        if current:
            continue
        if str(chunk.get("source_kind") or "").lower().startswith("pdf"):
            chunk["display_label"] = pdf_chunk_display_label(
                chunk.get("page_start"),
                chunk.get("page_end"),
                str(chunk.get("chunk_text") or ""),
            )
        else:
            label = str(chunk.get("label") or "").strip()
            page_label = _pdf_page_range_label(chunk.get("page_start"), chunk.get("page_end")).replace("PDF ", "Approx. ")
            chunk["display_label"] = f"{page_label}: {label}" if label else page_label
    return manifest


def strip_tex_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        line = re.sub(r"(?<!\\)%.*$", "", line)
        out.append(line)
    return "\n".join(out)


def load_pdf_pages(pdf_path: str | Path) -> list[str]:
    pdf_path = Path(pdf_path)
    if fitz is not None:
        doc = fitz.open(str(pdf_path))
        return [normalize_whitespace(page.get_text("text")) for page in doc]
    if PdfReader is not None:
        reader = PdfReader(str(pdf_path))
        pages = []
        for p in reader.pages:
            txt = p.extract_text() or ""
            pages.append(normalize_whitespace(txt))
        return pages
    raise RuntimeError("Neither PyMuPDF nor pypdf is available.")


def find_matching_end(text: str, env: str, start_pos: int) -> int:
    m = re.search(rf"\\end\{{{re.escape(env)}\}}", text[start_pos:])
    return -1 if m is None else start_pos + m.end()


def split_large_text_unit(text: str, max_chars: int = 4500, min_chars: int = 1200) -> list[str]:
    text = normalize_whitespace(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    blocks = re.split(r"\n\s*\n", text)
    out, current, cur_len = [], [], 0
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        blen = len(block) + 2
        if current and cur_len + blen > max_chars and cur_len >= min_chars:
            out.append("\n\n".join(current).strip())
            current, cur_len = [block], blen
        else:
            current.append(block)
            cur_len += blen
    if current:
        out.append("\n\n".join(current).strip())
    if len(out) == 1 and len(out[0]) > max_chars:
        s = out[0]
        out = [s[i:i + max_chars].strip() for i in range(0, len(s), max_chars) if s[i:i + max_chars].strip()]
    return out


def _chunk_from_tex_substring(label: str, sub: str, start_pos: int, raw_len: int, pdf_page_count: int, source_kind: str) -> dict[str, Any]:
    start_ratio = start_pos / max(1, raw_len)
    end_ratio = min(1.0, (start_pos + len(sub)) / max(1, raw_len))
    page_start = max(1, math.floor(start_ratio * pdf_page_count) + 1)
    page_end = max(page_start, math.ceil(end_ratio * pdf_page_count))
    return {
        "label": label,
        "boundary": f"Approx. pages {page_start}-{page_end} based on TeX order",
        "chunk_text": sub,
        "source_kind": source_kind,
        "page_start": page_start,
        "page_end": page_end,
        "measure": len(sub),
        "tex_start": start_pos,
        "tex_end": start_pos + len(sub),
    }


def parse_tex_chunks_full_coverage(
    tex_text: str,
    pdf_page_count: int,
    max_chars: int = 4500,
    min_chars: int = 1200,
) -> list[dict[str, Any]]:
    raw = normalize_whitespace(strip_tex_comments(tex_text))
    if not raw:
        return []

    begin_pat = re.compile(r"\\begin\{(" + "|".join(map(re.escape, THEOREM_ENVS)) + r")\}")
    matches = list(begin_pat.finditer(raw))
    units = []

    for idx, m in enumerate(matches, start=1):
        env = m.group(1)
        start = m.start()
        end = find_matching_end(raw, env, m.end())
        if end == -1:
            continue

        proof_begin = re.compile(r"\s*\\begin\{proof\}")
        proof_end = re.compile(r"\\end\{proof\}")
        pb = proof_begin.match(raw, end)
        if pb:
            pe = proof_end.search(raw, pb.end())
            if pe:
                end = pe.end()

        units.append({
            "kind": "theorem_like",
            "env": env,
            "start": start,
            "end": end,
            "text": raw[start:end].strip(),
            "idx": idx,
        })

    if not units:
        chunks = []
        search_from = 0
        for idx, sub in enumerate(split_large_text_unit(raw, max_chars=max_chars, min_chars=min_chars), start=1):
            pos = raw.find(sub[:min(100, len(sub))], search_from)
            if pos < 0:
                pos = search_from
            chunks.append(
                _chunk_from_tex_substring(
                    label=f"TeX chunk {idx}",
                    sub=sub,
                    start_pos=pos,
                    raw_len=len(raw),
                    pdf_page_count=pdf_page_count,
                    source_kind="tex",
                )
            )
            search_from = pos + len(sub)
        return chunks

    units.sort(key=lambda u: u["start"])
    out = []
    cursor = 0
    gap_idx = 1

    for unit in units:
        if unit["start"] > cursor:
            gap_text = raw[cursor:unit["start"]].strip()
            if gap_text:
                running_start = cursor
                parts = split_large_text_unit(gap_text, max_chars=max_chars, min_chars=min_chars)
                for part_idx, sub in enumerate(parts, start=1):
                    pos = raw.find(sub[:min(100, len(sub))], running_start, unit["start"])
                    if pos < 0:
                        pos = running_start
                    out.append(
                        _chunk_from_tex_substring(
                            label=f"TeX gap {gap_idx}" + (f" part {part_idx}" if len(parts) > 1 else ""),
                            sub=sub,
                            start_pos=pos,
                            raw_len=len(raw),
                            pdf_page_count=pdf_page_count,
                            source_kind="tex-gap",
                        )
                    )
                    running_start = pos + len(sub)
                gap_idx += 1

        parts = split_large_text_unit(unit["text"], max_chars=max_chars, min_chars=min_chars)
        for part_idx, sub in enumerate(parts, start=1):
            pos = raw.find(sub[:min(100, len(sub))], unit["start"], unit["end"])
            if pos < 0:
                pos = unit["start"]
            out.append(
                _chunk_from_tex_substring(
                    label=f"{unit['env'].title()} unit {unit['idx']}" + (f" part {part_idx}" if len(parts) > 1 else ""),
                    sub=sub,
                    start_pos=pos,
                    raw_len=len(raw),
                    pdf_page_count=pdf_page_count,
                    source_kind="tex",
                )
            )

        cursor = max(cursor, unit["end"])

    if cursor < len(raw):
        gap_text = raw[cursor:].strip()
        if gap_text:
            running_start = cursor
            parts = split_large_text_unit(gap_text, max_chars=max_chars, min_chars=min_chars)
            for part_idx, sub in enumerate(parts, start=1):
                pos = raw.find(sub[:min(100, len(sub))], running_start)
                if pos < 0:
                    pos = running_start
                out.append(
                    _chunk_from_tex_substring(
                        label=f"TeX gap {gap_idx}" + (f" part {part_idx}" if len(parts) > 1 else ""),
                        sub=sub,
                        start_pos=pos,
                        raw_len=len(raw),
                        pdf_page_count=pdf_page_count,
                        source_kind="tex-gap",
                    )
                )
                running_start = pos + len(sub)

    out.sort(key=lambda c: c["tex_start"])
    return out


def split_pdf_pages_into_chunks(
    pdf_pages: list[str],
    max_chars: int = 3500,
    min_chars: int = 800,
) -> list[dict[str, Any]]:
    chunks = []
    current_pages = []
    current_len = 0
    page_start = None

    def flush() -> None:
        nonlocal current_pages, current_len, page_start
        if not current_pages:
            return
        page_end = page_start + len(current_pages) - 1
        text = "\n\n".join(current_pages).strip()
        chunks.append({
            "label": f"PDF pages {page_start}-{page_end}",
            "display_label": pdf_chunk_display_label(page_start, page_end, text),
            "boundary": f"Pages {page_start}-{page_end}",
            "chunk_text": text,
            "source_kind": "pdf",
            "page_start": page_start,
            "page_end": page_end,
            "measure": len(text),
        })
        current_pages = []
        current_len = 0
        page_start = None

    for idx, txt in enumerate(pdf_pages, start=1):
        txt = txt.strip()
        if not txt:
            txt = f"[Page {idx}: no extractable text]"
        add_len = len(txt) + 2
        if page_start is None:
            page_start = idx
        if current_pages and current_len + add_len > max_chars and current_len >= min_chars:
            flush()
            page_start = idx
        current_pages.append(txt)
        current_len += add_len
    flush()
    return chunks


def build_auto_chunks(
    pdf_path: str | Path,
    tex_path: Optional[str | Path],
    tex_max_chars: int = 4500,
    pdf_max_chars: int = 3500,
    min_tex_coverage_ratio: float = 0.98,
) -> dict[str, Any]:
    pdf_pages = load_pdf_pages(pdf_path)
    page_count = len(pdf_pages)

    tex_chunks = []
    raw_tex_len = 0
    if tex_path and Path(tex_path).exists():
        try:
            tex_text = read_text_file(tex_path)
            raw_tex = normalize_whitespace(strip_tex_comments(tex_text))
            raw_tex_len = len(raw_tex)
            tex_chunks = parse_tex_chunks_full_coverage(
                tex_text,
                page_count,
                max_chars=tex_max_chars,
                min_chars=max(1000, tex_max_chars // 3),
            )
        except Exception as e:
            print(f"Warning: TeX parsing failed: {e}")

    if tex_chunks:
        covered = sum(max(1, c["measure"]) for c in tex_chunks)
        coverage_ratio = covered / max(1, raw_tex_len)
    else:
        coverage_ratio = 0.0

    if tex_chunks and coverage_ratio >= min_tex_coverage_ratio:
        chunks = tex_chunks
        mode = "tex-full-coverage"
    else:
        chunks = split_pdf_pages_into_chunks(
            pdf_pages,
            max_chars=pdf_max_chars,
            min_chars=max(800, pdf_max_chars // 3),
        )
        mode = "pdf-fallback"

    total_measure = sum(max(1, c["measure"]) for c in chunks)
    running = 0
    for idx, c in enumerate(chunks, start=1):
        c["chunk_index"] = idx
        c["chunk_id"] = f"chunk_{idx:03d}"
        c["paper_progress_start"] = running / max(1, total_measure)
        running += max(1, c["measure"])
        c["paper_progress_end"] = running / max(1, total_measure)

    if chunks:
        chunks[-1]["page_end"] = page_count
        if str(chunks[-1].get("source_kind") or "").lower().startswith("pdf"):
            chunks[-1]["display_label"] = pdf_chunk_display_label(
                chunks[-1].get("page_start"),
                chunks[-1].get("page_end"),
                str(chunks[-1].get("chunk_text") or ""),
            )

    manifest = {
        "pdf_path": str(Path(pdf_path).resolve()),
        "tex_path": str(Path(tex_path).resolve()) if tex_path and Path(tex_path).exists() else None,
        "pdf_page_count": page_count,
        "chunking_mode": mode,
        "tex_coverage_ratio": coverage_ratio,
        "created_at": utc_now(),
        "chunks": chunks,
    }
    ensure_chunk_display_labels(manifest)
    return manifest


__all__ = [
    "THEOREM_ENVS",
    "load_pdf_pages",
    "find_matching_end",
    "split_large_text_unit",
    "_chunk_from_tex_substring",
    "parse_tex_chunks_full_coverage",
    "pdf_chunk_display_label",
    "ensure_chunk_display_labels",
    "split_pdf_pages_into_chunks",
    "build_auto_chunks",
]

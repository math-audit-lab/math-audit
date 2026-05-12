# Project: mathematical paper audit app

This project contains a Python/PySide6 application for auditing mathematics papers chunk by chunk using the OpenAI API. The GUI is the primary frontend. The Jupyter notebook is now a secondary maintenance, debugging, and experimentation frontend.

The app supports long mathematical papers, preferably with both PDF and LaTeX source, but it must also work safely in PDF-only mode.

## Primary goal

Maintain and improve the audit app so that it is reliable for real mathematical paper auditing.

The most important priorities are:

- full-paper coverage;
- mathematically useful chunking;
- reliable interruption/resume behavior;
- robust structured output parsing;
- accurate progress, token, elapsed-time, and cost accounting;
- safe report generation;
- clear GUI behavior for real audit workflows;
- backward compatibility with existing audit folders when feasible.

## Current architecture

The GUI is the canonical production frontend.

Canonical modules include:

- `audit_runtime.py`
- `audit_policy_hooks.py`
- `audit_hooks.py`
- `audit_prompts.py`
- `audit_state.py`
- `audit_verification.py`
- `gui_controller.py`
- `gui_main_window.py`

The notebook:

- `automatic_math_paper_audit_consolidated.ipynb`

is secondary. It may be used for maintenance, debugging, experiments, and backend inspection, but it should not own production workflow logic that is already implemented in the canonical Python modules.

If logic exists both in the notebook and in canonical modules, prefer the canonical modules unless the user explicitly asks about notebook-only maintenance.

## Inputs usually available

Typical inputs are:

- paper PDF;
- companion `.tex` file when available;
- sometimes companion `.aux` file;
- audit working directory with state, chunk logs, reports, extracted artifacts, verification scripts, reruns, exports, and discussion history.

Representative audit folders may exist under `examples/..._audit/`. Use them as behavioral references and regression cases.

## Core audit requirements

- Preserve full-paper coverage during chunking.
- Prefer TeX-aware chunking when it covers the whole paper.
- Fall back safely to PDF-based chunking when TeX is missing or incomplete.
- Never silently stop after covering only theorem-like environments.
- Preserve or recover the paper’s own numbering when possible:
  - equation numbers should match the compiled PDF;
  - theorem, lemma, proposition, corollary, definition, remark, and section numbering should match the compiled PDF;
  - prefer `.aux`-derived numbering when available.
- PDF-only fallback must be honest about degradation:
  - no invented labels;
  - no fake TeX references;
  - page-first locations are acceptable when source labels are unavailable.
- Chunk manifests should preserve canonical chunk IDs.
- GUI-facing chunk labels may be improved, but they must not replace canonical IDs.

## GUI requirements

The GUI is the primary workflow surface. Preserve and improve the following behaviors:

- start fresh audit;
- resume audit;
- pause audit;
- cancel current stuck chunk;
- selective chunk rerun;
- failed-verification rerun;
- live status, progress, cost, token, and elapsed-time display;
- sticky per-audit model and reasoning-effort settings;
- report build/rebuild/open controls;
- verification execution and progress display;
- report freshness/staleness display;
- discussion pane with raw/rendered modes;
- multiple discussion threads per audit;
- ChatGPT context-pack export.

When editing GUI code:

- keep workflow labels user-facing and specific;
- avoid generic buttons when report-specific buttons are clearer;
- preserve scroll behavior in long report/verification output panes;
- do not block opening stale reports, but warn clearly when reports may be stale;
- after builds or verification runs, refresh GUI status immediately.

## Discussion-thread behavior

Preserve the intended discussion model:

- same thread means same conversation ID;
- new discussion thread means new conversation ID;
- legacy thread keeps its old conversation ID;
- fresh discussion threads use rich audit-only context plus that thread’s own prior Q&A;
- fresh discussion threads must not inherit older-thread Q&A contamination.

Discussion history should persist across app restarts.

Discussion usage should be accounted separately from audit usage:

- discussion cost;
- discussion tokens;
- discussion turns.

## Prompt management

Prompt logic belongs in canonical shared code, especially `audit_prompts.py`, not in notebook-only cells.

Fresh audits should snapshot the exact audit system prompt into session state, including prompt metadata.

Resumed audits should keep their saved prompt unless the user explicitly chooses otherwise.

The shipped default prompt should support GUI/report-safe output:

- use `$...$` and `$$...$$` for math;
- avoid `\(...\)` and `\[...\]` in generated prose;
- keep prose readable in GUI, JSON, Markdown reports, and LaTeX-generated reports;
- restrict severity labels to the closed set:
  - `low`
  - `medium`
  - `high`
  - `critical`

## Reports

The app may generate:

- full report;
- concise report;
- verification report;
- report metadata sidecars;
- PDF/TeX/Markdown/JSON variants where supported.

Report generation must prioritize robustness over fancy formatting.

Important report requirements:

- final reports must compile safely on macOS with TeXShop / `pdflatex`;
- generated LaTeX should avoid fragile constructs;
- prose should be escaped safely;
- code, raw patches, JSON, traceback text, and LaTeX patch suggestions should be rendered in verbatim-style environments, not accidentally executed as live LaTeX;
- only treat text as live LaTeX when it is intentionally meant to compile;
- if importing macros from a paper preamble, do so selectively and safely;
- include hyperlinked table of contents where supported;
- avoid duplicate front-matter summary blocks;
- include audit summary and verification summary when appropriate.

Report freshness matters. If audit state, verification state, rerun state, chunk state, usage state, or issue state changed after a report was generated, the GUI should mark the relevant report as stale or freshness-unknown. Stale reports may still be opened, but the warning must be visible.

## Concise report behavior

The concise report is configurable.

Preserve support for:

- strict concise default behavior;
- severity inclusion options;
- optional typo section;
- optional audit summary;
- optional verification summary;
- optional omitted-material note;
- open-issues-only mode.

The main concise issue section should normally emphasize high-impact issues, especially:

- `critical`;
- `high`.

Typo/editorial issues may remain in a separate section.

## Verification

Verification support is part of the main GUI workflow.

Preserve support for:

- generating or discovering verification scripts;
- running the verification suite;
- showing number of scripts available;
- showing previous verification summary;
- live per-script progress;
- statuses such as PASS, FAIL, TIMEOUT, and SKIPPED;
- failed-verification rerun;
- verification report rebuild/open.

When interpreting verification results, be careful with semantics:

- a PASS may mean a counterexample was successfully found when the script was intended to test a suspected false claim;
- do not assume PASS always means the paper’s claim was validated.

Where feasible, preserve enough rerun history for the user to understand whether timeouts or failures were later repaired.

## Export / ChatGPT handoff

The ChatGPT context-pack export is an important workflow.

The export folder may include:

- `audit_context.md`;
- `paper_structure.json`;
- the paper PDF;
- TeX source if available;
- concise report Markdown;
- verification report Markdown;
- optionally full report material.

The export manifest may be written as a sidecar outside the handoff folder.

Do not assume local filesystem paths in prompts are useful to ChatGPT. Handoff should be based on files the user manually attaches plus a starter prompt the user can paste.

General usefulness hierarchy:

- PDF + TeX only: fresh reading;
- PDF + `audit_context.md`: compact audit-aware discussion;
- PDF + `audit_context.md` + `paper_structure.json`: deeper dependency-aware discussion.

## State and backward compatibility

Audit state may include:

- `session.json`;
- `status.json`;
- `usage.json`;
- `chunk_manifest.json`;
- `chunks.jsonl`;
- `issues.json`;
- `verification.json`;
- `reference_map.json`;
- report sidecars;
- logs;
- rerun records;
- discussion/QA records;
- exports.

Preserve backward compatibility with existing audit folders when feasible.

When adding new state fields:

- make readers tolerant of missing fields;
- provide safe defaults;
- do not require old audits to be regenerated unless necessary;
- clearly warn if a change may invalidate old audit state.

Do not delete prior audit outputs unless the user explicitly asks.

## Cost, token, and timing accounting

Track and preserve:

- per-chunk token usage;
- per-chunk elapsed time;
- cumulative audit cost;
- cumulative audit tokens;
- total audit time;
- discussion cost and tokens separately from audit cost and tokens.

Be careful with model-specific pricing.

Long-context pricing and short-context pricing may differ for GPT-5.5 / GPT-5.4 model families. Do not collapse pricing categories unless that is explicitly intended and verified.

## Coding style and change policy

Make minimal, high-confidence changes.

Before editing:

1. inspect where the relevant logic lives;
2. identify the exact functions/classes/files responsible;
3. explain current behavior;
4. explain likely bug or limitation;
5. propose a focused patch plan.

When editing:

- keep diffs focused;
- avoid broad refactors unless necessary;
- avoid introducing unnecessary dependencies;
- prefer explicit helper functions over ad hoc inline patches;
- preserve existing public behavior unless the user requested a change;
- keep GUI and runtime logic separated where practical;
- avoid duplicating logic between the GUI, runtime, hooks, and notebook;
- avoid notebook shadowing of canonical module behavior.

After editing:

- validate with syntax checks where appropriate;
- trace the affected workflow end to end when feasible;
- summarize exactly what changed;
- summarize what the user should rerun or manually test.

## Preferred workflow for Codex or other coding agents

1. Inspect relevant files first.
2. Identify the responsible functions/classes.
3. Propose a minimal patch plan.
4. Make focused changes.
5. Run available lightweight validation, such as `python -m py_compile` on changed modules.
6. If a representative audit folder exists, test or reason against it.
7. Summarize:
   - files changed;
   - functions/classes changed;
   - new helper functions;
   - migration/backward-compatibility implications;
   - validation performed;
   - manual GUI test steps.

## Things to pay special attention to

- chunking completeness;
- PDF-only fallback behavior;
- TeX-aware chunking coverage;
- recovery after interruption;
- stale pending chunks and cancellation;
- structured output parsing;
- numbering normalization;
- reference-map degradation when TeX is missing;
- report freshness/staleness;
- final LaTeX report safety;
- TeX macro import from paper preamble;
- progress/cost/time reporting;
- discussion-thread contamination;
- export-pack completeness;
- regression behavior on real audit folders.

## Representative regression cases

Use real audit folders as behavioral references when available.

A known useful reference case is the LMJ PDF-only audit:

- `examples/LMJ/LMJ2604-004RA0.pdf`
- `examples/LMJ/LMJ2604-004RA0_audit/`

This case is useful for testing:

- PDF-only fallback chunking;
- no-TeX reference behavior;
- page-first issue locations;
- verification suite execution;
- failed-verification rerun workflow;
- report freshness after verification/rerun sequences;
- ChatGPT context-pack export for a PDF-only audit.

Do not hard-code behavior for this case. Use it as a regression reference only.

## When the user asks for analysis

Be precise and codebase-specific.

Quote filenames and function names.

Distinguish clearly between:

- current behavior;
- likely bugs;
- confirmed bugs;
- recommended fixes;
- optional improvements.

If evidence is incomplete, say so.

## When the user asks for edits

Keep diffs focused.

Avoid unnecessary new dependencies.

Explain any migration steps needed for old sessions.

If a change might affect existing audit folders, warn clearly.

## When the user asks for release preparation

The app may be prepared as an experimental research prototype, not polished consumer software.

Important release-prep items include:

- `README.md`;
- `QUICKSTART.md`;
- environment specification such as `environment.yml` or equivalent;
- bundled/local MathJax assets for rendered discussion;
- removal of personal path assumptions;
- startup checks for required tools and optional dependencies;
- clear statement that GUI is primary and notebook is secondary;
- clear platform status, especially if mainly tested on macOS.

## Non-goals unless explicitly requested

Do not make broad architectural rewrites.

Do not replace the OpenAI backend with cross-provider abstraction unless explicitly requested.

If provider abstraction is ever added, start conservatively and consider limiting alternative providers to discussion/report drafting before touching the full audit pipeline.

Do not remove the notebook entirely unless explicitly requested.

Do not silently migrate or delete existing audit state.


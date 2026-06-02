# Math Paper Audit

Math Paper Audit is an experimental research prototype for auditing mathematical papers chunk by chunk with the OpenAI API. It is designed to help a researcher inspect long papers, preserve audit state, generate reports, and run lightweight verification checks. It is not a production proof assistant, theorem prover, or substitute for expert mathematical review.

The PySide6 GUI is the primary maintained frontend. Private development branches may use notebooks for maintenance and experiments, but this public preview ships the GUI and shared backend modules as the supported interface.

## Current Status

This project is under active development and should be treated cautiously. The app is mainly tested on macOS, including TeXShop / `pdflatex` workflows for generated TeX reports. Other platforms may work, but they are not yet the main test target.

The `examples/` directory is intentionally ignored by Git. It is local regression and test data, not committed source code.

## Major Features

- Start fresh audits or resume existing audit sessions.
- Pause a running audit and cancel a pending/current chunk when recovery is needed.
- Chunk papers from PDF plus companion TeX when available, with PDF-only fallback when TeX is unavailable.
- Preserve audit state in an audit workdir next to the selected paper.
- Use the default continuous-conversation audit mode; `fresh_context_experimental` is available for experiments but remains explicitly experimental.
- Generate full, concise, and verification reports in Markdown, TeX, and JSON.
- Track report freshness so stale reports are visible after audit state changes.
- Run generated local Python verification scripts and view progress/results.
- Rerun selected chunks or chunks with failed/timed-out verification results.
- Ask post-audit questions in the Discussion pane, with saved thread history and rendered Markdown/math output.
- Export a one-way ChatGPT context pack for continuing work manually outside the app.
- Manage the shipped audit prompt and optional model-specific prompt overrides from the GUI.

## Architecture

Canonical backend/runtime logic lives in normal Python modules:

- `audit_runtime.py`
- `audit_policy_hooks.py`
- `audit_hooks.py`
- `audit_prompts.py`
- `audit_state.py`
- `audit_verification.py`
- `audit_chunking.py`

The GUI frontend lives in:

- `audit_gui.py`
- `gui_controller.py`
- `gui_main_window.py`

Private development notebooks may exist outside this public preview. New user-facing features should be implemented in the GUI/backend modules first.

## Repository Layout

- `audit_gui.py`, `gui_controller.py`, and `gui_main_window.py` implement the primary PySide6 GUI.
- `audit_runtime.py`, `audit_policy_hooks.py`, `audit_hooks.py`, `audit_state.py`, `audit_chunking.py`, `audit_verification.py`, and `audit_prompts.py` are the shared backend modules.
- `gui_assets/` contains bundled local MathJax and font assets. These are intentionally kept in the repository so the rendered discussion pane can typeset math without a CDN dependency.
- Private development notebooks are not part of this public preview; the GUI/backend modules are the production code path.
- `audit_prompt_profiles.json` stores GUI-edited prompt profile overrides. The checked-in file is an empty/default profile; avoid committing local prompt experiments or sensitive/proprietary prompt text.
- `examples/` is local ignored regression/test data. Generated audit workdirs, report outputs, verification results, reruns, discussion turns, and export folders should stay out of Git.

## Basic Use

See [QUICKSTART.md](QUICKSTART.md) for setup and first-run instructions. For a more detailed screenshot-based walkthrough, see [docs/user_guide.md](docs/user_guide.md).

At a high level:

1. Create and activate the conda environment.
2. Optionally run `python scripts/check_setup.py` to smoke-check local dependencies without running an audit.
3. Launch the GUI with `python audit_gui.py`.
4. Paste an OpenAI API key into the GUI.
5. Select a paper PDF.
6. If a same-basename `.tex` file exists next to the PDF, the app will try TeX-aware chunking; otherwise it falls back to PDF text extraction.
7. Start or resume the audit.
8. Build and open reports, run verification, and optionally export a ChatGPT context pack.

## Outputs and State

For a PDF named `paper.pdf`, audit state is written next to it in a folder like:

```text
paper_audit/
```

That workdir contains state JSON, chunk records, prompts/requests/responses, generated reports, verification scripts/results, discussion turns, rerun logs, and export folders. These are local audit artifacts and are generally not meant to be committed.

Do not commit audit outputs, paper PDFs/TeX sources, request/response logs, generated reports, verification scripts/results, rerun folders, or review sidecars. They may contain paper text, model responses, local filesystem paths, and sensitive review material.

## Limitations

- The app can find real issues, but generated audits and verification checks can be incomplete or mistaken.
- The `fresh_context_experimental` context mode is still experimental. It can help with long audits or context/file-service robustness, while continuous mode may be cheaper for short PDF-only audits with good cache reuse.
- PDF-only chunking depends on text extraction quality.
- TeX-aware chunking is preferred when a companion `.tex` file is available and covers the paper reliably.
- Report TeX is intended to be robust, but local LaTeX installations still vary.
- The GUI discussion rendered mode depends on bundled local MathJax assets.
- Pricing/cost estimates are local calculations and may differ from platform billing in edge cases.

## License

This project is released under the MIT License. See [LICENSE](LICENSE).

## Development Notes

- Keep the GUI as the primary user-facing frontend.
- Keep shared backend modules canonical.
- Keep public workflow logic in the GUI/backend modules; private notebooks should remain outside the public release branch.
- Do not commit generated audit outputs or local `examples/` data.
- Run `python scripts/check_regressions.py` for a lightweight no-API regression check of recent report-freshness and PDF-label behavior.
- Prefer small, focused changes that preserve backward compatibility with existing audit folders.
- The experimental Review tab is hidden by default; developers can enable it with `MATH_AUDIT_ENABLE_REVIEW_TAB=1 python audit_gui.py`.

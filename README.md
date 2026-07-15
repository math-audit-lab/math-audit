# Math Paper Audit

Math Paper Audit is an experimental research prototype for auditing mathematical papers chunk by chunk with the OpenAI API. It is designed to help a researcher inspect long papers, preserve audit state, generate reports, and run lightweight verification checks. It is not a production proof assistant, theorem prover, or substitute for expert mathematical review.

The PySide6 GUI is the primary maintained frontend. Private development branches may use notebooks for maintenance and experiments, but this public preview ships the GUI and shared backend modules as the supported interface.

## Current Status

This project is under active development and should be treated cautiously. The app is mainly tested on macOS, including TeXShop / `pdflatex` workflows for generated TeX reports. Other platforms may work, but they are not yet the main test target.

The `examples/` directory is intentionally ignored by Git. It is local regression and test data, not committed source code.

## Who Is This For?

Math Paper Audit is intended for researchers who want a structured, AI-assisted second pass through a mathematical manuscript. Typical uses include authors preparing a paper for journal or arXiv submission, researchers checking a paper whose results are important for their own work, and referees/reviewers only when journal policy, confidentiality rules, and the manuscript situation allow use of an external API-based tool.

In limited testing, the app has been able to find mathematical issues, proof gaps, incorrect references, and typographical errors in places that are easy to miss during ordinary reading. It can also help turn a long, difficult reading task into a more structured review process.

Use it cautiously. The app is not an automatic referee, proof assistant, or theorem prover. It can miss errors, overstate issues, and produce false positives. Findings are provisional and require human mathematical checking. For refereeing or review use, you are responsible for ensuring that use of an external API is allowed.

## Major Features

- Start fresh audits or resume existing audit sessions.
- Pause a running audit and cancel a pending/current chunk when recovery is needed.
- Chunk papers from PDF plus companion TeX when available, with PDF-only fallback when TeX is unavailable.
- Preserve audit state in an audit workdir next to the selected paper.
- Use the default continuous-conversation audit mode; `fresh_context_experimental` is available for experiments but remains explicitly experimental.
- Generate full, concise, and verification reports in Markdown, TeX, and JSON.
- Track report freshness so stale reports are visible after audit state changes.
- Run generated local Python verification scripts and view progress/results.
- Distinguish Python execution status from mathematical outcome; reported counterexamples are promoted as provisional findings in the full and concise reports.
- Rerun selected chunks or chunks with failed/timed-out verification results.
- Recheck completed counterexample/claim-failure findings with the full manuscript chunk, script, output, and structured counterexample evidence, without replacing the original chunk audit or deterministic finding.
- Repair parse errors, runtime errors, timeouts, and safety-policy failures at the script level before considering a full chunk re-audit. The repair request includes the complete failed script and failure evidence; users review generated replacements before explicitly confirming local safe-mode execution.
- When a recheck identifies a flawed verification script, review any proposed corrected or independent replacement checks before running them locally in safe mode; original scripts/results remain preserved and replacement outcomes remain provisional.
- Ask post-audit questions in the Discussion pane, with saved thread history and rendered Markdown/math output.
- Export a one-way ChatGPT context pack for continuing discussion in the regular ChatGPT app without making further API calls through the app.
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

### Easy Launchers

For the easiest startup path:

1. Install Miniforge from the [conda-forge download page](https://conda-forge.org/download/) or the [Miniforge GitHub repository](https://github.com/conda-forge/miniforge), unless you already have a compatible Anaconda, Miniconda, Miniforge, or Mambaforge installation with `conda` or `mamba` available.
2. Download/unzip this repository or clone it.
3. On macOS, double-click `run_math_audit.command`. If Gatekeeper blocks it, right-click it and choose **Open**.
4. On Windows, double-click `run_math_audit.bat`.
5. The launcher will create or reuse the `math-audit` Conda environment from `environment.yml`, run the setup check, and open the GUI.
6. Paste your OpenAI API key into the GUI, then select a paper PDF.

The launcher does not bundle Python. It searches for Conda/Mamba in `PATH` and common installation locations, then installs required Python/GUI dependencies into the `math-audit` environment automatically. Users should not manually install PySide6, Qt WebEngine, the OpenAI SDK, or PDF packages one by one. Very old or heavily customized Conda installations may have package-solving problems; if that happens, rerun the launcher, refresh the environment, or try a clean Miniforge installation. Windows currently uses the tested PySide6 6.9.3 package set, and the Windows launcher checks Qt imports before opening the GUI. The current Microsoft Visual C++ Redistributable x64 is recommended. Windows support remains experimental and less tested than macOS. Linux users should use the manual Conda setup for now. A packaged `.app` or installer is a future milestone; the `.command` and `.bat` launchers are the current public-preview convenience paths.

New audits default to GPT-5.6 Sol with `xhigh` reasoning effort for serious research-level mathematical auditing. GPT-5.5 remains selectable for comparison and compatibility with older audits. GPT-5.6 Sol `max` effort is available for the hardest quality-first audits or focused rechecks, but it is not the global default. Existing audits resume with their saved model and effort.

At a high level:

> **Important: use searchable PDFs, not scanned PDFs.** Best results come from PDFs compiled from TeX/LaTeX, but any PDF with good selectable text may work. The app does not perform OCR, so scanned/image-only PDFs can produce empty or corrupted chunks and unreliable audits.

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

- The app can find real issues, but generated audits and verification checks can be incomplete or mistaken. A completed script may find a counterexample, and a finite search with no counterexample is not a proof of an unrestricted claim.
- The `fresh_context_experimental` context mode is still experimental. It can help with long audits or context/file-service robustness, while continuous mode may be cheaper for short PDF-only audits with good cache reuse.
- PDF-only chunking depends on text extraction quality; scanned/image-only PDFs are not recommended because the app does not perform OCR.
- TeX-aware chunking is preferred when a companion `.tex` file is available and covers the paper reliably.
- Report TeX is intended to be robust, but local LaTeX installations still vary.
- The GUI discussion rendered mode depends on bundled local MathJax assets.
- Pricing/cost estimates are local calculations and may differ from platform billing in edge cases, including cache-write charges when those token counts are not exposed in usage metadata.

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

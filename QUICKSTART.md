# Quickstart

This guide gets the experimental Math Paper Audit GUI running from a fresh checkout. For a fuller screenshot-based walkthrough, see [`docs/user_guide.md`](docs/user_guide.md).

## 1. Easy Launchers

Math Paper Audit is a Python app distributed as a research-preview source package. The launcher scripts do not bundle Python; they need a working Conda or Mamba command so they can create the `math-audit` environment from `environment.yml`.

If you already have Anaconda, Miniconda, Miniforge, or Mambaforge installed, you can usually skip installing Miniforge as long as `conda` or `mamba` is available. The launcher searches in your shell `PATH` and common installation locations.

For new users, Miniforge is recommended because it is lightweight and uses conda-forge by default:

- [conda-forge download page](https://conda-forge.org/download/)
- [Miniforge GitHub repository](https://github.com/conda-forge/miniforge)

Choose the installer for your machine: macOS Apple Silicon uses macOS arm64, Intel Macs use macOS x86_64, and most Windows users should choose Windows x86_64. You may need to restart Terminal or Command Prompt after installing Miniforge.

Miniforge needs about 400 MB for installation. The downloaded Math Paper Audit source folder is about 80 MB after unzipping. It contains the app source code, documentation, screenshots, launcher scripts, and bundled local GUI assets, but it does not include the installed Python/GUI libraries needed to run the app. On first launch, Conda creates a separate `math-audit` environment from `environment.yml` and downloads packages such as PySide6, Qt WebEngine, the OpenAI SDK, PDF-processing libraries, NumPy, SymPy, and Markdown. This environment can require several additional GB of disk space. We recommend at least 5 GB of free disk space, and 10 GB if possible. A full LaTeX distribution, if installed separately for PDF report compilation, requires additional space.

Then use the easiest startup path:

1. Download/unzip this repository or clone it.
2. On macOS, double-click `run_math_audit.command`. If Gatekeeper blocks it, right-click it and choose **Open**.
3. On Windows, double-click `run_math_audit.bat`.
4. Wait while the launcher creates or reuses the `math-audit` Conda environment.
5. Paste your OpenAI API key into the GUI, then select a paper PDF.

The launcher runs `python scripts/check_setup.py` before opening the GUI. It does not store or request your API key and does not run an audit by itself.

Required Python/GUI packages such as PySide6, Qt WebEngine, the OpenAI SDK, and PDF packages are installed automatically into the `math-audit` environment from `environment.yml`; they are not part of Miniforge itself. If setup reports a missing required package, rerun the launcher or use the manual update command below.

Existing Anaconda or Miniconda installations usually work, but very old, heavily customized, or misconfigured Conda installations may cause package-solving or environment-creation problems. If package conflicts persist, a clean Miniforge installation is often the easiest recovery path.

Windows support is experimental and less tested than macOS. Linux users should use the manual Conda setup below for now. A packaged `.app` or installer is a future milestone; for now, `run_math_audit.command` and `run_math_audit.bat` are the public-preview convenience launchers.

## 2. Manual/Developer Conda Setup

From the project root:

```bash
conda env create -f environment.yml
```

Activate it:

```bash
conda activate math-audit
```

If you later update `environment.yml`, refresh the environment with:

```bash
conda env update -f environment.yml --prune
```

You can run a lightweight setup check before launching the GUI:

```bash
python scripts/check_setup.py
```

This does not run an audit or make an OpenAI API call. Missing optional items such as `OPENAI_API_KEY` or `pdflatex` are reported as warnings.

Do not manually install individual app packages unless you are debugging. If the environment is stale or incomplete, refresh it with:

```bash
conda env update -f environment.yml --prune
```

LaTeX is optional and separate. Install MacTeX, MiKTeX, or TeX Live yourself if you want to compile generated `.tex` reports into PDF; the launcher does not install a TeX distribution.

## 3. Configure an OpenAI API Key

The GUI has an API key field in the setup area. Paste your key there before starting/resuming an audit or using the discussion pane.

An OpenAI API key is a private access token for the OpenAI API, not the same thing as being logged into ChatGPT in a browser. You can create/manage keys using OpenAI's official links:

- [Where do I find my OpenAI API Key?](https://help.openai.com/en/articles/4936850-where-do-i-find-my-openai-api-key)
- [OpenAI API Keys](https://platform.openai.com/api-keys)

Live audits and live discussion calls can incur costs on your OpenAI API account. Setup checks and reading existing reports do not require a key.

Do not share your key, include it in screenshots, or commit it to Git. The full secret key is only shown when it is created; if it is lost or exposed, create a new one and revoke/delete the old one.

For backend or developer maintenance work, you can also set:

```bash
export OPENAI_API_KEY="your_api_key_here"
```

The current GUI live-call guard expects the key to be entered in the GUI field.

## 4. Launch the GUI Manually

From the activated environment:

```bash
python audit_gui.py
```

The PySide6 app is the primary maintained frontend for this public preview.

## 5. Basic Workflow

> **Important: use searchable PDFs, not scanned PDFs.** Best results come from PDFs compiled from TeX/LaTeX, but any PDF with good selectable text may work. The app does not perform OCR, so scanned or image-only PDFs may produce empty, incomplete, or corrupted chunks. A quick test is to open the PDF and try to select/copy a paragraph or formula.

1. Choose a paper PDF with **Browse...**.
2. Optionally place a same-basename TeX file next to the PDF, for example `paper.pdf` and `paper.tex`.
3. Use the default GPT-5.6 Sol / `xhigh` setting for serious research-level audits, or select GPT-5.5 for comparison/compatibility with older runs. GPT-5.6 Sol `max` is available for the hardest quality-first audits or focused rechecks, but it is not the default.
4. Click **Start Fresh Audit**.
5. Monitor progress, cost, chunks, pages, and status in the GUI.
6. Use **Quick review** to build/open a concise report.
7. Run the verification suite and inspect verification progress/results.
8. For technical script failures, use **Generate Repair Scripts** first. Re-auditing the manuscript chunk is a separate fallback. Use **Recheck Counterexample Chunks** when a completed script found a counterexample/claim failure.
9. Rebuild/open final reports after verification and repairs.
10. Optionally export a ChatGPT context pack for manual handoff outside the app.

Existing audits resume with the model and reasoning effort saved in their audit session; selecting a new default does not silently migrate old runs.

## Reports

Reports are written under the audit workdir, usually:

```text
paper_audit/reports/
```

The GUI can open the generated TeX report files and the reports folder. To compile TeX reports outside the app, use TeXShop or command-line `pdflatex` if installed.

## Verification

The audit can generate local Python verification scripts. The GUI distinguishes execution status (completed, runtime error, timeout, skipped) from mathematical outcome (for example, counterexample found, no counterexample found in the tested range, check satisfied, or inconclusive).

Successful Python execution does not mean the paper's claim succeeded: a completed script may find a counterexample. Likewise, a finite search with no counterexample does not prove an unrestricted theorem. Verification-derived findings are provisional supporting evidence, are surfaced in the main reports, and still require mathematical judgment.

Technical failed-verification reruns are for timeouts, runtime/parse errors, and similar execution problems. **Recheck Counterexample Chunks** is a separate API-backed review for scripts that completed and reported `counterexample_found` or `claim_failed`. It sends the full affected chunk, complete script, exact output, structured counterexample data, linked issues, labels, and compact surrounding context to the audit session's saved model/effort by default. The original script, result, and finding remain preserved. Possible advisory outcomes include confirmed counterexample/claim failure, script error, scope/hypothesis mismatch, notation/interpretation mismatch, or inconclusive. Rechecks incur API cost and still require human mathematical review.

For `parse_error`, `runtime_error`, `timeout`, or a safety-policy rejection, **Generate Repair Scripts** is the preferred first action. The API repair request receives the complete failed script, traceback/parser/safety/timeout evidence, exact output, complete manuscript chunk, labels, and linked issues. It must return corrected replacement code or explain why repair is unavailable. Review the complete code, then explicitly confirm local execution. Local execution uses no API call, never overwrites the original script/result, and remains provisional. A finite successful search does not prove a universal theorem. Use full chunk re-audit only as a separate fallback when repair is unavailable, repeatedly fails, or reveals a broader chunk-analysis problem.

If the recheck identifies a script error, it may propose corrected and independent replacement checks. Review the complete code first, then explicitly confirm **Run Safe Replacement Checks**. Replacement execution is local, uses the existing safe-mode checks, and does not make an API call. The original script/result and all replacement attempts remain preserved. A finite replacement search reporting no counterexample does not prove an unrestricted theorem, and conflicting replacement outcomes require human review.

## Experimental Context Modes

The default context mode is the stable continuous-conversation audit flow. The `fresh_context_experimental` mode is still experimental: it can be useful for long audits or for reducing dependence on a single long conversation/file-service state, but continuous mode may be cheaper for short PDF-only audits when context-cache reuse is good.

## Public Release Hygiene

Do not commit audit outputs, paper PDFs/TeX sources, request/response logs, generated reports, verification scripts/results, rerun folders, or review sidecars. These artifacts may contain paper text, model responses, local filesystem paths, and sensitive review material.

## License

Math Paper Audit is released under the MIT License. See `LICENSE`.

## ChatGPT Context Pack Export

The Reports tab can export a handoff folder for manual use in the normal ChatGPT app. This is different from the in-app Discussion tab: exported ChatGPT context packs do not make additional API calls through this app and do not use this app's API key. ChatGPT usage is governed by your ChatGPT plan, file-upload limits, usage limits, and usage policies.

Default handoff contents include:

- `audit_context.md`
- `paper_structure.json`
- the paper PDF
- the TeX source if available and selected
- selected Markdown reports if available and selected

Use **Copy Starter Prompt** after export, start a new ChatGPT conversation, paste the prompt, and attach the exported files to the same conversation. This gives ChatGPT context about the paper structure, audit findings, report summaries, and verification information so you can keep asking questions there.

The export is one-way only. It does not upload files, automate a browser, sync responses back into the app, or import ChatGPT answers into audit state. Exported files can contain manuscript text and sensitive audit findings, so only upload them to ChatGPT if you are allowed to share that material there.

## Troubleshooting

### Missing PySide6 or QWebEngine

If launch fails with an import error for `PySide6`, `QtWebEngineCore`, or `QtWebEngineWidgets`, reinstall/update the environment:

```bash
conda env update -f environment.yml --prune
```

The environment uses the PySide6 wheel because the GUI needs Qt WebEngine for rendered discussion output.

### Missing API Key

If live audit/discussion actions do nothing except log an API-key warning, paste your key into the GUI API key field. Exporting reports and reading saved status do not require an API key.

### `pdflatex` Not Found

The app generates `.tex` reports but does not install a TeX distribution. On macOS, install MacTeX or BasicTeX if you want to compile reports locally.

You can still read Markdown and JSON reports without LaTeX.

### MathJax or Rendered Discussion Issues

Raw discussion mode always preserves the exact answer text. Rendered mode depends on local assets under:

```text
gui_assets/mathjax/
gui_assets/mathjax-fonts/
```

If rendered math is unavailable or incomplete, use Raw mode and confirm those local assets are present.

### PDF-Only Fallback Behavior

If no same-basename `.tex` file is found, or if TeX parsing does not provide reliable full-paper coverage, the app falls back to PDF text extraction. PDF-only audits can work well, but chunk labels and mathematical context depend on the quality of extracted PDF text.

### Existing Audit State

For `paper.pdf`, the app uses `paper_audit/` as the workdir. If that folder already exists, **Resume Audit** continues from saved state. **Start Fresh Audit** may archive or replace state depending on the runtime path and current app behavior, so preserve important audit folders before experimenting.

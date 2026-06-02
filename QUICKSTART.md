# Quickstart

This guide gets the experimental Math Paper Audit GUI running from a fresh checkout. For a fuller screenshot-based walkthrough, see [`docs/user_guide.md`](docs/user_guide.md).

## 1. Create the Conda Environment

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

## 2. Configure an OpenAI API Key

The GUI has an API key field in the setup area. Paste your key there before starting/resuming an audit or using the discussion pane.

For backend or developer maintenance work, you can also set:

```bash
export OPENAI_API_KEY="your_api_key_here"
```

The current GUI live-call guard expects the key to be entered in the GUI field.

## 3. Launch the GUI

From the activated environment:

```bash
python audit_gui.py
```

The PySide6 app is the primary maintained frontend for this public preview.

## 4. Basic Workflow

1. Choose a paper PDF with **Browse...**.
2. Optionally place a same-basename TeX file next to the PDF, for example `paper.pdf` and `paper.tex`.
3. Click **Start Fresh Audit**.
4. Monitor progress, cost, chunks, pages, and status in the GUI.
5. Use **Quick review** to build/open a concise report.
6. Run the verification suite and inspect verification progress/results.
7. Rerun failed-verification chunks if needed.
8. Rebuild/open final reports after verification and repairs.
9. Optionally export a ChatGPT context pack for manual handoff outside the app.

## Reports

Reports are written under the audit workdir, usually:

```text
paper_audit/reports/
```

The GUI can open the generated TeX report files and the reports folder. To compile TeX reports outside the app, use TeXShop or command-line `pdflatex` if installed.

## Verification

The audit can generate local Python verification scripts. The GUI shows how many scripts exist, which script is running, and PASS/FAIL/TIMEOUT/SKIPPED outcomes.

Verification scripts are heuristic support evidence. They are not a substitute for mathematical judgment.

## Experimental Context Modes

The default context mode is the stable continuous-conversation audit flow. The `fresh_context_experimental` mode is still experimental: it can be useful for long audits or for reducing dependence on a single long conversation/file-service state, but continuous mode may be cheaper for short PDF-only audits when context-cache reuse is good.

## Public Release Hygiene

Do not commit audit outputs, paper PDFs/TeX sources, request/response logs, generated reports, verification scripts/results, rerun folders, or review sidecars. These artifacts may contain paper text, model responses, local filesystem paths, and sensitive review material.

## License

Math Paper Audit is released under the MIT License. See `LICENSE`.

## ChatGPT Context Pack Export

The Reports tab can export a handoff folder for manual use in ChatGPT. The export is one-way only. It does not upload files, automate a browser, or sync responses back into the app.

Default handoff contents include:

- `audit_context.md`
- `paper_structure.json`
- the paper PDF
- the TeX source if available and selected
- selected Markdown reports if available and selected

Use **Copy Starter Prompt** after export and paste the prompt manually into ChatGPT with the exported files attached.

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

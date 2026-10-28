# Prompt architecture and offline model/prompt comparison

Verified against official OpenAI documentation on **2026-10-28**. This change improves prompt organization and makes mathematical judgment criteria explicit; it does not establish that the new prompts or models improve MATH_AUDIT's accuracy. Findings remain provisional until the existing verification and canonical adjudication workflow accepts them.

## Sources and scope

The [GPT-6 guide](https://developers.openai.com/api/docs/guides/latest-model) recommends outcome-focused instructions and evaluated migrations. The [prompting guide](https://developers.openai.com/api/docs/guides/prompting) recommends code-managed, versioned prompts, stable roles, and task-specific user content. The [prompt-engineering guide](https://developers.openai.com/api/docs/guides/prompt-engineering) explains message hierarchy and structured outputs. These principles guide the organization below; the mathematical requirements come from MATH_AUDIT's observed failures and auditing workflow.

No paid comparison or live API request was performed. The offline tests check contracts and application behavior, not mathematical quality.

## Previous production architecture

- `audit_prompts.py` supplied the shared developer prompt, custom defaults/model overrides, and SHA-256 session snapshots. The snapshot had no explicit version, and subsidiary prompt revisions were not covered.
- `audit_policy_hooks.py::build_user_message_for_chunk` supplied chunk instructions, a repeated verification contract/example, reference-numbering rules, compact continuous context or fresh retrieved context, a macro glossary, and manuscript text.
- `audit_runtime.py` seeded the developer prompt into each conversation, added verification-mode instructions, and attached the unchanged strict audit schema. Reruns reused this machinery. Discussion used a separate short developer prompt. Verification recheck/repair calls supplied specialized developer instructions and JSON schemas.
- `audit_verification.py` built detailed evidence packages and specialized user prompts for negative-result rechecks and technical repairs. Complete scripts, stdout/stderr, counterexamples, and original failure evidence were already retained.
- `scripts/run_issue_family_recheck.py` provided an advisory family-reconciliation prompt and its own schema. Canonical clustering is deterministic proposal preparation plus explicitly accepted lifecycle events; it is not an autonomous model verdict. Reports already select accepted canonical dispositions when their readiness conditions are met. No separate model-driven global synthesis was added.

The core contained unnecessary GUI/notebook descriptions and repeated formatting instructions. The main and chunk prompts repeated verification contracts. Mathematical challenge criteria were less explicit about reconstructing the entire argument, refuting the objection, local rate-preserving repairs, and false intermediate steps whose theorems survive. The verification recheck text called a recorded finding deterministic evidence without emphasizing that its mathematical interpretation could be wrong. Source hierarchy and external-source uncertainty needed explicit treatment. These are identified instruction gaps, not proof that earlier model findings were wrong.

## Revised separation

1. **Core developer prompt:** compact mathematical role, high-recall discovery, epistemic/source standards, consequence-based severity, candidate lifecycle, output formatting, and incremental ledger rules.
2. **Chunk user task:** chunk identity and location, strict schema request, a single detailed `VERIFICATION_GENERATION_CONTRACT`, source-numbering guidance, mode-specific context, and manuscript evidence. The manuscript is visibly delimited as evidence. The continuous compact ledger and fresh-context retrieval behavior are retained.
3. **Specialized adjudication:** shared `MATHEMATICAL_ADJUDICATION_INSTRUCTIONS` supplement negative-result and family rechecks. Technical repairs keep their focused failure-specific instructions and original-script preservation. Discussion gets source and lifecycle standards without pretending it is an accepted adjudication.
4. **Output contracts:** audit, verification-recheck, repair, and family schemas are unchanged. Mathematical categories are expressed in existing prose/tags fields; no parser-changing fields were introduced.
5. **Provenance:** human-readable versions and SHA-256 hashes describe the main prompt, supporting constants, exact builder source, and actual generated request input. Source hashes intentionally change when a builder's implementation changes, even if the resulting prompt is equivalent.

The core prompt is shorter than the frozen baseline, while the task-specific verification contract remains present on every chunk. New instructions preserve granular discovery and distinguish false theorems, false intermediate assertions, invalid proofs of possibly true results, locally repairable gaps, missing hypotheses, notation defects, extraction artifacts, redundant steps, context-resolved objections, and external-source uncertainty. Severity depends on demonstrated consequence and repairability. Location in a theorem and repeated downstream occurrences cannot alone justify high severity.

## Safeguards retained

- Exact formulas/source labels from TeX, compiled numbering from AUX, and submitted-document identity from PDF; corrupted extraction and incomplete source coverage remain explicit limitations.
- Fine-grained claims, hypotheses, quantifiers, regimes, endpoints, equalities/inequalities, dependencies, counterexamples, source locations, and minor copyediting findings remain in scope.
- Every generated local check emits the strict `MATH_AUDIT_VERIFICATION_RESULT_JSON=` result. Execution status and mathematical outcome remain separate. Counterexamples cannot become passes because return code is zero. Finite searches cannot prove unrestricted statements; exact arithmetic and numerical heuristics are distinguished.
- Technical repair preserves original scripts and failure evidence; complete recheck evidence remains available. Safe-mode execution policies and parser enforcement are unchanged.
- Candidate output cannot accept canonical decisions, verify repairs, hide verification findings, or overwrite lifecycle events. Final-report preparation uses accepted canonical state only when complete.

## Prompt versions and historical sessions

The default core version is `math-audit-2026-10-28-v2`; the suite is `math-audit-suite-2026-10-28-v2`. Custom prompt content is labelled `custom-content-hash`, with its exact SHA-256. `prompt_suite_metadata()` records supporting constants and builder-source hashes. `request_prompt_metadata()` hashes the ordered JSON message input including dynamic evidence. Request metadata describes the builders actually used for that call; it does not relabel an old session as having used new subsidiary prompts.

Fresh sessions receive the new metadata through `prompt_snapshot_metadata`. Loading an old session must not rewrite its existing prompt, version, hash, context mode, model, effort, or mode. A later request's exact metadata is additive provenance. Missing historical version fields mean unknown historical version, not the current version. Saved custom prompts remain respected.

The offline baseline in `docs/prompt_baselines/production_2026-10-28/` contains:

- the exact previous shipped core prompt;
- the original Git revision, source-module hashes, and unchanged schema fingerprints;
- `prompt_inventory.json.gz`, an exhaustive original Python literal/f-string inventory for production prompt-bearing modules and clustering/family helpers. It includes nonprompt literals deliberately so no hidden request text is omitted. Dynamic manuscript evidence is excluded. The gzip timestamp is fixed for reproducibility.

The baseline contains no API keys, personal paths, or email addresses found by the migration privacy scan. It is historical reference material, not executable configuration.

## Reproducible evaluation

Prepare an offline manifest and blank reviewer labels:

```bash
python scripts/prepare_model_prompt_evaluation.py --output /path/outside/examples/new-evaluation
python scripts/check_prompt_regressions.py
```

The preparation script never imports or calls an API client, runs verification scripts, or writes to the source audits. It refuses outputs inside `examples/` and refuses to overwrite an existing evaluation directory. It hashes available historical state, prompts, and check evidence and records saved configuration without modifying it.

The five evidence sets are JNT PDF-only, Dickman/JNT TeX-aware, LMJ verification, Stirling-large-k TeX-aware, and elementary de-Poissonization. PDF-only and TeX-aware runs are separate source conditions; do not attribute a difference in manuscript/source access to model quality. Existing JNT/Dickman revisions must be compared only after checking their source equivalence.

Use three arms after explicit paid-evaluation approval:

| Arm | Model | Prompts | Attribution |
| --- | --- | --- | --- |
| A | Saved historical model | Original saved production requests/results | Existing baseline; no paid replay required |
| B | Current model, fixed supported effort/mode | Frozen original core and task builders | Model migration alone |
| C | Same current model, effort/mode | Modernized core and task builders | Prompt changes relative to B |

For B, start from the recorded baseline Git revision plus the separable model-migration change; for C, apply prompt modernization as well. Preserve chunk manifests, source bytes, schema, cache/tier settings, and context mode. Use new output directories and independent conversations/ledgers per arm. Continuous mode needs the ordered prior context; an isolated late-chunk request is not equivalent. Repeat approved uncertain comparisons at least three times and retain exact requests, response IDs, usage, cost estimates, elapsed time, scripts, and mathematical outcomes.

Have reviewers blind to the arm assign independent canonical clusters and rationales. Measure substantive candidate recall, valid minor-issue recall, false-positive rate, counterexample correctness, local repair/rate preservation, duplicate/downstream treatment, external-source uncertainty, and severity calibration. Assess cost and latency per accepted independent issue; fewer raw findings alone is not improvement. Never hard-code historical finding counts into production decisions.

The Dickman raw audit has the cited 86-candidate record, but the independent comparison and its accepted substantive canonical dispositions are **not bundled** in this repository. The generated manifest points to candidate IDs as review questions for false intermediate estimates, rate-preserving repairs, context-refuted objections, duplicate/downstream findings, external sources, and local minor defects. Those anchors are not gold truth labels. Obtain and independently review the comparison evidence before using it to score mathematical accuracy. The regression suite's six lifecycle examples are explicit synthetic schema/semantics fixtures, not fabricated historical adjudications.

Pro mode, persisted reasoning, dynamic effort updates, and context compaction require separate controlled comparisons; they must not be changed at the same time as the B-versus-C prompt comparison.

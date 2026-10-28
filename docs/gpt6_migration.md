# GPT-6 migration record

Specifications verified on **2026-10-28** against official OpenAI documentation:

- [Model catalog](https://developers.openai.com/api/docs/models): exact API IDs and supported efforts.
- [GPT-6 migration and prompting guide](https://developers.openai.com/api/docs/guides/latest-model): request compatibility and migration policy.
- [Reasoning](https://developers.openai.com/api/docs/guides/reasoning): effort, standard/pro mode, token accounting, and persisted reasoning.
- [Prompting](https://developers.openai.com/api/docs/guides/prompting) and [prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering): code-managed prompts, role/task/evidence separation, and evaluation.
- [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching): 30m/12h retention, write usage fields, and migration away from `prompt_cache_retention`.
- [Pricing](https://developers.openai.com/api/docs/pricing): Standard token rates and long-context pricing.
- [Background requests](https://developers.openai.com/api/docs/guides/background): storage, polling, and cancellation.

## Model and request policy

New audits offer only GPT-6.1 Sol (default, xhigh), GPT-6 Astra (xhigh), GPT-6.1 Terra (high), and GPT-6 Luna (medium). Astra/Sol accept low, medium, high, xhigh, max; Terra/Luna also accept none. UI switches preserve a supported selected effort and otherwise explain the destination default. Request validation rejects unsupported settings; UI normalization is never applied to stored provenance. General vendor model positioning has not been validated comparatively for MATH_AUDIT.

Every Responses creation path uses the shared request policy: chunk audits, continuous/fresh context, selected reruns, counterexample and technical repair rechecks, discussion, issue-family rechecks, and the standalone context comparison runner. JSON schemas, response retrieval, polling, cancellation, storage, and existing code-interpreter fallback behavior remain. Requests omit sampling/logprob controls. Errors do not trigger model substitution. Background execution requires stored responses.

The installed SDK may predate the `prompt_cache_options` keyword. The application sends this documented API field through `extra_body`, with offline tests of the actual SDK serialization. No dependency upgrade or live request is needed for that compatibility path. The stable prompt prefix and compact running ledger remain. Automatic caching uses a 30m TTL and Standard processing (`service_tier: default`); explicit breakpoints and a 12h UI option are deferred.

Pro mode is documented independently of effort. New sessions record standard mode. The backend preserves and sends a current session's saved standard/pro mode, and never invents a Pro model ID. The GUI does not offer Pro selection and blocks continuation of a saved nonstandard mode with an explanation. End-to-end GUI selection, per-recheck choice, and quality/cost evaluation are deferred; changing effort to max does not enable Pro.

Requests explicitly use `reasoning.context: current_turn`. This deliberately avoids inheriting a newer model's persisted-reasoning default while evaluating this migration. Visible conversation history and compact running context still work. Persisted all-turn reasoning, dynamic-effort configuration updates, compaction, and new delegation features need separate controlled experiments because they can alter evidence available to a chunk. Existing context-mode metadata is not rewritten.

## Cost accounting

Standard short-context USD per million tokens:

| Model | Input | Cache read | 30m write | 12h write | Output |
| --- | ---: | ---: | ---: | ---: | ---: |
| GPT-6 Astra | 10 | 1 | 12.5 | 25 | 50 |
| GPT-6.1 Sol | 2 | 0.10 | 2.5 | 5 | 10 |
| GPT-6.1 Terra | 1 | 0.05 | 1.25 | 2.5 | 5 |
| GPT-6 Luna | 0.10 | 0.01 | 0.125 | 0.25 | 0.50 |

For requests with more than 272,000 input tokens, input and cache rates double and output rates multiply by 1.5 for the whole request. Cache reads and writes are subtracted from ordinary input before charging their own rates. Write rates are total rates, not surcharges. The documented `cache_write_tokens` and `cache_write_tokens_details.ttl_30m_tokens/ttl_12h_tokens` fields support mixed lifetimes. Missing or inconsistent usage marks the estimate approximate, with explicit limitations. Reasoning tokens are already part of output usage and are not charged twice. Pro's aggregated usage uses the chosen model's token rates.

The GUI exposes Standard processing only. The estimator recognizes verified Fast/Priority and Flex/Batch multipliers for explicit callers; unsupported tiers remain approximate. Regional premiums, tool/container charges, and taxes are not represented by the text-token estimate. Prices may change. Historical prices remain frozen for compatibility, and previously stored costs are never recomputed with this table.

## Historical audits

Reading old sessions preserves their model ID, effort/mode, prompt snapshots, response IDs, usage, costs, context mode, QA history, and existing report sidecars. Legacy or unknown sessions cannot issue new requests in this release, including through an explicit current-model override. An incomplete saved configuration also fails clearly. Pending legacy requests can still be cancelled; live recovery is disabled along with continuation.

A deliberate Start Fresh archives the prior workdir using the existing behavior, then creates a current-model session. Creating a session directly cannot overwrite an existing session without explicit archival. Changing current-session settings deliberately records the prior settings in append-only history; pending responses must first be recovered or cancelled. Prompt snapshots on resume are retained unless the caller explicitly supplies an override. New request records fingerprint the actual prompt input and prompt suite so later subsidiary-prompt changes remain identifiable.

## Comparison and validation

The production prompt baseline was captured before editing. See [prompt architecture and comparison plan](prompt_architecture.md). Model migration and prompt modernization are separately reviewable; do not attribute any change in mathematical findings to the model alone when prompts also changed.

All implementation checks are offline. `scripts/check_regressions.py` blocks network connections and includes model/pricing, SDK wire serialization, current-session settings, GUI model switching, byte-preserving historical reads, prompt contracts, source fixtures, verification outcomes, canonical lifecycle, reports, and Windows Qt compatibility checks. No new model has been shown to improve MATH_AUDIT mathematical accuracy by these tests. No paid audit is authorized by the evaluation preparation scripts.

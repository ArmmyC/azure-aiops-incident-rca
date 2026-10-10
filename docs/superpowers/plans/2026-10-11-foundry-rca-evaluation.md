# Foundry RCA Evaluation Implementation Plan

**Goal:** Add a versioned RCA response category and a local-first, cost-controlled evaluation harness for 24 labeled incidents.

**Architecture:** Keep agent inference, deterministic scoring, and LLM judging as separate commands. The prediction generator sends only whitelisted `incident` telemetry and pins `agent_reference.version`; it writes cached predictions keyed by case ID and agent version. The scorer reads the tracked JSONL dataset and cached predictions without network access. The judge is an opt-in, case-scoped Foundry model call with a separate cache. Azure-derived artifacts and caches stay ignored under `data/evaluations/`.

**Tech:** Python 3.12+, Pydantic, current Azure AI Projects/OpenAI clients, Pandas ETL, pytest.

## Task 1: Preserve v1 and capture the real 504 baseline

1. Verify the existing 404 fixture and resolve the Application Insights linked workspace without printing credential values.
2. Run the exact ETL `--operation-id` selection for the known 504 request. Save the raw local fixture separately and restore the existing 404 as `latest.json`.
3. Invoke agent version 1 once, save the response and any validation rejection separately, and inspect whether the category-level expected behavior is present without changing v1.
4. Stop if the 504 cannot be retrieved or the Foundry request cannot be tied to v1; do not guess an operation ID or silently call the latest version.

**Check:** The 504 fixture has request status 504, request duration near 1007 ms, `search_service` dependency duration near 1004 ms, and dependency success false. The pre-existing 404 fixture remains intact.

## Task 2: Add 24 labeled cases and v2 schema validation

1. Create `evaluation/` package, the 24-row JSONL dataset, and local data ignore rules.
2. Use the real 504 and 404 plus synthetic telemetry to create six cases per category.
3. Add a strict v2 Pydantic contract, category-specific telemetry guards, and scalar input whitelist fields for exception and latency baseline evidence. Preserve a v1 contract/parser.
4. Update shared agent instructions to require the v2 category while keeping the 404 abstention and evidence-only rules.
5. Add mocked tests for dataset composition, prompt isolation, version pinning, cache behavior, category evidence, schema rejection, and the four scenario groups.

**Check:** The JSONL has exactly 24 unique, finite, correctly labeled rows; only incident telemetry appears in agent input. The v2 validator rejects category/evidence mismatches and still abstains for the real 404.

## Task 3: Implement prediction generation and deterministic scoring

1. Add an explicit `--agent-version`, case selection, and limited-pilot mode to prediction generation.
2. Cache successful, schema-invalid, and validator-rejected responses by `(case_id, agent_version)` so re-runs do not repeat paid inference.
3. Keep inference payload creation centralized on the whitelisted incident input and exclude `expected` unconditionally.
4. Add an offline scorer for the requested metrics, valid-diagnosis coverage, per-case outcomes, denominators, and provisional target comparisons.
5. Ensure validator rejections remain separately reported from semantic category accuracy.

**Check:** Mocked prediction runs prove labels never enter the model input and a second run uses cache. Offline scoring produces stable metrics without Azure credentials.

## Task 4: Add a separate opt-in judge

1. Implement strict 1–5 judge result schema and rubric.
2. Require one case ID for a live judge call; read an existing prediction, then send the incident, prediction, expected label, and rubric to the judge model.
3. Cache judge outputs separately using case ID, agent version, and judge deployment; do not invoke the judge from generation or scoring.

**Check:** Mocked tests validate judge input fields, response schema, cache key, and non-invocation by ordinary evaluation commands.

## Task 5: Verify locally, then stage live v2 evaluation

1. Run focused evaluator tests and the full pytest suite, compileall, lock check, and diff check.
2. Confirm the remote agent state still has v1 as the latest version before creating v2; create exactly one v2 and record its returned version.
3. Generate predictions for the real 404 and 504 exactly once with v2; inspect and score those two before the pilot.
4. Run the five-case pilot across all four categories; reuse the real-case cache entries.
5. If accepted by the design's stability checks, generate only uncached cases for the 24-case benchmark and write the report. Do not invoke the judge during this step.

**Check:** Every live call is version-pinned and case-keyed; the 24-case report lists all counts, targets, and validation rejections. Full test suite remains green.

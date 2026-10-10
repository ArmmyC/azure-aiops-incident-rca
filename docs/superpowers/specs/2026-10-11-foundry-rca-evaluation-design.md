# Foundry RCA Evaluation Design

## Goal

Measure whether the Foundry RCA agent correctly classifies evidence-supported incidents, abstains on ambiguous telemetry, and grounds its evidence claims. Preserve the existing version 1 behavior as a baseline and add a version 2 response contract with a stable root-cause category.

## Approved approach

Build a local evaluation harness first. Keep the evaluator deterministic and make Foundry inference an explicit, version-pinned command. Add a separate, explicitly invoked LLM-as-a-Judge command; never call the judge automatically from prediction generation or scoring. Do not register a Microsoft Foundry custom evaluator in this step because that feature is preview and the supplied design calls for local evaluation first.

The labeled dataset contains 24 cases, six each for dependency timeouts, application exceptions with exception evidence, dependency latency regressions with a baseline, and insufficient or ambiguous telemetry. Include the existing real 404 and the selected real 504 incident; create the remaining cases locally. Keep Azure query artifacts, predictions, judge results, and evaluation reports under ignored `data/evaluations/`. Do not track actual Azure operation IDs in the dataset.

Only the incident telemetry object is sent to the RCA agent. Expected labels are passed only to the deterministic scorer or, when separately requested, the judge. Cache predictions by dataset case ID and agent version so re-scoring and the live pilot do not repeat inference.

## Versioned response contract

Version 1 remains available and retains its existing five-field contract. Version 2 adds required `root_cause_category`, which is one of `dependency_timeout`, `application_exception`, `dependency_latency`, or `null`. An insufficient-evidence report must have both `root_cause` and `root_cause_category` set to null. A diagnosed report must contain a non-empty root cause and a non-null category.

Version 2 includes whitelisted scalar telemetry for request exception type/code and dependency baseline duration. Unknown metadata, ground-truth labels, and operation IDs stay out of the model input. All telemetry strings remain untrusted data.

The deterministic validator requires category-appropriate evidence:

- `dependency_timeout`: HTTP 504, a named dependency, and `success=false`.
- `application_exception`: HTTP 500 and a non-empty exception type.
- `dependency_latency`: a named successful dependency whose duration is at least twice its baseline and at least 50 ms above baseline.

Every cited evidence token must still exactly match an input scalar using the existing JSON-rendered `path=value` convention. A 404 without correlated dependency evidence must abstain.

## Evaluation data and metrics

Each JSONL row has `id`, `incident`, and `expected`. `expected` contains only the expected status and category. Dataset validation checks the 24-case composition and rejects duplicate IDs, missing fields, invalid labels, and non-finite numbers.

The local scorer reports:

- Diagnosis accuracy: matching diagnosed categories divided by diagnosable cases with a parseable v2 prediction. It also reports the valid-diagnosis coverage so validator rejections cannot be hidden.
- Abstention recall: correctly abstained insufficient-evidence cases divided by all insufficient-evidence cases.
- False diagnosis rate: diagnosed predictions on insufficient-evidence cases divided by all insufficient-evidence cases.
- Schema validity: strict v2 reports divided by all prediction attempts.
- Evidence grounding: supported exact `path=value` evidence tokens divided by all evidence tokens in parseable reports. The separate judge assesses the free-text explanation and unsupported prose claims.
- Validator rejection rate: evidence or telemetry-invariant rejections divided by all prediction attempts.

The report includes numerators and denominators, per-case results, and provisional targets (80% category accuracy, 90% abstention recall, 100% schema validity, and 0% false diagnoses). A 24-case benchmark is an engineering check, not a statistical reliability claim. A validator rejection remains separately visible and is not automatically relabeled as a wrong semantic category.

## Judge

The separate judge receives the incident, a prediction, the expected label, and the 1–5 rubric. Its strict JSON result includes a score, explanation, and unsupported claims. Judge results are not ground truth. The command is case-scoped, cached separately by case, agent version, and judge deployment, and never runs as part of the RCA prediction or deterministic scoring commands.

## Live-call sequence and cost controls

1. Preserve the current real 404 artifact and export the historical real 504 by its exact operation ID. Pin and call version 1 once for the 504, recording its output and any validator rejection separately.
2. Build the full dataset and mocked tests locally; run the complete Python test suite before publishing a managed agent version.
3. Create version 2 exactly once, then call it once for the real 404 and once for the real 504.
4. Run a five-case pilot spanning all four categories. Reuse those two cached real-case predictions; inspect the pilot before expanding.
5. If the pilot passes the local schema/validator checks and the pipeline is stable, run the remaining uncached dataset cases once. Do not run the judge unless separately invoked.

All versioned Responses API calls explicitly include the requested agent version. Stop on missing Azure configuration, unavailable telemetry, unexpected remote version state, or any failed cost-control condition; do not create an extra agent version or repeat a paid request to recover an ambiguous response.

## Microsoft documentation

- [Foundry runtime components and agent references](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/runtime-components)
- [Custom evaluators (preview)](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/custom-evaluators)

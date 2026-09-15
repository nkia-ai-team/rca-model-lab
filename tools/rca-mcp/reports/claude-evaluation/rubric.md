# Retrospective reference-relative evaluation v1

This evaluates the 45 preserved Claude CLI results against scenario_metadata
in the corresponding /data/eval-cases case. Metadata is an author-written
reference of injection intent, NOT a validated statement of captured reality.
Do not expose these references to an investigator or rewrite original results.

Read the complete scenario_metadata and complete result.json for each case.
Score the report's adopted explanation using cause + causalChain + stopReason;
mentioning a correct cause only as an unadopted alternative is not a hit.

- match: adopted cause identifies the intended causal mechanism and location;
  equivalent wording is accepted. All independent roots must be identified for
  a multi-root case. Exact injection commands, source code details, and object
  identifiers not supported by telemetry are not required.
- partial: correct root location or a meaningful causal segment is identified
  but mechanism is missing, one independent root is missed, or a plausible
  incomplete explanation is explicitly qualified. Symptom overlap alone does
  not qualify if the adopted root cause contradicts the intended one.
- wrong: adopted root mechanism or location materially contradicts reference.
- abstained: no adopted causal explanation; insufficient evidence reported.
- disputed: reference and available evidence appear inconsistent enough that
  judging against reference would be unreliable. State the specific conflict.

Keep truth matching separate from empirical grounding. A correct answer may
be contaminated; a wrong answer may be honestly provisional. Do not invent a
best-achievable-status observability contract, target UUID, or causal proof.
Default grounding and confidence calibration to not_assessed; a report citing
refs is not proof those refs support the claims.

Per-case output fields: case, scenario_id, verdict, confidence (high/medium/low),
reference_summary, report_summary, rationale, missing_or_conflicting (array),
reported_status, grounding="not_assessed", source_paths (metadata and result).
Preserve Korean rationale. Include short exact report/reference excerpts if
needed. Never force match just because the model said confirmed.

Aggregation: strict match rate = match / all cases; report partial separately.
Show disputed and abstention rates, plus adjudicable rate excluding disputed.
Compute both capture-weighted and macro-by-scenario results. The two confirmed
experiment-label exposures (F03-H/F05-P) must also have a separate exclusion
sensitivity view; unflagged cases are not certified leak-free.

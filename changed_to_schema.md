definition_status (text)
Purpose:
Indicates whether a goal is fully defined and safe to be used by the logging and extraction pipeline.

Meaning:
- 'pending' — the goal was created, but its machine-readable definition has not yet been generated.
- 'ready' — the goal has a complete, validated definition and can be matched against user log entries.
- 'needs_clarification' — the goal is too vague to be measured (e.g. "be healthier") and requires a follow-up question.
- 'error' — definition generation failed due to a system or LLM error and may be retried.

Rules:
- Default value is 'pending'.
- Only goals with definition_status = 'ready' must be considered during log matching.
- Goals in any other state must be ignored by the logging pipeline.

---

definition_json (jsonb)
Purpose:
Stores the machine-readable semantic definition of a goal, generated automatically at goal creation time.

Expected structure:
- canonical_action: a normalized verb representing the goal's action (e.g. "run", "read", "lift").
- keywords: a list of synonyms and variants used to match user text (e.g. ["run", "running", "jog"]).
- allowed_units: a list of valid measurement units for this goal (e.g. ["km", "min", "pages"]).
- examples: example user phrases that correctly map to this goal (used for matching and disambiguation).
- needs_clarification: boolean indicating whether the goal lack_

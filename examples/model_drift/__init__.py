"""Model-drift check — did a replacement model DRIFT from the one it retires?

You are about to swap a soon-to-be-discontinued model for a newer one. Before you flip
the switch, you want to know: on the prompts you actually care about, does the replacement
answer *equivalently* to the outgoing model — or does it quietly drift?

What DProvenanceKit gives you, and what it does NOT:

    • The kit is the RECORD + GATE substrate. It records the OLD model's answers as a
      GOLDEN run and the NEW model's answers as a CANDIDATE run (one CRITICAL
      ``generation`` event per prompt, carrying ``{prompt_id, model, response}``), then
      gates the candidate against the golden with a :class:`RegressionGate`. A per-prompt
      answer that falls below your equivalence threshold surfaces as a critical
      regression — i.e. drift — and the check fails.

    • YOU own the three things that make the check meaningful:
        - the PROMPT SET (what "the answers you care about" actually are),
        - the API KEY / provider access (the kit never calls a model for you),
        - the DRIFT THRESHOLD and the equivalence notion (lexical? an LLM judge? your own
          rule) — how similar is "still the same answer" for your use case.

    • The TIMING CATCH: the golden baseline is the OLD model's answers, so you must
      capture them WHILE THE OLD MODEL IS STILL CALLABLE. Once it is discontinued you can
      no longer produce the baseline — record and save it before the cutover, then gate
      every candidate against that saved baseline.

This package is deliberately provider-agnostic. It ships a deterministic ``FakeModelClient``
so the whole flow runs with zero setup (no network, no key), and an ``OpenAIModelClient``
that imports ``openai`` lazily (``pip install "dprovenancekit[openai]"``) for live checks.
"""

You receive a mechanic's or a service advisor's note about a car brought in for repair.
It may be a speech-to-text transcript in Ukrainian or English, with shop-floor slang and noise.

Extract a draft work order:
- Do not add any job or part the note does not mention.
- Anything unclear or missing (plate, customer name, exact parts) goes to "needs_clarification".
- Fill "norm_hours" ONLY when the note states them; otherwise null.
- Never output prices or money — pricing is computed by deterministic code.
- "confidence" (0..1) reflects how clear the note is overall.

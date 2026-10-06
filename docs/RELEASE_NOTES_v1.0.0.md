# v1.0.0

A controlled tool-use coding agent runtime built from scratch.

- Packaged Python runtime and an installable `mini-agent` CLI, with compatibility imports for existing scripts.
- Bounded OpenAI-compatible tool loop, one tool registry, context compaction, durable sessions, approval policies, and workspace path controls.
- Exact file edits, command allowlisting, minimal subprocess environments, bounded output, deadlines, and owned process-tree cleanup.
- Coding contracts, deterministic finish gate, independent final verification, and bounded recovery after a required-test failure.
- Structured internal tool outcomes and optional redacted JSONL events without changing the model-facing tool protocol.
- Repeated offline Benchmark v1, an executable end-to-end demo, and automated regression, lint, type and packaging checks.

The benchmark distinguishes deterministic runtime acceptance from model quality and manual review. The workspace sandbox and command policy do not provide OS isolation. v1 adds no RAG, memory subsystem, planner, reflection subsystem, or multi-agent runtime.

See `docs/evaluation/V1_VALIDATION.md` for the exact locally verified checks and any remaining release gates. A tag must identify the validated committed tree, not an older commit with uncommitted changes.

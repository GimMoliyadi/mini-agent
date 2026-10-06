# Benchmark v1 fixtures

Benchmark runs use a fresh copy of the deterministic repository fixture in
[`eval/phase22_fixtures.py`](../../eval/phase22_fixtures.py). That fixture has
33 files, six known coding defects, fixed focused tests, and verifier-only
ground truth. Reusing it keeps the benchmark aligned with the existing
acceptance contract without copying another production-like repository into
the project.

`run_benchmark.py` adds one generated `docs/long_notes.txt` file for the three
long-file tasks. It is created inside each temporary workspace and is never
written to the source tree. Every task receives a clean fixture and no API
configuration, `.env`, sessions, or host environment secrets are copied into
the child process.

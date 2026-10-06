# Historical experiment artifacts

The JSON files from the Phase 22–24 experiments are retained here as
historical artifacts. They are useful for reproducing old investigations, but
their numbers are tied to their original fixture, model, and runtime commit.
They do not define v1.0 performance.

This directory is the canonical archive for those result files. Historical
scripts remain under `eval/` and read/write archived results here. Fixture
code, task inputs and the original `eval/results.json` baseline remain in
`eval/` for compatibility. New comparable results should use
[`benchmark/run_benchmark.py`](../../benchmark/run_benchmark.py) and its
versioned manifest.

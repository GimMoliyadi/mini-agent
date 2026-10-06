# v1.0 local validation and release gates

Initial hardening checks were validated on Windows, 2026-10-05. The offline
matrix, lint, type check and Benchmark v1 were rerun after cross-platform fixes
on 2026-10-07. These local results do not claim a successful remote GitHub
Actions run or live-model semantic quality.

| Check | Result |
| --- | --- |
| `python scripts/check.py`, Python 3.11.9 | 6/6 stages; 482 tests, 481 passed and one platform-condition skip |
| `python scripts/check.py`, Python 3.13.7 | 6/6 stages; 482 tests, 481 passed and one platform-condition skip |
| `python scripts/check.py --packaging-only` | 6/6; real sdist/wheel, offline wheel install, installed help/version/sessions |
| `python -m ruff check mini_agent benchmark examples tests scripts` | Passed |
| `python -m mypy --platform linux` / `--platform win32` | Passed, 27 production source files |
| Clean Python 3.11 editable installation | Passed without system site packages |
| Clean Python 3.13 locked editable/development installation | Passed without system site packages |
| `scripts/verify_quickstart.py` in both clean environments | Config, version, doctor, start, sessions, task passed using a local HTTP provider |
| Offline coding demo | Search, read, first edit, failing test, repair, passing test, finish gate, independent verifier passed |
| Benchmark v1, 36 schedules × 2 | 56 positive rows accepted; 16 intended negative rows rejected; zero runtime errors |
| JSONL privacy regressions | Private bodies and model text omitted; credentials redacted before JSON encoding; short credential, framing and symlink/hardlink controls checked |

The configuration smoke test substitutes stdin for `getpass` for its fictional
key, since Windows console input cannot be automated through pipes. It uses
the installed launcher and real configuration writer. `start` and `task`
exercise the installed entry point and real SDK against a local HTTP server.
No real API key or paid provider was used.

Benchmark reports retain unknown token usage as `null`; the separate scripted
estimate is not a provider cost measurement. Manual semantic review has 36
pending rows, and does not become complete when deterministic acceptance passes.
The 36 schedules reuse six fixture defects and navigation/policy controls.
All max-step rows and protected-test mutations are intentional negative controls.

Compatibility shims preserve the original imports, CLI scripts, registry identity
and monkeypatch points. Existing user awareness/configuration work was retained,
not introduced as a v1 feature. Two existing test fixtures were adapted without
weakening their assertions: configuration copies the package into its temporary
checkout; command success now runs a real test instead of relying on an empty
suite's exit code. Python 3.13's `NO TESTS RAN` stays unverified, never accepted.
An AST symbol audit found no missing original `main.py` or `tools.py` function
or class exports. After restoring four private helper re-exports, the focused
16-test search suite also passed with its one platform-condition skip.

The repeated benchmark exposed same-size, rapid source edits reusing stale
bytecode. Controlled test processes now use an independent, unwritten bytecode
prefix. A regression creates a stale cache and proves the rerun reads the new source.

The first remote CI run exposed three platform assumptions: backslash paths
on POSIX, Windows short temporary-directory names, and generated fixture
newlines differing from the saved byte snapshot. Path checks now handle both
separator styles, the test harness resolves its temporary root before building
child environments, and the historical recovery fixture pins its recorded CRLF
bytes. Existing assertions and immutable baseline evidence remain intact.

The runtime lint/type scope is the package, current benchmark, examples, tests and
release scripts. A broader exploratory lint run found 12 existing lint issues in
legacy `eval/` scripts; these are outside the configured CI lint scope. Python 3.13
also emits existing docstring escape warnings there; syntax compilation passes.

## Release requirements and remaining limitations

- The GitHub workflow tests Windows/Linux on Python 3.11/3.13. Remote results
  are authoritative at [GitHub Actions](https://github.com/GimMoliyadi/mini-agent/actions).
  This local record does not assert a remote job outcome. Linux/macOS Quick
  Start has not been run locally.
- The `v1.0.0` tag must point to the validated committed tree after all remote
  CI jobs succeed.
- Semantic/manual review and live-provider quality remain unverified.
- Docker execution is not implemented; path sandbox and command policy do not
  provide OS isolation. This release remains for trusted local projects.

Detailed release notes: [v1.0.0](../RELEASE_NOTES_v1.0.0.md).
Benchmark: [report](../../benchmark/reports/benchmark-v1.md).
Example: [redacted events](../../examples/example_trace.jsonl).

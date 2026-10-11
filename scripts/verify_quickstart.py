"""Exercise an installed CLI with a local provider and private temporary state."""

from __future__ import annotations

import argparse
from pathlib import Path
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_transport import loopback_provider, _completion  # noqa: E402

DUMMY_KEY = "quickstart-fictional-key"


def greeting(_provider, body):
    return 200, _completion(body["model"], {"role": "assistant", "content": "Hello from the local provider."}, "stop")


def verify(entry: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mini-agent-quickstart-", ignore_cleanup_errors=True) as directory:
        state = Path(directory) / "state"
        workspace = Path(directory) / "workspace"
        workspace.mkdir()
        environment = {name: value for name, value in os.environ.items()
                       if name.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}}
        environment.update(MINI_AGENT_STATE_DIR=str(state), AGENT_WORKSPACE=str(workspace),
                           PYTHONUTF8="1", NO_PROXY="*", MINI_AGENT_HTTP_PROXY="", TOOL_APPROVAL_MODE="DENY")
        with loopback_provider(greeting) as provider:
            def run(arguments: list[str], input_text: str = "") -> subprocess.CompletedProcess[str]:
                completed = subprocess.run([str(entry), *arguments], input=input_text,
                                           cwd=directory, env=environment, capture_output=True,
                                           text=True, encoding="utf-8", timeout=30, check=False)
                if completed.returncode:
                    raise RuntimeError(f"CLI {arguments[0]} failed with exit code {completed.returncode}")
                return completed

            # Windows getpass reads the console directly. For this unattended
            # smoke test only, inject stdin for the fictional key; exercise the
            # same installed launcher and configuration writer without a TTY.
            wizard = (
                "from mini_agent import configure, launcher\n"
                "setup = configure.configure\n"
                "configure.configure = lambda path: setup(path, secret_func=input)\n"
                "raise SystemExit(launcher.main(['config']))\n"
            )
            configured = subprocess.run([sys.executable, "-c", wizard],
                                        input=f"{provider.base_url}\noffline-provider\n{DUMMY_KEY}\n",
                                        cwd=directory, env=environment, capture_output=True,
                                        text=True, encoding="utf-8", timeout=30, check=False)
            if configured.returncode:
                raise RuntimeError("configuration wizard failed")
            if not (state / ".env").is_file():
                raise RuntimeError("config did not save temporary configuration")
            run(["version"])
            run(["doctor", "--workspace", str(workspace)])
            if provider.requests:
                raise RuntimeError("doctor unexpectedly called a provider")
            run(["start", "--workspace", str(workspace)], "Hello\nexit\n")
            if len(provider.requests) != 1:
                raise RuntimeError("start did not complete exactly one provider turn")
            run(["sessions"])
            run(["task", "--task", "Hello", "--workspace", str(workspace)])
            if len(provider.requests) != 2:
                raise RuntimeError("task did not complete exactly one provider turn")
    print("Quick Start passed: config, version, doctor, start, sessions, task; local provider only.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", required=True, type=Path, help="installed mini-agent executable")
    args = parser.parse_args()
    verify(args.entry.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

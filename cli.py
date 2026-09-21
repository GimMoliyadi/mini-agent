"""Run one task with a machine-readable result: python cli.py --task '...'"""
import argparse
from contextlib import redirect_stdout
import json
import sys

from openai import APIError

import main
from config import get_approval_mode, load_config


def run_task(task: str) -> dict:
    messages = [{"role": "system", "content": main.SYSTEM_PROMPT},
                {"role": "user", "content": task}]
    config = load_config()
    client = None
    try:
        client = main.build_client(config)
        with redirect_stdout(sys.stderr):
            reply = main.ask(client, config.model, main.build_model_context(messages))
            main.log_reply(1, reply)
            main.run_agent_loop(
                client,
                config.model,
                messages,
                reply,
                set(),
                main.approval_callback_for_mode(get_approval_mode()),
            )
        last = messages[-1]
        answer = last.get("content") if last.get("role") == "assistant" and not last.get("tool_calls") else None
        return {"status": "completed" if answer else "incomplete", "answer": answer,
                "error": None if answer else "Agent stopped without a final answer"}
    except KeyboardInterrupt:
        return {"status": "cancelled", "answer": None, "error": "Cancelled by user; completed file writes remain"}
    except (APIError, ConnectionError, TimeoutError, ImportError, ValueError) as exc:
        return {"status": "failed", "answer": None, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if client is not None:
            client.close()


def configure_stdout_utf8() -> None:
    """Keep machine-readable CLI output Unicode-safe on Windows and pipes."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")


def cli() -> int:
    parser = argparse.ArgumentParser(description="Run a single workspace task; logs go to stderr, JSON to stdout")
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    if not args.task.strip():
        parser.error("--task must not be empty")
    result = run_task(args.task)
    configure_stdout_utf8()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 130 if result["status"] == "cancelled" else 1


if __name__ == "__main__":
    raise SystemExit(cli())

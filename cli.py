"""Run one task with a machine-readable result: python cli.py --task '...'"""
import argparse
from contextlib import redirect_stdout
import json
import sys

from openai import APIError

from acceptance import (
    CodingTaskContract,
    TaskState,
    TaskStatus,
    coding_task_guidance,
    load_contract,
    snapshot_workspace,
    verify_contract,
)
import main
from config import get_approval_mode, load_config


def run_task(task: str, contract: CodingTaskContract | None = None) -> dict:
    if contract is not None:
        task = contract.instruction
    system_prompt = main.SYSTEM_PROMPT
    if contract is not None:
        system_prompt += "\n\n" + coding_task_guidance(contract)
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": task}]
    trace = main.CodingTaskTrace()
    initial_snapshot = (
        snapshot_workspace(main.WORKSPACE_DIR) if contract is not None else None
    )
    task_state = (
        TaskState(initial_snapshot=initial_snapshot or {})
        if contract is not None
        else None
    )
    config = load_config()
    if contract is not None and main.approval_needs_interactive_input(get_approval_mode()):
        return {
            "status": "failed",
            "answer": None,
            "error": main.NON_INTERACTIVE_APPROVAL_ERROR,
            "trace": trace.summary(),
        }
    client = None
    runtime_exception = None
    try:
        client = main.build_client(config)
        with redirect_stdout(sys.stderr):
            reply = main.ask(client, config.model, main.build_model_context(messages))
            main.log_reply(1, reply)
            loop_options = {"trace": trace}
            if contract is not None:
                loop_options["required_test"] = (
                    contract.test_command.command,
                    contract.test_command.args,
                    contract.test_command.cwd,
                )
                loop_options["contract"] = contract
                loop_options["task_state"] = task_state
                loop_options["verifier_enabled"] = True
            main.run_agent_loop(
                client,
                config.model,
                messages,
                reply,
                set(),
                main.approval_callback_for_mode(get_approval_mode()),
                **loop_options,
            )
    except KeyboardInterrupt:
        runtime_exception = "KeyboardInterrupt"
        if contract is None:
            return {"status": "cancelled", "answer": None,
                    "error": "Cancelled by user; completed file writes remain",
                    "trace": trace.summary()}
    except (APIError, ConnectionError, TimeoutError, ImportError, ValueError) as exc:
        runtime_exception = f"{type(exc).__name__}: {exc}"
        if contract is None:
            return {"status": "failed", "answer": None,
                    "error": runtime_exception,
                    "trace": trace.summary()}
    except Exception as exc:
        # Preserve the old generic-task behavior, but make a coding contract
        # report unexpected Runtime failures through the deterministic result.
        runtime_exception = f"{type(exc).__name__}: {exc}"
        if contract is None:
            raise
    finally:
        if client is not None:
            client.close()

    if task_state is not None and runtime_exception is not None:
        task_state.status = TaskStatus.ERROR
        task_state.unresolved_runtime_error = runtime_exception
        trace.set_task_state(task_state)

    last = messages[-1] if messages else {}
    answer = last.get("content") if last.get("role") == "assistant" and not last.get("tool_calls") else None
    if contract is None:
        return {"status": "completed" if answer else "incomplete", "answer": answer,
                "error": None if answer else "Agent stopped without a final answer",
                "trace": trace.summary()}

    acceptance = verify_contract(
        contract,
        main.WORKSPACE_DIR,
        initial_snapshot or {},
        task_state=task_state,
        agent_final_answer_present=answer is not None,
        agent_ran_required_test=any(
            event.get("tool") == "run_command"
            and event.get("executed")
            and _event_matches_test_command(event, contract)
            for event in trace.events
        ),
        max_steps_reached=trace.max_steps_reached,
        runtime_exception=runtime_exception,
    )
    return {
        "status": "completed" if acceptance["accepted"] else "incomplete",
        "answer": task_state.finish_message if task_state is not None else answer,
        "error": None if acceptance["accepted"] else "; ".join(acceptance["reasons"]),
        "trace": trace.summary(),
        "task_state": task_state.as_dict() if task_state is not None else None,
        "acceptance": acceptance,
    }


def _event_matches_test_command(event: dict, contract: CodingTaskContract) -> bool:
    try:
        arguments = json.loads(event.get("arguments", "{}"))
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        arguments.get("command") == contract.test_command.command
        and arguments.get("args", []) == list(contract.test_command.args)
        and arguments.get("cwd", ".") == contract.test_command.cwd
    )


def configure_stdout_utf8() -> None:
    """Keep machine-readable CLI output Unicode-safe on Windows and pipes."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")


def cli() -> int:
    parser = argparse.ArgumentParser(description="Run a single workspace task; logs go to stderr, JSON to stdout")
    parser.add_argument("--task")
    parser.add_argument("--contract", help="JSON Coding Task Contract")
    args = parser.parse_args()
    if not args.task and not args.contract:
        parser.error("one of --task or --contract is required")
    if args.task is not None and not args.task.strip():
        parser.error("--task must not be empty")
    contract = load_contract(args.contract) if args.contract else None
    result = run_task(args.task or contract.instruction, contract=contract)
    configure_stdout_utf8()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 130 if result["status"] == "cancelled" else 1


if __name__ == "__main__":
    raise SystemExit(cli())

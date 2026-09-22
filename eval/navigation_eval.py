"""Phase 18 repository-navigation eval runner.

The default entry point runs each scenario once in a fresh fixture workspace.
``--child`` runs one scenario inside the workspace supplied by AGENT_WORKSPACE.
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import tempfile

EVAL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from .navigation_fixtures import build_fixture, coding_contract, get_spec, validate_fixture  # noqa: E402
from .navigation_metrics import calculate_navigation_metrics, navigation_accepted  # noqa: E402


SCENARIOS = {
    "small_symbol_navigation": {
        "size": "SMALL",
        "task_type": "navigation-only",
        "task": "找到 calculate_discount 的实现，告诉我它在哪个文件。",
    },
    "medium_symbol_coding": {
        "size": "MEDIUM",
        "task_type": "coding",
        "task": "修复 calculate_discount 的错误，让相关测试通过。",
    },
    "large_symbol_coding": {
        "size": "LARGE-SYNTHETIC",
        "task_type": "coding",
        "task": "修复 calculate_discount 的错误，让相关测试通过。",
    },
    "medium_error_string_navigation": {
        "size": "MEDIUM",
        "task_type": "navigation-only",
        "task": "找到错误信息里 'invalid discount rate' 相关的问题，告诉我它在哪个文件。",
    },
}


def _required_test_args(contract: dict) -> tuple[str, tuple[str, ...], str]:
    test = contract["test_command"]
    return test["command"], tuple(test["args"]), test["cwd"]


def _required_test_ran(metrics: dict, contract: dict) -> bool:
    command, args, cwd = _required_test_args(contract)
    for event in metrics["tool_chain"]:
        if event["tool"] != "run_command":
            continue
        arguments = json.loads(event["arguments"])
        if (
            arguments.get("command") == command
            and tuple(arguments.get("args", [])) == args
            and arguments.get("cwd", ".") == cwd
            and event.get("exit_code") == "0"
        ):
            return True
    return False


def _run_child(scenario_name: str) -> dict:
    """Run the real Agent once and return JSON-safe evidence."""
    # Imports happen after AGENT_WORKSPACE is set by the parent process.
    import acceptance
    import main

    scenario = SCENARIOS[scenario_name]
    root = Path(os.environ["AGENT_WORKSPACE"]).resolve()
    spec = get_spec(scenario["size"])
    validate_fixture(root, spec)
    target_file = spec.target_file if scenario["task_type"] == "coding" or "symbol" in scenario_name else spec.error_file
    contract_data = coding_contract(spec) if scenario["task_type"] == "coding" else None
    model_replies = []
    messages = [
        {"role": "system", "content": main.SYSTEM_PROMPT},
        {"role": "user", "content": scenario["task"]},
    ]
    trace = main.CodingTaskTrace()
    runtime_errors = []
    client = None
    before = acceptance.snapshot_workspace(root) if contract_data else None
    real_ask = main.ask

    def recording_ask(llm_client, model_name, history):
        reply = real_ask(llm_client, model_name, history)
        model_replies.append(reply)
        return reply

    main.ask = recording_ask
    try:
        config = main.load_config()
        client = main.build_client(config)
        first_reply = main.ask(client, config.model, messages)
        main.log_reply(1, first_reply)
        required_test = _required_test_args(contract_data) if contract_data else None
        main.run_agent_loop(
            client,
            config.model,
            messages,
            first_reply,
            set(),
            main.always_allow,
            trace=trace,
            required_test=required_test,
        )
        model_name = config.model
    except BaseException as exc:  # Preserve a machine-readable failed run.
        runtime_errors.append(f"{type(exc).__name__}: {exc}")
        model_name = os.environ.get("OPENAI_MODEL", "unknown")
    finally:
        main.ask = real_ask
        if client is not None:
            client.close()

    metrics = calculate_navigation_metrics(model_replies, messages, target_file)
    metrics["runtime_errors"] = runtime_errors
    metrics["finish_reason"] = [getattr(reply, "finish_reason", None) for reply in model_replies]
    metrics["max_steps_reached"] = trace.max_steps_reached
    metrics["apply_patch_calls"] = trace.apply_patch_calls
    metrics["write_file_calls"] = trace.write_file_calls
    metrics["run_command_calls"] = trace.run_command_calls
    metrics["final_answer_present"] = metrics["final_answer"] is not None
    metrics["required_test_ran"] = _required_test_ran(metrics, contract_data) if contract_data else False

    acceptance_result = None
    if contract_data:
        contract = acceptance.CodingTaskContract.from_dict(contract_data)
        acceptance_result = acceptance.verify_contract(
            contract,
            root,
            before,
            agent_final_answer_present=metrics["final_answer_present"],
            agent_ran_required_test=metrics["required_test_ran"],
            max_steps_reached=trace.max_steps_reached,
            runtime_exception=runtime_errors[0] if runtime_errors else None,
        )
        accepted = acceptance_result["accepted"]
    else:
        accepted = not runtime_errors and not trace.max_steps_reached and navigation_accepted(metrics, target_file)

    return {
        "scenario": scenario_name,
        "repo_size": spec.name,
        "task_type": scenario["task_type"],
        "task": scenario["task"],
        "ground_truth_file": target_file,
        "fixture": spec.as_dict(),
        "model": model_name,
        "metrics": metrics,
        "trace": trace.summary(),
        "acceptance": acceptance_result,
        "accepted": accepted,
    }


def _run_one_subprocess(scenario_name: str, root: Path, timeout_seconds: int = 900) -> dict:
    environment = {
        **os.environ,
        "AGENT_WORKSPACE": str(root),
        "TOOL_APPROVAL_MODE": "ALLOW",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    completed = subprocess.run(
        [
            str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
            "-m",
            "eval.navigation_eval",
            "--child",
            scenario_name,
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        env=environment,
        timeout=timeout_seconds,
    )
    try:
        result = json.loads(completed.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {
            "scenario": scenario_name,
            "accepted": False,
            "process_exit_code": completed.returncode,
            "process_error": completed.stderr.decode("utf-8", "replace")[-4000:],
            "metrics": {},
        }
    result["process_exit_code"] = completed.returncode
    result["process_stderr"] = completed.stderr.decode("utf-8", "replace")[-4000:]
    return result


def run_all() -> dict:
    started_at = datetime.now().isoformat(timespec="seconds")
    results = []
    for scenario_name, scenario in SCENARIOS.items():
        with tempfile.TemporaryDirectory(prefix=f"navigation-{scenario['size'].lower()}-") as directory:
            root = Path(directory) / "workspace"
            spec = build_fixture(root, scenario["size"])
            validate_fixture(root, spec)
            results.append(_run_one_subprocess(scenario_name, root))
        print(
            f"[{len(results)}/{len(SCENARIOS)}] {scenario_name}: "
            f"accepted={results[-1].get('accepted')} "
            f"tokens={results[-1].get('metrics', {}).get('total_tokens')}",
            file=sys.stderr,
        )

    payload = {
        "phase": 18,
        "started_at": started_at,
        "planned_scenarios": len(SCENARIOS),
        "results": results,
        "workspace_final_state": "每个场景使用临时 fixture，源工作目录未修改",
    }
    output = EVAL_DIR / "navigation_results.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=sorted(SCENARIOS))
    args = parser.parse_args()
    if args.child:
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        try:
            result = _run_child(args.child)
        finally:
            sys.stdout = real_stdout
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    run_all()


if __name__ == "__main__":
    main()

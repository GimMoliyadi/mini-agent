"""仅从标准输入读取的觉察 CLI，不进入执行型会话流程。"""

import argparse
from contextlib import contextmanager
import json
import logging
import os
import sys
from typing import Any, NoReturn

from awareness import (
    AwarenessError, MAX_INPUT_CHARS, guidance_after_reply, input_guidance_state,
    organize, render_result, validate_input,
)

PRIVACY_NOTICE = "本次不保存会话、不提供工具；文字仍会发送给配置的模型服务商。请勿输入秘密。"
EXIT_COMMANDS = frozenset({"exit", "quit", "q", "退出", "结束对话", "结束聊天", "不聊了"})


class AwarenessArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise AwarenessError("arguments")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = AwarenessArgumentParser(
        prog="mini awareness", description="终端持续对话，仅在内存保留上下文；管道为单次整理，不提供工具或保存会话。",
        allow_abbrev=False,
    )
    parser.add_argument("--json", action="store_true", help="stdout 输出完整结构化结果而不是自然对话")
    return parser.parse_args(argv)


def read_input() -> str | None:
    if sys.stdin.isatty():
        print("请输入文字（回车提交，exit 或 退出结束；Ctrl+C 取消）：", file=sys.stderr)
        text = sys.stdin.readline(MAX_INPUT_CHARS + 1)
        if text == "":
            return None
        if len(text) > MAX_INPUT_CHARS and not text.endswith("\n"):
            # 清掉超长输入的剩余部分，避免将尾部自动当作下一轮消息。
            remainder = text
            while remainder and not remainder.endswith("\n"):
                remainder = sys.stdin.readline(MAX_INPUT_CHARS + 1)
    else:
        text = sys.stdin.read(MAX_INPUT_CHARS + 1)
    validate_input(text)
    return text


@contextmanager
def private_provider_logging():
    previous_disable = logging.root.manager.disable
    previous_setting = os.environ.pop("OPENAI_LOG", None)
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous_disable)
        if previous_setting is not None:
            os.environ["OPENAI_LOG"] = previous_setting


def execute(text: str, *, history: list[dict] | None = None, guidance_state: str = "off") -> dict:
    from launcher import prepare_environment

    prepare_environment(create=False)
    with private_provider_logging():
        from config import load_config
        from main import build_client

        try:
            config = load_config()
            validate_input(text)
            client = build_client(config)
        except AwarenessError:
            raise
        except (Exception, SystemExit):
            raise AwarenessError("configuration") from None
        try:
            return organize(client, config.model, text, history=history, guidance_state=guidance_state)
        finally:
            try:
                client.close()
            except (Exception, SystemExit):
                print("客户端清理未正常完成。", file=sys.stderr)


def emit(payload: dict, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    elif payload["status"] == "completed":
        print(render_result(payload["result"]))
    else:
        print(payload["error"]["message"], file=sys.stderr)
    return 0 if payload["status"] == "completed" else 130 if payload["status"] == "cancelled" else 1


def failure(error: AwarenessError) -> dict:
    return {
        "status": "cancelled" if error.code == "cancelled" else "failed",
        "result": None, "error": {"code": error.code, "message": str(error)},
    }


def main(argv: list[str] | None = None) -> int:
    from cli import configure_stdout_utf8

    configure_stdout_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parse_args(argv)
    except AwarenessError as exc:
        emit(failure(exc), as_json="--json" in argv)
        return 2
    interactive = sys.stdin.isatty()
    input_configured = False
    history: list[dict] = []
    guidance_state = "off"
    print(PRIVACY_NOTICE, file=sys.stderr)
    if interactive:
        print("可连续补充或纠正；觉察引导会先询问意愿，你可以拒绝或跳过。退出后不保存上下文。", file=sys.stderr)
    while True:
        try:
            if not input_configured:
                # TextIOWrapper 首次读取后不能重新配置编码。
                reconfigure = getattr(sys.stdin, "reconfigure", None)
                if reconfigure is not None:
                    reconfigure(encoding="utf-8", errors="strict")
                input_configured = True
            text = read_input()
            if text is None:
                print("已结束；没有保存本次会话。", file=sys.stderr)
                return 0
            if interactive and text.strip().lower() in EXIT_COMMANDS:
                print("已结束；没有保存本次会话。", file=sys.stderr)
                return 0
            guidance_state = input_guidance_state(text, guidance_state)
            result = execute(text, history=history if interactive else None, guidance_state=guidance_state)
            payload: dict[str, Any] = {"status": "completed", "result": result, "error": None}
            if interactive:
                guidance_state = guidance_after_reply(result, guidance_state, text)
                history.extend((
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)},
                ))
        except KeyboardInterrupt:
            return emit(failure(AwarenessError("cancelled")), as_json=args.json)
        except AwarenessError as exc:
            if exc.guidance_state is not None:
                guidance_state = exc.guidance_state
            payload = failure(exc)
        except UnicodeError:
            payload = failure(AwarenessError("input"))
        except (Exception, SystemExit):
            payload = failure(AwarenessError("runtime"))
        if payload["status"] == "failed" and guidance_state != "declined":
            # 不从失败回复推断许可，也不沿用可能已失效的话题许可。
            guidance_state = "off"
        code = emit(payload, as_json=args.json)
        if payload["status"] == "completed" and result["mode"] == "organize" and result["response_type"] == "end":
            return code
        if not interactive or (payload["error"] and payload["error"]["code"] in {"configuration", "budget", "runtime"}):
            return code


if __name__ == "__main__":
    raise SystemExit(main())

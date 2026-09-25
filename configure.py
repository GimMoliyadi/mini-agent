"""Interactive setup for the local, Git-ignored .env file."""

import getpass
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

from config import ENV_FILE


FIELDS = ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY")


def read_existing(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    values = {}
    for line in lines:
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in FIELDS:
            values[key] = value.strip().strip('"').strip("'")
    return lines, values


def save_config(path: Path, lines: list[str], values: dict[str, str]) -> None:
    updated = []
    seen = set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in FIELDS:
            if key not in seen:
                updated.append(f"{key}={values[key]}")
                seen.add(key)
        else:
            updated.append(line)
    updated.extend(f"{key}={values[key]}" for key in FIELDS if key not in seen)

    fd, temporary = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(updated) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure(path: Path = ENV_FILE, input_func=input, secret_func=getpass.getpass) -> int:
    lines, values = read_existing(path)
    if values.get("OPENAI_API_KEY") == "sk-your-key-here":
        values["OPENAI_API_KEY"] = ""
    print(f"配置文件：{path}")
    print("直接回车可保留已有值；API Key 输入时不回显。")

    for key, label in (("OPENAI_BASE_URL", "API 地址"), ("OPENAI_MODEL", "模型名")):
        current = values.get(key, "")
        answer = input_func(f"{label}{f' [{current}]' if current else ''}: ").strip()
        if answer:
            values[key] = answer

    current_key = "（已配置，回车保留）" if values.get("OPENAI_API_KEY") else ""
    answer = secret_func(f"API Key{current_key}: ").strip()
    if answer:
        values["OPENAI_API_KEY"] = answer

    if any(not values.get(key) for key in FIELDS):
        print("配置未保存：API 地址、模型名和 API Key 都不能为空。", file=sys.stderr)
        return 2
    if any(any(char in values[key] for char in "\r\n\0") for key in FIELDS):
        print("配置未保存：不能包含换行或空字符。", file=sys.stderr)
        return 2
    try:
        url = urlsplit(values["OPENAI_BASE_URL"])
        valid_url = (
            url.scheme in {"http", "https"}
            and bool(url.hostname)
            and not (url.username or url.password)
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        print("配置未保存：API 地址需要是有效的 http(s) URL。", file=sys.stderr)
        return 2

    save_config(path, lines, values)
    print("配置已保存。运行 .\\mini.cmd start 开始使用。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(configure())
    except (EOFError, KeyboardInterrupt):
        print("\n已取消，配置未更改。", file=sys.stderr)
        raise SystemExit(130)
    except OSError as exc:
        print(f"配置保存失败：{exc}", file=sys.stderr)
        raise SystemExit(1)

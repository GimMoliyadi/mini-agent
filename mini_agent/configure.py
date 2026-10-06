"""Interactive setup for the local, Git-ignored .env file."""

import getpass
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

FIELDS = ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY")


def read_existing(path: Path) -> tuple[list[str], dict[str, str]]:
    if path.is_symlink():
        raise PermissionError("配置文件不能是符号链接。")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    values = {}
    for line in lines:
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in FIELDS:
            values[key] = value.strip().strip(chr(34)).strip(chr(39))
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

    if path.is_symlink():
        raise PermissionError("配置文件不能是符号链接。")
    path.parent.mkdir(parents=True, exist_ok=True)
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


def collect_values(values: dict[str, str], input_func, secret_func) -> dict[str, str]:
    from file_safety import redact_text

    values = dict(values)
    if values.get("OPENAI_API_KEY") == "sk-your-key-here":
        values["OPENAI_API_KEY"] = ""
    for key, label in (("OPENAI_BASE_URL", "API 地址"), ("OPENAI_MODEL", "模型名")):
        display = redact_text(values.get(key, ""))
        existing_key = values.get("OPENAI_API_KEY")
        if existing_key:
            display = display.replace(existing_key, "[REDACTED]")
        answer = input_func(f"{label}{f' [{display}]' if display else ''}: ").strip()
        if answer:
            values[key] = answer
    current_key = "（已配置，回车保留）" if values.get("OPENAI_API_KEY") else ""
    answer = secret_func(f"API Key{current_key}: ").strip()
    if answer:
        values["OPENAI_API_KEY"] = answer
    return values


def configuration_error(values: dict[str, str]) -> str | None:
    if any(not values.get(key) for key in FIELDS):
        return "API 地址、模型名和 API Key 都不能为空。"
    if any(any(char in values[key] for char in "\r\n\0") for key in FIELDS):
        return "不能包含换行或空字符。"
    try:
        url = urlsplit(values["OPENAI_BASE_URL"])
        valid_url = (
            url.scheme in {"http", "https"}
            and bool(url.hostname)
            and not (url.username or url.password)
            and (url.port is None or 0 < url.port <= 65535)
        )
    except ValueError:
        valid_url = False
    return None if valid_url else "API 地址需要是有效的 http(s) URL。"


def configure(path: Path | None = None, input_func=input, secret_func=getpass.getpass) -> int:
    if path is None:
        from launcher import prepare_environment

        state, _ = prepare_environment()
        path = state / ".env"
    lines, values = read_existing(path)
    print(f"配置文件：{path}")
    print("直接回车可保留已有值；API Key 输入时不回显。")
    values = collect_values(values, input_func, secret_func)
    error = configuration_error(values)
    if error:
        print(f"配置未保存：{error}", file=sys.stderr)
        return 2
    save_config(path, lines, values)
    print("配置已保存。运行 mini start 开始使用。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(configure())
    except (EOFError, KeyboardInterrupt):
        print("\n已取消，配置未更改。", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, UnicodeError, ValueError) as exc:
        from file_safety import redact_text

        print(redact_text(f"配置保存失败：{exc}"), file=sys.stderr)
        raise SystemExit(1)

"""在独立临时副本中运行离线回归；不加载项目配置或调用模型。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.metadata
import os
from pathlib import Path
import py_compile
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECK_TIMEOUT_SECONDS = 300
CLEANUP_TIMEOUT_SECONDS = 5
IGNORED_NAMES = {
    ".git", ".serena", ".venv", "venv", "env", "sessions", "__pycache__",
    ".pytest_cache", ".state", ".mini-agent", ".mini-agent-state", ".dist",
    ".install", "build", "dist", "node_modules", ".workspace_snapshot",
    "changes", ".changes", ".mini_agent", "journals",
    "runs", "release-runs",
}
SYSTEM_ENV_NAMES = {
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE",
    "OS", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL",
}


def is_link(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def ignored_entries(directory: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        path = Path(directory) / name
        normalized = name.casefold()
        secret = normalized == ".env" or (normalized.startswith(".env.") and normalized != ".env.example")
        if normalized in IGNORED_NAMES or secret or normalized.endswith(".egg-info") or is_link(path):
            ignored.add(name)
    return ignored


def isolated_environment(root: Path) -> dict[str, str]:
    environment = {key: os.environ[key] for key in os.environ if key.upper() in SYSTEM_ENV_NAMES}
    directories = {name: root / name for name in (".tmp", ".home", ".state", ".smoke")}
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=True)
    environment.update({
        "OPENAI_API_KEY": "offline-test-key",
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        "OPENAI_MODEL": "offline-test-model",
        "NO_PROXY": "*", "no_proxy": "*",
        "MINI_AGENT_HTTP_PROXY": "",
        "AGENT_WORKSPACE": str(root / "demo_workspace"),
        "MINI_AGENT_STATE_DIR": str(directories[".state"]),
        "TMP": str(directories[".tmp"]), "TEMP": str(directories[".tmp"]),
        "TMPDIR": str(directories[".tmp"]),
        "HOME": str(directories[".home"]), "USERPROFILE": str(directories[".home"]),
        "APPDATA": str(directories[".home"] / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(directories[".home"] / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(directories[".home"] / ".config"),
        "GIT_CEILING_DIRECTORIES": str(root), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
        "PYTHONPATH": os.pathsep.join((str(root), str(root / "eval"))),
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONNOUSERSITE": "1",
        "PIP_NO_INDEX": "1", "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "TOOL_APPROVAL_MODE": "DENY",
        "CONTEXT_MODE": "WRITE_ONLY",
    })
    return environment


def create_venv_link(root: Path, environment: dict[str, str]) -> Path | None:
    if sys.prefix == sys.base_prefix:
        return None
    link = root / ".venv"
    if os.name == "nt":
        subprocess.run(
            [environment.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J", str(link), sys.prefix],
            cwd=root, env=environment, stdin=subprocess.DEVNULL, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=CLEANUP_TIMEOUT_SECONDS,
        )
    else:
        link.symlink_to(Path(sys.prefix), target_is_directory=True)
    return link


def remove_venv_link(link: Path) -> None:
    if not is_link(link):
        raise RuntimeError(f"拒绝递归清理：临时虚拟环境路径已不再是链接：{link}")
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


@contextmanager
def isolated_project(source: Path):
    temporary_root = Path(tempfile.mkdtemp(prefix="mini-agent-check-"))
    root = temporary_root / "project"
    link = None
    try:
        shutil.copytree(source, root, ignore=ignored_entries)
        environment = isolated_environment(root)
        link = create_venv_link(root, environment)
        yield root, environment
    finally:
        # 先只删除链接，再清理副本；绝不递归删除共享虚拟环境目标。
        if link is not None and (link.exists() or link.is_symlink()):
            remove_venv_link(link)
        shutil.rmtree(temporary_root)


def stop_check(process: subprocess.Popen, root: Path, environment: dict[str, str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            cwd=root, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=CLEANUP_TIMEOUT_SECONDS, check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)


def run_step(name: str, arguments: list[str], root: Path, environment: dict[str, str]) -> int:
    print(f"\n[检查] {name}", flush=True)
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    try:
        process = subprocess.Popen(arguments, cwd=root, env=environment, stdin=subprocess.DEVNULL, **options)
    except OSError as error:
        print(f"[失败] 无法启动 {name}：{error}", flush=True)
        return 1
    try:
        code = process.wait(timeout=CHECK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        stop_check(process, root, environment)
        print(f"[失败] {name} 超过 {CHECK_TIMEOUT_SECONDS} 秒", flush=True)
        return 124
    except KeyboardInterrupt:
        stop_check(process, root, environment)
        raise
    print(f"[{'通过' if code == 0 else '失败'}] {name}（退出码 {code}）", flush=True)
    return code


def compile_project(root: Path) -> int:
    failures = []
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [name for name in names if name.casefold() not in IGNORED_NAMES and not is_link(Path(directory) / name)]
        for name in files:
            path = Path(directory) / name
            if path.suffix != ".py" or is_link(path):
                continue
            try:
                py_compile.compile(str(path), doraise=True)
            except py_compile.PyCompileError as error:
                failures.append(str(error))
    for failure in failures:
        print(f"[语法错误] {failure}", flush=True)
    return 1 if failures else 0


def check_metadata(root: Path) -> int:
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    modules = metadata["tool"]["setuptools"]["py-modules"]
    source_modules = {path.stem for path in root.glob("*.py")}
    errors = []
    if set(modules) != source_modules or len(modules) != len(set(modules)):
        errors.append(f"py-modules 不一致：缺少 {sorted(source_modules - set(modules))}；无源码 {sorted(set(modules) - source_modules)}")
    if metadata["project"]["scripts"].get("mini-agent") != "mini_agent.launcher:main":
        errors.append("CLI 入口必须为 mini-agent=mini_agent.launcher:main")
    packages = metadata["tool"]["setuptools"]["packages"]
    for package in packages:
        if not (root / package.replace(".", "/") / "__init__.py").is_file():
            errors.append(f"包缺少 __init__.py：{package}")
    lock_lines = {
        line.strip() for line in (root / "requirements.lock").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    for requirement in metadata["project"]["dependencies"]:
        if requirement not in lock_lines:
            errors.append(f"包直接依赖未按同版本锁定：{requirement}")
    for line in sorted(lock_lines):
        name, version = line.split("==", 1)
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"锁定依赖未安装：{name}")
            continue
        if installed != version:
            errors.append(f"锁定依赖版本不同：{name}，需要 {version}，实际 {installed}")
    for error in errors:
        print(f"[打包错误] {error}", flush=True)
    return 1 if errors else 0


def smoke_installed_entry(root: Path, environment: dict[str, str]) -> int:
    wheels = list((root / ".dist").glob("*.whl"))
    if len(wheels) != 1:
        print("[打包错误] 没有唯一可用 wheel，无法验证离线安装和 CLI 入口", flush=True)
        return 1
    target = root / ".install"
    result = run_step("离线安装 wheel", [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", "--target", str(target), str(wheels[0])], root, environment)
    if result:
        return result
    environment = {**environment, "PYTHONPATH": str(target)}
    entry = target / "bin" / ("mini-agent.exe" if os.name == "nt" else "mini-agent")
    checks = (("安装入口 --help", ["--help"]), ("安装版本查询", ["version"]), ("安装会话列表", ["sessions", "list"]))
    results = [run_step(name + "（不调用模型）", [str(entry), *arguments], root / ".smoke", environment) for name, arguments in checks]
    return 0 if all(code == 0 for code in results) else 1


def regression_steps(root: Path) -> list[tuple[str, list[str]]]:
    python = sys.executable
    return [
        ("unittest tests", [python, "-m", "unittest", "discover", "-s", "tests", "-v"]),
        ("脚本 test_loop", [python, "tests/test_loop.py"]),
        ("脚本 test_sandbox", [python, "tests/test_sandbox.py"]),
        ("eval metrics", [python, "eval/test_metrics.py"]),
        ("eval 五项可靠性测试", [python, "-m", "unittest", "discover", "-s", "eval", "-p", "test_reliability.py", "-v"]),
        ("全部 Python 语法", [python, "scripts/check.py", "--internal", "compile"]),
    ]


def packaging_steps(root: Path) -> list[tuple[str, list[str]]]:
    python = sys.executable
    return [
        ("打包模块与依赖版本", [python, "scripts/check.py", "--internal", "metadata"]),
        ("离线依赖闭包", [python, "-m", "pip", "install", "--dry-run", "--no-index", "-r", "requirements.lock"]),
        ("sdist 构建", [python, "-c", "from setuptools.build_meta import build_sdist; build_sdist('.dist')"]),
        ("wheel 构建", [python, "-c", "from setuptools.build_meta import build_wheel; build_wheel('.dist')"]),
        ("全部 Python 语法", [python, "scripts/check.py", "--internal", "compile"]),
        ("安装与 CLI smoke", [python, "scripts/check.py", "--internal", "install"]),
    ]


def run_internal(action: str, root: Path) -> int:
    if not (root / ".state").is_dir() or os.environ.get("MINI_AGENT_STATE_DIR") != str(root / ".state"):
        raise ValueError("内部检查只允许在隔离副本中运行")
    if action == "compile":
        return compile_project(root)
    if action == "metadata":
        return check_metadata(root)
    return smoke_installed_entry(root, dict(os.environ))


def main() -> int:
    parser = argparse.ArgumentParser(description="离线回归；默认仅操作临时项目副本，不调用真实模型")
    parser.add_argument("--packaging-only", action="store_true", help="只校验打包、离线安装和 CLI，不运行行为回归")
    parser.add_argument("--internal", choices=("compile", "metadata", "install"), help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    try:
        if arguments.internal:
            return run_internal(arguments.internal, PROJECT_ROOT)
        with isolated_project(PROJECT_ROOT) as (root, environment):
            steps = packaging_steps(root) if arguments.packaging_only else regression_steps(root)
            print(f"离线检查副本：{root}", flush=True)
            results = [(name, run_step(name, command, root, environment)) for name, command in steps]
            failures = [name for name, code in results if code != 0]
            print(f"\n检查汇总：{len(results) - len(failures)}/{len(results)} 通过", flush=True)
            if failures:
                print("失败项：" + "、".join(failures), flush=True)
            return 1 if failures else 0
    except KeyboardInterrupt:
        print("\n检查已取消", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"[检查失败] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

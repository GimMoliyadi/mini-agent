"""受信项目命令的有界采集与自有进程树清理；不是操作系统沙盒。"""

import ctypes
import importlib
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
_WINDOWS_CREATE_SUSPENDED = 0x00000004
_WINDOWS_KILL_ON_JOB_CLOSE = 0x00002000
_WINDOWS_EXTENDED_LIMIT_INFORMATION = 9
OUTPUT_READ_CHUNK_BYTES = 4096
PROCESS_CHECK_INTERVAL_SECONDS = 0.02
PROCESS_CLEANUP_TIMEOUT_SECONDS = 5
_ENVIRONMENT_KEYS = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "TZ", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"})


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_limit_exceeded: bool = False
    captured_bytes: int = 0


def command_environment(workspace: Path) -> dict[str, str]:
    workspace = workspace.resolve()
    environment = {key: value for key, value in os.environ.items() if key.upper() in _ENVIRONMENT_KEYS}
    environment.update({
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
        # A same-size edit within one timestamp tick must not reuse stale .pyc.
        # A fresh, unwritten prefix bypasses both project and installed caches.
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(workspace / "__pycache__" / uuid.uuid4().hex),
        "TMP": str(workspace), "TEMP": str(workspace), "HOME": str(workspace), "USERPROFILE": str(workspace),
        "AGENT_WORKSPACE": str(workspace), "MINI_AGENT_STATE_DIR": str(workspace),
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CEILING_DIRECTORIES": str(workspace.parent),
        "GIT_PAGER": "", "PAGER": "",
    })
    return environment


def _current_budget():
    try:
        module = importlib.import_module("runtime_guards")
    except ModuleNotFoundError as exc:
        if exc.name != "runtime_guards":
            raise
        return None
    return module.get_current_budget()


class _OutputCapture:
    def __init__(self, limit: int):
        self.limit = limit
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.lock = threading.Lock()
        self.limit_reached = threading.Event()
        self.finished = [threading.Event(), threading.Event()]
        self.errors: list[OSError | ValueError] = []

    def read_stream(self, stream, output: bytearray, index: int) -> None:
        try:
            while True:
                chunk = stream.read(OUTPUT_READ_CHUNK_BYTES)
                if not chunk:
                    break
                with self.lock:
                    remaining = self.limit - len(self.stdout) - len(self.stderr)
                    output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        self.limit_reached.set()
                        break
        except (OSError, ValueError) as exc:
            self.errors.append(exc)
        finally:
            self.finished[index].set()

    def start(self, process: subprocess.Popen, threads: list[threading.Thread]) -> None:
        for index, (stream, output) in enumerate(((process.stdout, self.stdout), (process.stderr, self.stderr))):
            thread = threading.Thread(target=self.read_stream, args=(stream, output, index), daemon=True)
            thread.start()
            threads.append(thread)


class _WindowsJob:
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Windows Job Objects 仅支持 Windows")
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t), ("maximum_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD), ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read_operations", "write_operations", "other_operations", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = _WINDOWS_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, _WINDOWS_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach_and_resume(self, process: subprocess.Popen) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows Job Objects 仅支持 Windows")
        handle = ctypes.c_void_p(int(getattr(process, "_handle")))
        if not self.kernel.AssignProcessToJobObject(self.handle, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        resume = ctypes.WinDLL("ntdll").NtResumeProcess
        resume.argtypes = [ctypes.c_void_p]
        resume.restype = ctypes.c_long
        status = resume(handle)
        if status != 0:
            raise OSError(f"无法恢复受控子进程：NTSTATUS {status}")

    def close(self) -> None:
        if sys.platform != "win32":
            return
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _close_process_handles(process: subprocess.Popen) -> None:
    assert process.stdout is not None and process.stderr is not None
    try:
        process.stdout.close()
    finally:
        try:
            process.stderr.close()
        finally:
            if os.name == "nt":
                getattr(process, "_handle").Close()


def _start_process(argv: list[str], cwd: Path, environment: dict) -> tuple[subprocess.Popen, _WindowsJob | None]:
    job = _WindowsJob() if sys.platform == "win32" else None
    options = {"creationflags": _WINDOWS_CREATE_SUSPENDED} if job else {"start_new_session": True}
    process = None
    try:
        process = subprocess.Popen(argv, cwd=str(cwd), env=environment, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, **options)
        if job:
            job.attach_and_resume(process)
        return process, job
    except BaseException:
        if job:
            job.close()
        if process is not None:
            process.kill()
            process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
            _close_process_handles(process)
        raise


def _stop_process_tree(process: subprocess.Popen, job: _WindowsJob | None) -> None:
    if job:
        job.close()
    else:
        try:
            getattr(os, "killpg")(process.pid, getattr(signal, "SIGKILL"))
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)


def _wait_for_result(process: subprocess.Popen, capture: _OutputCapture, deadline: float, budget) -> tuple[bool, bool]:
    while True:
        if budget is not None:
            budget.check()
        if capture.limit_reached.is_set():
            return False, True
        if time.monotonic() >= deadline:
            return True, False
        if process.poll() is not None and all(event.is_set() for event in capture.finished):
            if capture.errors:
                raise OSError("命令输出采集失败") from capture.errors[0]
            return False, False
        capture.limit_reached.wait(PROCESS_CHECK_INTERVAL_SECONDS)


def run_process(argv: list[str], cwd: Path, *, workspace: Path, timeout_seconds: float, output_limit_bytes: int = MAX_COMMAND_OUTPUT_BYTES) -> ProcessResult:
    workspace_root = Path(workspace).resolve()
    execution_cwd = Path(cwd).resolve()
    if not execution_cwd.is_relative_to(workspace_root):
        raise PermissionError(f"cwd 越出 workspace，拒绝执行：{cwd}")
    if not execution_cwd.is_dir():
        raise NotADirectoryError(f"cwd 不是目录：{cwd}")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("命令超时必须是有限的正数")
    if isinstance(output_limit_bytes, bool) or not isinstance(output_limit_bytes, int) or output_limit_bytes < 1:
        raise ValueError("输出字节上限必须是正整数")
    budget = _current_budget()
    if budget is not None:
        budget.check()
    deadline = time.monotonic() + timeout_seconds
    process, job = _start_process(argv, execution_cwd, command_environment(workspace_root))
    capture = _OutputCapture(output_limit_bytes)
    threads: list[threading.Thread] = []
    try:
        capture.start(process, threads)
        timed_out, output_limit_exceeded = _wait_for_result(process, capture, deadline, budget)
        returncode = process.returncode if not timed_out and not output_limit_exceeded else None
    finally:
        try:
            _stop_process_tree(process, job)
            for thread in threads:
                thread.join(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
            if any(thread.is_alive() for thread in threads):
                raise RuntimeError("命令输出采集线程未能退出")
        finally:
            _close_process_handles(process)
    return ProcessResult(returncode, bytes(capture.stdout), bytes(capture.stderr), timed_out, output_limit_exceeded, len(capture.stdout) + len(capture.stderr))

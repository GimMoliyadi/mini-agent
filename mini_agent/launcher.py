"""Thin Windows/package launcher; path selection precedes application assembly."""

import ctypes
import os
from pathlib import Path
import sys
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def get_desktop_path() -> Path:
    if sys.platform != "win32":
        return Path.home() / "Desktop"
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    folder_id = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID("b4bfcc3a-db2c-424c-b029-7fe99a87c641").bytes_le)
    folder_path = ctypes.c_wchar_p()
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    result = shell.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(folder_path))
    try:
        if result < 0 or not folder_path.value:
            raise OSError("无法读取桌面位置；请使用 --workspace")
        return Path(folder_path.value)
    finally:
        if folder_path:
            ole.CoTaskMemFree(ctypes.cast(folder_path, ctypes.c_void_p))


def prepare_environment(*, workspace=None, desktop=False, create=True) -> tuple[Path, Path]:
    if desktop and workspace is not None:
        raise ValueError("--desktop 和 --workspace 不能同时使用")
    source = (PROJECT_ROOT / "demo_workspace").is_dir()
    default_state = PROJECT_ROOT if source else Path.home() / ".mini-agent"
    state = Path(os.environ.get("MINI_AGENT_STATE_DIR", default_state)).expanduser().resolve()
    default_workspace = PROJECT_ROOT / "demo_workspace" if source else state / "workspace"
    selected = get_desktop_path() if desktop else workspace or os.environ.get("AGENT_WORKSPACE", default_workspace)
    work = Path(selected).expanduser().resolve()
    if create:
        state.mkdir(parents=True, exist_ok=True)
        if work == default_workspace:
            work.mkdir(parents=True, exist_ok=True)
    if not work.is_dir():
        raise NotADirectoryError(f"工作目录不存在：{work}")
    return state, work


def main(argv=None) -> int:
    from .cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

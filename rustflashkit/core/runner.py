"""adb / fastboot 可执行文件定位与子进程执行封装。

所有外部命令一律使用参数列表（不经 shell），杜绝注入；
Windows 下隐藏子控制台窗口。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

LogFn = Callable[[str], None]


def app_root() -> Path:
    """应用根目录（打包为 exe 时指向 exe 所在目录）。"""
    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


class ToolLocator:
    """按优先级查找 adb / fastboot：显式路径 → 应用 tools/ 目录 → PATH。"""

    def __init__(self, adb_path: str = "", fastboot_path: str = ""):
        self.adb_path = adb_path or ""
        self.fastboot_path = fastboot_path or ""

    def _candidates(self, name: str, explicit: str) -> List[Path]:
        found: List[Path] = []
        if explicit:
            p = Path(explicit).expanduser()
            found.append(p if p.is_absolute() else app_root() / p)
        exe = name + (".exe" if IS_WINDOWS else "")
        found.append(app_root() / "tools" / exe)
        found.append(Path(__file__).resolve().parent / "tools" / exe)
        on_path = shutil.which(name)
        if on_path:
            found.append(Path(on_path))
        return found

    def resolve(self, name: str) -> Optional[Path]:
        for cand in self._candidates(name, self.adb_path if name == "adb" else self.fastboot_path):
            if cand and cand.is_file():
                return cand.resolve()
        return None


@dataclass
class CmdResult:
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def output(self) -> str:
        return (self.stdout or "") + (("\n[stderr]\n" + self.stderr) if self.stderr.strip() else "")


class ToolRunner:
    """统一的 adb/fastboot 命令执行器。"""

    def __init__(self, locator: ToolLocator, log: LogFn = lambda s: None):
        self.locator = locator
        self.log = log

    def tool_path(self, name: str) -> Optional[Path]:
        return self.locator.resolve(name)

    def run(self, tool: str, args: Sequence[str], timeout: float = 60) -> CmdResult:
        exe = self.tool_path(tool)
        if exe is None:
            return CmdResult([tool, *args], -1, "", f"未找到 {tool} 可执行文件，请将其放入 tools/ 目录或安装 platform-tools", False)
        argv = [str(exe), *args]
        self.log("$ " + " ".join(_q(a) for a in argv))
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if out:
                self.log(out)
            return CmdResult(argv, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            return CmdResult(argv, -1, "", f"命令超时（>{timeout:.0f}s）", True)
        except OSError as exc:  # 文件损坏、无法执行等
            return CmdResult(argv, -1, "", f"无法执行 {exe}: {exc}")


def _q(s: str) -> str:
    """日志展示用的引号包裹。"""
    return f'"{s}"' if (" " in s or "\t" in s) else s

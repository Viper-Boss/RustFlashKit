"""固件导出（备份）：从 adb 设备读取分区镜像到本地。

导出是只读操作，安全（备份 EFS/IMEI 分区反而是推荐做法）。
需要设备处于系统模式（adb 可见）且 adbd 能读块设备——这类棒子的
原厂/自建系统通常以 root 跑 adbd。Bootloader (fastboot) 模式没有
标准的分区读取命令，因此不支持导出。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from rustflashkit.core.modules import sha256_of
from rustflashkit.core.runner import ToolRunner

BY_NAME = "/dev/block/bootdevice/by-name"

# 逐行输出: "boot /dev/block/mmcblk0p7 8388608"
TABLE_LINE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]{1,64})\s+(?P<path>\S+)\s+(?P<size>\d+)$")

# 在设备上枚举分区名 + 真实大小（toybox/busybox 均支持 readlink -f 与 stat -c）
READ_TABLE_CMD = (
    f"cd {BY_NAME} 2>/dev/null || exit 9; "
    "for f in *; do "
    'echo "$f $(readlink -f "$f") $(stat -c %s "$f" 2>/dev/null || echo 0)"; '
    "done"
)


def _fmt_mb(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}MB"
    return f"{size / 1024:.0f}KB"


@dataclass
class PartitionInfo:
    name: str
    path: str
    size: int

    @property
    def size_text(self) -> str:
        return _fmt_mb(self.size)


def export_timeout(size: int) -> int:
    """单分区导出超时：至少 120s，按 ~1MB/s 预留（8s/MB）。"""
    return max(120, int(size / (1024 * 1024)) * 8)


def read_partition_table(runner: ToolRunner, serial: str) -> List[PartitionInfo]:
    """通过 adb 枚举设备分区（名称 + 块设备路径 + 字节数）。"""
    res = runner.run("adb", ["-s", serial, "shell", READ_TABLE_CMD], timeout=30)
    if not res.ok:
        raise RuntimeError((res.stderr or res.stdout or "读取分区表失败").strip()[:300]
                           + "（需要设备处于系统模式且 adb 可用）")
    parts: List[PartitionInfo] = []
    for line in (res.stdout or "").splitlines():
        m = TABLE_LINE_RE.match(line.strip())
        if m:
            parts.append(PartitionInfo(m["name"], m["path"], int(m["size"])))
    if not parts:
        raise RuntimeError("未解析到分区：设备可能不是标准 MSM 分区布局"
                           f"（缺少 {BY_NAME}）")
    return parts


def export_partition(runner: ToolRunner, serial: str, part: PartitionInfo,
                     dest: Path) -> None:
    """adb pull 单个分区到 dest。失败抛 RuntimeError。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    res = runner.run("adb", ["-s", serial, "pull", f"{BY_NAME}/{part.name}", str(dest)],
                     timeout=export_timeout(part.size))
    if not res.ok:
        msg = (res.stderr or res.stdout or "导出失败").strip()[:300]
        if "permission denied" in msg.lower():
            msg += "（adbd 无权限读取块设备，需要 root adbd）"
        raise RuntimeError(msg)
    if not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError(f"导出文件为空: {dest}")


@dataclass
class ExportItemResult:
    part: PartitionInfo
    ok: bool
    detail: str = ""
    sha256: str = ""
    file: Optional[Path] = None


class ExportJob:
    """后台线程批量导出分区，可取消；回调在工作线程中调用。"""

    def __init__(self, runner: ToolRunner, serial: str, parts: List[PartitionInfo],
                 out_dir: Path, with_sha256: bool,
                 on_log: Callable[[str], None],
                 on_item: Callable[[ExportItemResult], None],
                 on_done: Callable[[bool, str], None]):
        self.runner = runner
        self.serial = serial
        self.parts = list(parts)
        self.out_dir = out_dir
        self.with_sha256 = with_sha256
        self.on_log = on_log
        self.on_item = on_item
        self.on_done = on_done
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="exporter", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        self.on_log("⏹ 用户请求取消（当前分区完成后停止）…")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------
    def _run(self) -> None:
        self.on_log(f"▶ 开始导出 {len(self.parts)} 个分区 → {self.out_dir}")
        ok_all = True
        message = "导出完成"
        for i, part in enumerate(self.parts, 1):
            if self._cancel.is_set():
                ok_all = False
                message = "已取消"
                self.on_log(f"⏹ 第 {i} 项前取消")
                break
            dest = self.out_dir / f"{part.name}.img"
            self.on_log(f"[{i}/{len(self.parts)}] 导出 {part.name}（{part.size_text}）…")
            t0 = time.time()
            try:
                export_partition(self.runner, self.serial, part, dest)
                sha = sha256_of(dest) if self.with_sha256 else ""
                self.on_log(f"✓ {part.name} 完成，用时 {time.time() - t0:.0f}s")
                self.on_item(ExportItemResult(part, True, "", sha, dest))
            except RuntimeError as exc:
                self.on_log(f"✗ {part.name} 失败: {exc}")
                self.on_item(ExportItemResult(part, False, str(exc)))
                ok_all = False
                message = f"{part.name} 导出失败: {exc}"
                self.on_log("已停止后续分区")
                break
        self.on_done(ok_all, message)

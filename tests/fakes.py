"""可执行假 adb / fastboot：把收到的参数记录到日志文件供断言。

假 adb 额外模拟：shell（返回 canned 分区表）、pull（写 8KB 文件；
modemst1 模拟 permission denied）。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

FASTBOOT_SH = """#!/bin/sh
LOG="$FAKE_LOG"
echo "fastboot $*" >> "$LOG"
cmd="$1"; shift
case "$cmd" in
  devices) echo "TESTSERIAL\\tfastboot" ;;
  flash)
    case "$2" in
      *bad.img) echo "FAILED (remote: 'Image is not a boot image')" >&2; exit 1 ;;
    esac
    echo "OKAY" ;;
  reboot) echo "OKAY" ;;
  *) exit 0 ;;
esac
"""

ADB_SH = """#!/bin/sh
LOG="$FAKE_LOG"
echo "adb $*" >> "$LOG"
if [ "$1" = "-s" ]; then shift 2; fi
case "$1" in
  devices) echo "List of devices attached"; echo "TESTSERIAL\\tdevice" ;;
  shell)
    echo "boot /dev/block/mmcblk0p7 8388608"
    echo "rootfs /dev/block/mmcblk0p18 4831838208"
    echo "modemst1 /dev/block/mmcblk0p2 2048"
    ;;
  pull)
    src="$2"; dst="$3"
    case "$(basename "$src")" in
      modemst1) echo "adb: failed to copy '$src': Permission denied" >&2; exit 1 ;;
    esac
    dd if=/dev/zero of="$dst" bs=1k count=8 2>/dev/null
    echo "[100%] $dst"
    ;;
  *) exit 0 ;;
esac
"""


def make_fake_tools(directory: Path) -> tuple["ToolLocator", Path]:
    """在 directory 下生成假 adb/fastboot，返回 (ToolLocator, 调用日志路径)。"""
    log = directory / "calls.log"
    log.write_text("")
    os.environ["FAKE_LOG"] = str(log)
    adb = directory / "adb"
    fb = directory / "fastboot"
    adb.write_text(ADB_SH)
    fb.write_text(FASTBOOT_SH)
    for p in (adb, fb):
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    from rustflashkit.core.runner import ToolLocator
    return ToolLocator(adb_path=str(adb), fastboot_path=str(fb)), log


def calls(log: Path) -> list[list[str]]:
    return [ln.split() for ln in log.read_text().splitlines() if ln.strip()]

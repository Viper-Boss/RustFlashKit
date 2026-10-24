"""可执行假 adb / fastboot：把收到的参数记录到日志文件供断言。"""
from __future__ import annotations

import os
import stat
from pathlib import Path

FASTBOOT_SH = """#!/bin/sh
LOG="$FAKE_LOG"
echo "fastboot $*" >> "$LOG"
cmd="$1"; shift
case "$cmd" in
  devices) echo "TESTSERIAL\tfastboot" ;;
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
case "$1" in
  devices) echo "List of devices attached"; echo "TESTSERIAL\tdevice" ;;
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

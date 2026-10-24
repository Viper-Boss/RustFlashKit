"""设备状态探测：adb / fastboot 设备轮询与模式切换。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from rustflashkit.core.runner import ToolRunner

ADB_STATE_NAMES = {
    "device": "系统模式 (adb)",
    "unauthorized": "未授权（请在设备上允许 USB 调试）",
    "recovery": "Recovery 模式",
    "sideload": "Sideload 模式",
    "offline": "离线",
}


@dataclass
class DeviceInfo:
    mode: str            # "none" | "system" | "bootloader"
    serial: str = ""
    detail: str = ""
    adb_found: bool = False
    fastboot_found: bool = False

    @property
    def label(self) -> str:
        if self.mode == "bootloader":
            return f"Bootloader 模式 · {self.serial}"
        if self.mode == "system":
            return f"{self.detail} · {self.serial}"
        if not self.adb_found and not self.fastboot_found:
            return "未找到 adb / fastboot 工具"
        return "未检测到设备"


class DeviceService:
    """聚合 adb/fastboot 设备查询。供 GUI 轮询调用。"""

    def __init__(self, runner: ToolRunner):
        self.runner = runner

    def _fastboot_devices(self) -> List[str]:
        res = self.runner.run("fastboot", ["devices"], timeout=15)
        out = res.stdout or ""
        return [ln.split("\t")[0] for ln in out.splitlines()
                if re.match(r"^[A-Za-z0-9_.-]+\s+fastboot", ln)]

    def _adb_devices(self) -> List[tuple]:
        res = self.runner.run("adb", ["devices"], timeout=15)
        found = []
        for ln in (res.stdout or "").splitlines():
            m = re.match(r"^([A-Za-z0-9_.:-]+)\t(\S+)", ln.strip())
            if m and m.group(2) != "offline*":
                found.append((m.group(1), m.group(2)))
        return found

    def probe(self) -> DeviceInfo:
        fb = self._fastboot_devices()
        if fb:
            return DeviceInfo("bootloader", serial=fb[0],
                              adb_found=self.runner.tool_path("adb") is not None,
                              fastboot_found=True)
        adbs = self._adb_devices()
        if adbs:
            serial, state = adbs[0]
            return DeviceInfo("system", serial=serial,
                              detail=ADB_STATE_NAMES.get(state, state),
                              adb_found=True,
                              fastboot_found=self.runner.tool_path("fastboot") is not None)
        return DeviceInfo("none",
                          adb_found=self.runner.tool_path("adb") is not None,
                          fastboot_found=self.runner.tool_path("fastboot") is not None)

    # ---- 动作 ---------------------------------------------------------
    def reboot_to_bootloader(self) -> bool:
        """从系统模式重启进 Bootloader。"""
        info = self.probe()
        if info.mode == "bootloader":
            return True
        if info.mode != "system":
            return False
        return self.runner.run("adb", ["-s", info.serial, "reboot", "bootloader"], timeout=30).ok

    def reboot_device(self) -> bool:
        """从 Bootloader 重启设备。"""
        return self.runner.run("fastboot", ["reboot"], timeout=30).ok

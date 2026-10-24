"""设备状态探测：adb / fastboot 设备枚举（支持多设备）与模式切换。"""
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
            return f"Bootloader · {self.serial}"
        if self.mode == "system":
            return f"{self.detail} · {self.serial}"
        return "未检测到设备"


class DeviceService:
    """聚合 adb/fastboot 设备查询。probe_all 返回全部设备（fastboot 在前）。"""

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

    def probe_all(self) -> List[DeviceInfo]:
        tools_ok = {"adb": self.runner.tool_path("adb") is not None,
                    "fastboot": self.runner.tool_path("fastboot") is not None}
        devices: List[DeviceInfo] = []
        for s in self._fastboot_devices():
            devices.append(DeviceInfo("bootloader", serial=s,
                                      fastboot_found=tools_ok["fastboot"],
                                      adb_found=tools_ok["adb"]))
        for serial, state in self._adb_devices():
            devices.append(DeviceInfo("system", serial=serial,
                                      detail=ADB_STATE_NAMES.get(state, state),
                                      adb_found=tools_ok["adb"],
                                      fastboot_found=tools_ok["fastboot"]))
        return devices

    def probe(self) -> DeviceInfo:
        """第一台设备（fastboot 优先）；无设备时返回 mode="none"。"""
        devs = self.probe_all()
        if devs:
            return devs[0]
        return DeviceInfo("none",
                          adb_found=self.runner.tool_path("adb") is not None,
                          fastboot_found=self.runner.tool_path("fastboot") is not None)

    # ---- 动作 ---------------------------------------------------------
    def reboot_to_bootloader(self, serial: str = "") -> bool:
        """从系统模式重启进 Bootloader；serial 为空时自动探测。"""
        if not serial:
            info = self.probe()
            if info.mode == "bootloader":
                return True
            if info.mode != "system":
                return False
            serial = info.serial
        return self.runner.run("adb", ["-s", serial, "reboot", "bootloader"],
                               timeout=30).ok

    def reboot_device(self, serial: str = "") -> bool:
        """从 Bootloader 重启设备。"""
        args = (["-s", serial] if serial else []) + ["reboot"]
        return self.runner.run("fastboot", args, timeout=30).ok

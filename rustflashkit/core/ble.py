"""BLE 蓝牙配置（实验性）。

背景：RustSimHub 的 BLE 应急配网（断网蓝牙配置）在固件端尚未实现——
docs/ble/README.md（2026-10-24 任务书）仅完成需求拆分：GATT UUID、
分帧、ECDH/HKDF/AEAD 加密会话、恢复公钥等均"待实施"。

因此本模块只提供两件确定有用的事：

1. BLE 诊断：扫描附近设备、连接并枚举 GATT 服务/特征
   （用于验证棒子的广播/HCI/GATT 能力，即任务书 B0 的证据项）；
2. 模板化配网发送骨架：服务/特征 UUID 与载荷字段从
   config/ble-provisioning-template.json 读取，固件协议定稿后
   修改模板即可对接，无需改代码。

bleak 为可选依赖（pip install bleak；Windows 使用内置 WinRT 后端）。
"""
from __future__ import annotations

import json
import re
import uuid as uuid_mod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

TEMPLATE_SCHEMA = 1

DEFAULT_TEMPLATE: dict = {
    "schema": TEMPLATE_SCHEMA,
    "name": "默认 BLE 配网模板（协议占位）",
    "description": "RustSimHub BLE 协议定稿前，本模板仅用于发送骨架联调；"
                   "UUID 为占位符，发送前必须替换为固件实际广播的 UUID。",
    "name_filter": "RustSimHub",
    "service_uuid": "00000000-0000-0000-0000-000000000000",
    "write_char": "00000000-0000-0000-0000-000000000000",
    "notify_char": "",
    "payload": {"v": 1, "type": "wifi.recovery"},
    "fields": ["ssid", "psk"],
}


class BleUnavailable(Exception):
    """bleak 未安装或平台蓝牙不可用。"""


def get_bleak():
    try:
        from bleak import BleakClient, BleakScanner  # noqa: F401
        return BleakScanner, BleakClient
    except ImportError as exc:
        raise BleUnavailable(
            "未安装 bleak：请在命令行执行  pip install bleak  后重试"
            "（Windows 需要本机蓝牙适配器）") from exc


# ======================================================================
# 模板
# ======================================================================
@dataclass
class BleTemplate:
    name: str = "默认模板"
    description: str = ""
    name_filter: str = ""
    service_uuid: str = ""
    write_char: str = ""
    notify_char: str = ""
    payload: dict = field(default_factory=dict)
    fields: List[str] = field(default_factory=list)

    @staticmethod
    def load(path: Path) -> "BleTemplate":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if int(data.get("schema", 0)) > TEMPLATE_SCHEMA:
            raise ValueError(f"模板 schema 高于本工具支持的 {TEMPLATE_SCHEMA}")
        known = {f for f in BleTemplate.__dataclass_fields__}
        return BleTemplate(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"schema": TEMPLATE_SCHEMA,
               "name": self.name, "description": self.description,
               "name_filter": self.name_filter,
               "service_uuid": self.service_uuid,
               "write_char": self.write_char, "notify_char": self.notify_char,
               "payload": self.payload, "fields": self.fields}
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    def validate(self) -> List[str]:
        errors = []
        for field_name in ("service_uuid", "write_char"):
            v = getattr(self, field_name)
            if not UUID_RE.match(v or ""):
                errors.append(f"{field_name} 不是合法 UUID：{v or '（空）'}")
        if self.notify_char and not UUID_RE.match(self.notify_char):
            errors.append(f"notify_char 不是合法 UUID：{self.notify_char}")
        if not self.fields:
            errors.append("fields 为空（至少需要 ssid）")
        return errors

    @property
    def is_placeholder(self) -> bool:
        """UUID 是否还是全零占位符。"""
        return self.service_uuid.startswith("00000000-0000-0000-0000-000000000000") or \
            self.write_char.startswith("00000000-0000-0000-0000-000000000000")


def build_payload(template: BleTemplate, values: dict) -> bytes:
    """按模板生成配网载荷：{**payload, **fields 取值}，换行结尾。"""
    doc = dict(template.payload or {})
    for key in template.fields:
        if key not in values or values[key] is None:
            raise ValueError(f"缺少字段: {key}")
        doc[key] = values[key]
    return (json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def ensure_template(path: Path) -> BleTemplate:
    """模板存在则加载；否则写出默认模板并返回。"""
    if Path(path).is_file():
        return BleTemplate.load(path)
    t = BleTemplate(**{k: v for k, v in DEFAULT_TEMPLATE.items() if k in BleTemplate.__dataclass_fields__})
    t.save(path)
    return t


# ======================================================================
# BLE 操作（async；GUI 侧用线程 + asyncio.run 包装）
# ======================================================================
async def scan_devices(timeout: float = 6.0) -> List[dict]:
    BleakScanner, _ = get_bleak()
    found = await BleakScanner.discover(timeout=timeout)
    out = []
    for d in found:
        out.append({"address": d.address, "name": d.name or "（未命名）",
                    "rssi": getattr(d, "rssi", None)})
    out.sort(key=lambda x: (-(x["rssi"] or -999), x["name"]))
    return out


async def enumerate_gatt(address: str, log: Callable[[str], None]) -> List[str]:
    _, BleakClient = get_bleak()
    lines: List[str] = []
    async with BleakClient(address, timeout=15) as client:
        log(f"已连接 {address}，MTU={client.mtu_size}")
        for service in client.services:
            lines.append(f"[服务] {service.uuid}  {service.description or ''}")
            log(lines[-1])
            for ch in service.characteristics:
                props = ",".join(ch.properties)
                lines.append(f"  [特征] {ch.uuid}  ({props})")
                log(lines[-1])
    return lines


async def send_payload(address: str, template: BleTemplate, payload: bytes,
                       log: Callable[[str], None], ack_timeout: float = 8.0) -> Optional[bytes]:
    """连接并写入配网载荷。notify_char 配置时等待回执。"""
    _, BleakClient = get_bleak()
    if template.is_placeholder:
        raise ValueError("模板 UUID 还是全零占位符——请在固件协议定稿后修改 "
                         "config/ble-provisioning-template.json")
    async with BleakClient(address, timeout=15) as client:
        log(f"已连接 {address}")
        got: List[bytes] = []

        def _on_notify(_: int, data: bytearray) -> None:
            got.append(bytes(data))
            log(f"← 回执 {len(data)} 字节: {bytes(data)!r}")

        if template.notify_char:
            await client.start_notify(template.notify_char, _on_notify)
        log(f"→ 写入 {len(payload)} 字节 → {template.write_char}")
        await client.write_gatt_char(template.write_char, payload, response=True)
        if template.notify_char:
            import asyncio
            loop_deadline = asyncio.get_event_loop().time() + ack_timeout
            while not got and asyncio.get_event_loop().time() < loop_deadline:
                await asyncio.sleep(0.2)
            await client.stop_notify(template.notify_char)
            if not got:
                log(f"⚠ {ack_timeout:.0f}s 内未收到回执", )
        log("✓ 发送完成")
        return got[0] if got else None

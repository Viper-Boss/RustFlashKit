# SPDX-License-Identifier: GPL-3.0-only
"""Keyless maintenance via the dedicated USB CDC ACM serial interface."""
import json

from . import storage_layout

ACTIONS = {"identify": "IDENTIFY", "device-info": "DEVICE_INFO", "ssh-status": "SSH_STATUS",
           "ssh-on": "SSH_ON", "ssh-off": "SSH_OFF"}
IDENTITY = {"ok": True, "protocol": 1, "product": "RustSimHub", "role": "local-administrator"}
RESPONSE_LIMIT = 256


def pick_port(requested, ports):
    candidates = [p.device for p in ports if (p.vid, p.pid) == (0x1D6B, 0x0104)]
    if requested:
        if requested not in candidates:
            raise ValueError("指定端口不是 RustSimHub USB 串口")
        return requested
    if len(candidates) != 1:
        raise ValueError("请连接一根已启动的棒子，或指定它的 USB 串口")
    return candidates[0]


def exchange(serial_port, command):
    data = f"RSHUSB1 {command}\n".encode("ascii")
    if serial_port.write(data) != len(data):
        raise ValueError("USB 控制请求未完整送达")
    raw = serial_port.read_until(b"\n", RESPONSE_LIMIT)
    if len(raw) >= RESPONSE_LIMIT or not raw.endswith(b"\n"):
        raise ValueError("USB 串口未返回完整结果，请重新读取状态")
    try:
        response = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("USB 串口回复格式无效") from None
    if not isinstance(response, dict):
        raise ValueError("USB 串口回复格式无效")
    return response


def run(action, port=None):
    import serial
    from serial.tools import list_ports

    with serial.Serial(pick_port(port, list_ports.comports()), 115200, timeout=12, write_timeout=2) as link:
        # Start the maintenance exchange on a clean serial frame after a possible disconnect.
        link.reset_input_buffer()
        if exchange(link, "IDENTIFY") != IDENTITY:
            raise ValueError("USB 串口未报告 RustSimHub 维护协议")
        if action == "identify":
            return IDENTITY.copy()
        response = exchange(link, ACTIONS[action])
        if response.get("ok") is not True:
            messages = {
                "ssh_identity_unavailable": "设备没有可用的 SSH 主机身份或维护公钥",
                "ssh_start_failed": "SSH 服务启动失败",
                "ssh_stop_failed": "SSH 服务停止结果待确认",
                "ssh_policy_unavailable": "SSH 开关保存失败",
            }
            error = response.get("error")
            raise ValueError(messages.get(error, "设备未确认 SSH 操作") if isinstance(error, str)
                             else "设备未确认 SSH 操作")
        if action == "device-info":
            sectors = response.get("sectors")
            if response.get("board") not in ("UFI003", "unknown") or type(sectors) is not int or sectors <= 0:
                raise ValueError("USB 串口未返回有效的设备信息")
            return response
        if any(type(response.get(field)) is not bool for field in ("ssh_enabled", "ssh_running")):
            raise ValueError("USB 串口未返回有效的 SSH 状态")
        return response


def describe(response):
    if response == IDENTITY:
        return "RustSimHub USB 维护通道已连接"
    if "sectors" in response:
        size = response["sectors"] * storage_layout.SECTOR_BYTES
        try:
            profile = f"（{storage_layout.capacity_profile(size)} GB 档）"
        except storage_layout.LayoutError:
            profile = ""
        board = response["board"] if response["board"] != "unknown" else "型号未知"
        return f"{board} · eMMC {size:,} 字节{profile}"
    enabled = "开启" if response["ssh_enabled"] else "关闭"
    running = "运行中" if response["ssh_running"] else "未运行"
    return f"SSH 设置为{enabled}，服务{running}"

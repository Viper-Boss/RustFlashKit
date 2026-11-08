# SPDX-License-Identifier: GPL-3.0-only
"""在 Windows 上找处于 9008（EDL）模式的设备。只枚举，不打开设备。"""
import os

QUALCOMM_VID = 0x05C6
EDL_PID = 0x9008


def serial_candidates(list_ports=None):
    """高通官方 QDLoader 驱动：设备表现为 COM 口。"""
    if list_ports is None:
        from serial.tools import list_ports as module
        list_ports = module.comports
    found = []
    for port in list_ports():
        if port.vid == QUALCOMM_VID and port.pid == EDL_PID:
            found.append({'mode': 'serial', 'port': port.device, 'description': port.description or ''})
    return found


def libusb_backend():
    """使用 edl 自带的 libusb-1.0.dll（Windows），其他系统用系统库。"""
    import usb.backend.libusb1
    if os.name == 'nt':
        import edlclient
        dll = os.path.join(os.path.dirname(edlclient.__file__), 'Windows', 'libusb-1.0.dll')
        return usb.backend.libusb1.get_backend(find_library=lambda _name: dll)
    return usb.backend.libusb1.get_backend()


def usb_candidates(find=None):
    """WinUSB/libusb 驱动（Zadig 安装）。"""
    if find is None:
        import usb.core
        backend = libusb_backend()
        if backend is None:
            return []

        def find():
            return list(usb.core.find(find_all=True, idVendor=QUALCOMM_VID, idProduct=EDL_PID, backend=backend))
    return [{'mode': 'usb', 'port': f'{dev.bus}-{dev.address}', 'description': 'WinUSB/libusb'} for dev in find()]


def detect(list_ports=None, find=None):
    """返回 (设备列表, 提示列表)。同一设备可能同时出现在两种枚举里，以 COM 口优先。"""
    notes = []
    try:
        serial = serial_candidates(list_ports)
    except Exception as err:  # 枚举失败不应让界面崩溃
        serial, notes = [], [f'COM 口枚举失败：{err}']
    try:
        usb = usb_candidates(find)
    except Exception as err:
        usb = []
        notes.append(f'libusb 枚举失败：{err}')
    if serial:
        if usb:
            notes.append('同时在 libusb 中看到 9008 设备，按 COM 口方式连接')
        return serial, notes
    return usb, notes

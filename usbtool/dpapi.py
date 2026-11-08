# SPDX-License-Identifier: GPL-3.0-only
"""用 Windows DPAPI（当前用户）加密本地私钥文件，换一个 Windows 账号无法解开。"""
import ctypes
import os
from ctypes import wintypes


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


ENTROPY = b'RustSimHub-UsbTool client key v1'
CRYPTPROTECT_UI_FORBIDDEN = 0x01


def _blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _call(function, data):
    if os.name != 'nt':
        raise OSError('DPAPI 只在 Windows 上可用')
    source, _keep = _blob(data)
    entropy, _keep2 = _blob(ENTROPY)
    out = DATA_BLOB()
    if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None,
                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(f'DPAPI 失败：{ctypes.GetLastError()}')
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(data):
    crypt = ctypes.windll.crypt32
    return _call(lambda s, _d, e, r, p, f, o: crypt.CryptProtectData(s, 'RustSimHub-UsbTool', e, r, p, f, o), data)


def unprotect(data):
    crypt = ctypes.windll.crypt32
    return _call(lambda s, _d, e, r, p, f, o: crypt.CryptUnprotectData(s, None, e, r, p, f, o), data)

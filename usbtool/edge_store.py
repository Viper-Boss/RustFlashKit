# SPDX-License-Identifier: GPL-3.0-only
"""本机保存的设备信任信息与工具私钥，都在 .local/edge/ 下（不进 git）。

device.json：地址、网址、证书 SHA256、设备公钥（首次连接时由用户核对后写入）。
client.key：工具自己的 Ed25519 私钥，用 Windows DPAPI 按当前用户加密。
"""
import json
import os

from . import dpapi, paths


def folder():
    path = paths.local_dir() / 'edge'
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_device():
    try:
        data = json.loads((folder() / 'device.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ('address', 'origin', 'cert_sha256', 'device_public_key')):
        return None
    return data


def save_device(address, origin, cert_sha256, device_public_key):
    data = {'format': 1, 'address': address, 'origin': origin, 'cert_sha256': cert_sha256,
            'device_public_key': device_public_key}
    tmp = folder() / 'device.json.partial'
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, folder() / 'device.json')
    return data


def forget_device():
    try:
        (folder() / 'device.json').unlink()
    except FileNotFoundError:
        pass


def load_or_create_key(protect=dpapi.protect, unprotect=dpapi.unprotect):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = folder() / 'client.key'
    if path.exists():
        raw = unprotect(path.read_bytes())
        if len(raw) != 32:
            raise ValueError('工具私钥文件损坏；不会自动重建，请检查后手动删除 .local/edge/client.key 并重新授权')
        return Ed25519PrivateKey.from_private_bytes(raw)
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    with open(path, 'xb') as stream:
        stream.write(protect(raw))
        stream.flush()
        os.fsync(stream.fileno())
    return key

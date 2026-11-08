# SPDX-License-Identifier: GPL-3.0-only
"""核对高通 MBN（ELF32）镜像自带的 SHA-256 哈希段。只读，纯计算。

哈希段是 p_flags 第 24–26 位为 2 的那个程序段：40 字节头之后，每个程序头一条 SHA-256，全零表示不校验。
移植自 RustSimHub 二手棒子离线分析用的校验办法（同一版权人）。
"""
import hashlib
import struct


def verify(data):
    """返回 (通过的段数, 不通过的段号列表)。不是可校验的 MBN 时抛 ValueError。"""
    if data[:4] != b'\x7fELF' or data[4] != 1 or data[5] != 1:
        raise ValueError('不是 32 位小端 ELF')
    phoff, = struct.unpack_from('<I', data, 28)
    phentsize, phnum = struct.unpack_from('<HH', data, 42)
    if phentsize != 32 or not 2 <= phnum <= 128 or phoff + 32 * phnum > len(data):
        raise ValueError('程序头无效')
    headers = [struct.unpack_from('<8I', data, phoff + 32 * i) for i in range(phnum)]
    hashed = [i for i, h in enumerate(headers) if (h[6] >> 24) & 7 == 2]
    if len(hashed) != 1:
        raise ValueError('没有唯一的哈希段')
    _, offset, _, _, size, _, _, _ = headers[hashed[0]]
    table = data[offset:offset + size]
    if len(table) < 40 + 32 * phnum:
        raise ValueError('哈希段不完整')
    passed, failed = 0, []
    for i, (_, seg_offset, _, _, seg_size, _, _, _) in enumerate(headers):
        if i == hashed[0]:
            continue
        want = table[40 + 32 * i:72 + 32 * i]
        if want == bytes(32):
            continue
        if seg_offset + seg_size <= len(data) and hashlib.sha256(data[seg_offset:seg_offset + seg_size]).digest() == want:
            passed += 1
        else:
            failed.append(i)
    return passed, failed


def intact(data):
    """所有带哈希的段都通过，且至少有一段。"""
    try:
        passed, failed = verify(data)
    except ValueError:
        return False
    return passed > 0 and not failed

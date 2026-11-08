# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import struct

import pytest

from usbtool import mbn

HASH_FLAG = 2 << 24


def make_mbn(payload):
    """ELF32 头 + 哈希段 + 一个代码段；哈希段覆盖头和代码段。"""
    phnum = 3
    head_len, table_len = 52 + 32 * phnum, 40 + 32 * phnum
    table_at = 4096
    code_at = table_at + table_len
    code = payload * 64

    def phdr(offset, size, flags):
        return struct.pack('<8I', 1, offset, 0, 0, size, size, flags, 4)
    ehdr = b'\x7fELF\x01\x01\x01' + bytes(9) + struct.pack('<HHIIIIIHHHHHH', 2, 40, 1, 0, 52, 0, 0, 52, 32, phnum, 40, 0, 0)
    head = ehdr + phdr(0, head_len, 5) + phdr(table_at, table_len, HASH_FLAG) + phdr(code_at, len(code), 5)
    table = bytes(40) + hashlib.sha256(head).digest() + bytes(32) + hashlib.sha256(code).digest()
    return head.ljust(table_at, b'\0') + table + code


def test_intact_image_passes():
    assert mbn.verify(make_mbn(b'x')) == (2, [])
    assert mbn.intact(make_mbn(b'x') + bytes(1000))  # 分区尾部补零不影响


def test_one_flipped_bit_fails():
    data = bytearray(make_mbn(b'x'))
    data[-3] ^= 4
    assert mbn.verify(bytes(data)) == (1, [2])
    assert not mbn.intact(bytes(data))


@pytest.mark.parametrize('data', [b'', bytes(4096), b'\x7fELF\x02' + bytes(100)])
def test_not_an_mbn_is_not_intact(data):
    assert not mbn.intact(data)
    with pytest.raises(ValueError):
        mbn.verify(data)


def test_image_without_hash_segment_is_not_intact():
    data = bytearray(make_mbn(b'x'))
    struct.pack_into('<I', data, 52 + 32 + 24, 5)  # 去掉哈希段标志
    assert not mbn.intact(bytes(data))

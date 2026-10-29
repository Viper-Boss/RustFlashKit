# SPDX-License-Identifier: GPL-3.0-only
import os
import random

import pytest

from usbtool import gpt

DISK_SECTORS = 8192
PARTS = [('sbl1', 34, 99), ('modem', 100, 1099), ('fsg', 1100, 1199), ('modemst1', 1200, 1299),
         ('boot', 1300, 1555), ('rootfs', 1556, 6999), ('NSHSTATE', 7000, 8100)]


class FakeDisk:
    """内存中的合成 eMMC：带合法主/备 GPT。可注入二次读取不一致或短读。"""

    def __init__(self, sectors=DISK_SECTORS, parts=PARTS, seed=1):
        rng = random.Random(seed)
        self.data = bytearray(rng.randbytes(sectors * 512))
        primary, backup = gpt.build_gpt(sectors, parts)
        self.data[:len(primary)] = primary
        self.data[-len(backup):] = backup
        self.reads = []
        self.flip_after = None  # 第 N 次读取后翻转一个字节，模拟传输错误
        self.short = False

    def read(self, start, count):
        self.reads.append((start, count))
        if start < 0 or start + count > len(self.data) // 512:
            raise IOError('越界读取')
        chunk = bytearray(self.data[start * 512:(start + count) * 512])
        if self.flip_after is not None and len(self.reads) > self.flip_after:
            chunk[0] ^= 0xFF
        if self.short:
            chunk = chunk[:-1]
        return bytes(chunk)

    def range(self, first, last):
        return bytes(self.data[first * 512:(last + 1) * 512])


@pytest.fixture
def disk():
    return FakeDisk()


@pytest.fixture(autouse=True)
def local_dir(tmp_path, monkeypatch):
    path = tmp_path / 'local'
    monkeypatch.setenv('USBTOOL_LOCAL_DIR', str(path))
    return path


@pytest.fixture
def small_chunks(monkeypatch):
    from usbtool import backup
    monkeypatch.setattr(backup, 'CHUNK_SECTORS', 300)

# SPDX-License-Identifier: GPL-3.0-only
import struct
import zlib

import pytest

from usbtool import gpt
from conftest import DISK_SECTORS, PARTS


def build():
    return gpt.build_gpt(DISK_SECTORS, PARTS)


def test_parse_valid_layout():
    primary, backup = build()
    assert gpt.disk_sectors_from_primary(primary) == DISK_SECTORS
    layout = gpt.parse_gpt(primary, backup, DISK_SECTORS)
    names = [p['name'] for p in layout['partitions']]
    assert names == [p[0] for p in PARTS]
    boot = layout['partitions'][4]
    assert boot == {'index': 5, 'name': 'boot', 'first_sector': 1300, 'last_sector': 1555, 'sectors': 256, 'bytes': 256 * 512}
    assert layout['gpt_sha256'] == gpt.gpt_digest(primary, backup)


def test_header_crc_mismatch_rejected():
    primary, backup = build()
    bad = bytearray(primary)
    bad[512 + 40] ^= 1  # 改 first_usable，不更新 CRC
    with pytest.raises(gpt.GptError, match='CRC'):
        gpt.parse_gpt(bytes(bad), backup, DISK_SECTORS)


def test_entry_tamper_rejected():
    primary, backup = build()
    bad = bytearray(primary)
    bad[1024 + 60] ^= 1  # 改一个分区名
    with pytest.raises(gpt.GptError, match='表项'):
        gpt.parse_gpt(bytes(bad), backup, DISK_SECTORS)


def test_wrong_disk_size_rejected():
    primary, backup = build()
    with pytest.raises(gpt.GptError):
        gpt.parse_gpt(primary, backup, DISK_SECTORS + 1)


def test_overlap_rejected():
    primary, backup = gpt.build_gpt(DISK_SECTORS, [('a', 34, 200), ('b', 150, 300)])
    with pytest.raises(gpt.GptError, match='重叠'):
        gpt.parse_gpt(primary, backup, DISK_SECTORS)


def test_short_capture_rejected():
    primary, backup = build()
    with pytest.raises(gpt.GptError, match='长度'):
        gpt.parse_gpt(primary[:-512], backup, DISK_SECTORS)


def test_missing_signature_rejected():
    primary, backup = build()
    with pytest.raises(gpt.GptError, match='签名'):
        gpt.disk_sectors_from_primary(bytes(len(primary)))


def test_sixteen_entry_table_like_ufi003():
    # UFI003 实测：16 个 128 字节表项，但保留 32 扇区表区；CRC 只覆盖声明的表项。
    primary, backup = build()
    count = struct.unpack_from('<I', primary, 512 + 80)[0]
    assert count == 16
    assert zlib.crc32(primary[1024:1024 + 16 * 128]) == struct.unpack_from('<I', primary, 512 + 88)[0]

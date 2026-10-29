# SPDX-License-Identifier: GPL-3.0-only
"""GPT 解析与主/备校验。校验规则移植自 RustSimHub tools/recovery/edl_readonly.py。"""
import hashlib
import struct
import zlib

SECTOR = 512
PRIMARY_SECTORS = 34  # LBA0 保护 MBR + LBA1 头 + 32 扇区表项
BACKUP_SECTORS = 33   # 32 扇区表项 + 最后一个扇区的备份头


class GptError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise GptError(message)


def parse_header(blob, expected_lba):
    _require(len(blob) >= 92 and blob[:8] == b'EFI PART', '缺少 GPT 签名')
    revision, size, checksum, reserved = struct.unpack_from('<4I', blob, 8)
    _require(revision == 0x10000 and 92 <= size <= SECTOR and reserved == 0, '不支持的 GPT 头')
    checked = bytearray(blob[:size])
    checked[16:20] = bytes(4)
    _require(zlib.crc32(checked) == checksum, 'GPT 头 CRC 不匹配')
    current, alternate, first, last = struct.unpack_from('<4Q', blob, 24)
    _require(expected_lba is None or current == expected_lba, 'GPT 头所在 LBA 不对')
    entries_lba, count, entry_size, entries_crc = struct.unpack_from('<Q3I', blob, 72)
    _require(entry_size == 128 and 0 < count and count * entry_size <= 32 * SECTOR, '不支持的 GPT 表项几何')
    return {'current': current, 'alternate': alternate, 'first_usable': first, 'last_usable': last,
            'disk_guid': blob[56:72], 'entries_lba': entries_lba, 'count': count, 'entries_crc': entries_crc}


def disk_sectors_from_primary(primary):
    """读主 GPT 头得到磁盘总扇区数（备份头位于最后一个扇区）。"""
    _require(len(primary) == PRIMARY_SECTORS * SECTOR, '主 GPT 读取长度不对')
    header = parse_header(primary[SECTOR:2 * SECTOR], 1)
    _require(header['alternate'] > PRIMARY_SECTORS + BACKUP_SECTORS, '备份 GPT 位置异常')
    return header['alternate'] + 1


def parse_gpt(primary, backup, disk_sectors):
    """校验主备两份 GPT 一致且 CRC 正确，返回分区布局。"""
    _require(len(primary) == PRIMARY_SECTORS * SECTOR and len(backup) == BACKUP_SECTORS * SECTOR, 'GPT 读取长度不对')
    a = parse_header(primary[SECTOR:2 * SECTOR], 1)
    b = parse_header(backup[-SECTOR:], disk_sectors - 1)
    _require(a['alternate'] == disk_sectors - 1 and b['alternate'] == 1, '主备 GPT 互指位置不对')
    _require(PRIMARY_SECTORS <= a['first_usable'] <= a['last_usable'] < disk_sectors - BACKUP_SECTORS, 'GPT 可用范围越界')
    _require((a['first_usable'], a['last_usable'], a['disk_guid'], a['count'])
             == (b['first_usable'], b['last_usable'], b['disk_guid'], b['count']), '主备 GPT 头不一致')
    _require(a['entries_lba'] == 2 and b['entries_lba'] == disk_sectors - BACKUP_SECTORS, 'GPT 表项位置不对')
    length = a['count'] * 128
    entries = primary[2 * SECTOR:2 * SECTOR + length]
    _require(entries == backup[:length], '主备 GPT 表项不一致')
    _require(zlib.crc32(entries) == a['entries_crc'] == b['entries_crc'], 'GPT 表项 CRC 不匹配')
    partitions = []
    for i in range(a['count']):
        row = entries[i * 128:(i + 1) * 128]
        if row[:16] == bytes(16):
            continue
        first, last = struct.unpack_from('<2Q', row, 32)
        name = row[56:128].decode('utf-16-le', errors='strict').split('\0', 1)[0]
        _require(name, f'分区 {i + 1} 没有名字')
        _require(name not in {p['name'] for p in partitions}, f'分区名 {name} 重复')
        _require(a['first_usable'] <= first <= last <= a['last_usable'], f'分区 {name} 越界')
        for p in partitions:
            _require(last < p['first_sector'] or first > p['last_sector'], f'分区 {name} 与 {p["name"]} 重叠')
        partitions.append({'index': i + 1, 'name': name, 'first_sector': first, 'last_sector': last,
                           'sectors': last - first + 1, 'bytes': (last - first + 1) * SECTOR})
    return {'disk_sectors': disk_sectors, 'disk_bytes': disk_sectors * SECTOR,
            'gpt_sha256': gpt_digest(primary, backup), 'partitions': partitions}


def gpt_digest(primary, backup):
    """与 RustSimHub 相同的定义：前 34 扇区 + 最后 33 扇区拼接后的 SHA256。"""
    return hashlib.sha256(primary + backup).hexdigest()


def build_gpt(disk_sectors, partitions, count=16):
    """生成合成 GPT（测试和夹具用）。partitions: [(name, first, last)]。返回 (primary, backup)。"""
    entries = bytearray(count * 128)
    for i, (name, first, last) in enumerate(partitions):
        row = bytearray(128)
        row[0:16] = bytes([0xA0 + i]) * 16  # 类型 GUID（任意非零）
        row[16:32] = bytes([0x10 + i]) * 16
        struct.pack_into('<2Q', row, 32, first, last)
        encoded = name.encode('utf-16-le')
        row[56:56 + len(encoded)] = encoded
        entries[i * 128:(i + 1) * 128] = row
    entries = bytes(entries)
    first_usable, last_usable = PRIMARY_SECTORS, disk_sectors - BACKUP_SECTORS - 1

    def header(current, alternate, entries_lba):
        h = bytearray(SECTOR)
        h[0:8] = b'EFI PART'
        struct.pack_into('<4I', h, 8, 0x10000, 92, 0, 0)
        struct.pack_into('<4Q', h, 24, current, alternate, first_usable, last_usable)
        h[56:72] = b'\x42' * 16
        struct.pack_into('<Q3I', h, 72, entries_lba, count, 128, zlib.crc32(entries))
        struct.pack_into('<I', h, 16, zlib.crc32(bytes(h[:92])))
        return bytes(h)

    table = entries + bytes(32 * SECTOR - len(entries))
    primary = bytes(SECTOR) + header(1, disk_sectors - 1, 2) + table
    backup = table + header(disk_sectors - 1, 1, disk_sectors - BACKUP_SECTORS)
    return primary, backup

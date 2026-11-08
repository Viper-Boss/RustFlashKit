# SPDX-License-Identifier: GPL-3.0-only
"""由棒子自己的原厂 GPT 生成 RustSimHub 分区表。移植自 RustSimHub tools/second-board/plan_gpt.py（同一版权人）。

保留分区的 128 字节表项原样不变（LBA、类型和唯一 GUID 都不动），删除安卓系统区，
在腾出的空间里新建 boot、NSHSTATE、devinfo、rootfs。生成后重新解析，机器检查安全性质。
"""
import struct
import uuid
import zlib

from . import gpt

SECTOR = gpt.SECTOR
DISK_SECTORS = 7634944
NSHSTATE_SECTORS = 264159  # rustsimhub-state-check 固定检查：第 15 条、这个扇区数
PROTECTED = ('modem', 'sbl1', 'sbl1bak', 'aboot', 'abootbak', 'rpm', 'rpmbak', 'tz', 'tzbak', 'hyp', 'hypbak',
             'pad', 'modemst1', 'modemst2', 'misc', 'fsc', 'ssd', 'splash', 'DDR', 'fsg', 'sec', 'persist')
EXPECTED_STOCK = ('modem', 'sbl1', 'sbl1bak', 'aboot', 'abootbak', 'rpm', 'rpmbak', 'tz', 'tzbak', 'hyp', 'hypbak',
                  'pad', 'modemst1', 'modemst2', 'misc', 'fsc', 'ssd', 'splash', 'DDR', 'fsg', 'sec', 'boot',
                  'system', 'persist', 'cache', 'recovery', 'userdata')
LINUX_DATA = uuid.UUID('0fc63daf-8483-4772-8e79-3d69d8477de4')
DEVINFO_TYPE = uuid.UUID('65addcf4-0c5c-4d9a-ac2d-d90b5cbfcd03')
NAMESPACE = uuid.UUID('6e736866-7566-6930-3033-736563737469')
# 名字 -> (起始扇区, 扇区数, 类型 GUID；None 表示沿用原厂 boot 的类型)
NEW = {
    'boot': (1116672, 131072, None),
    'NSHSTATE': (1247744, NSHSTATE_SECTORS, LINUX_DATA),
    'devinfo': (1511936, 2048, DEVINFO_TYPE),
    'rootfs': (4789248, 2845656, LINUX_DATA),
}
ORDER = ('modem', 'sbl1', 'sbl1bak', 'aboot', 'abootbak', 'rpm', 'rpmbak', 'tz', 'tzbak', 'hyp', 'hypbak',
         'boot', 'modemst1', 'rootfs', 'NSHSTATE', 'modemst2', 'misc', 'fsc', 'ssd', 'splash', 'DDR', 'fsg',
         'sec', 'persist', 'pad', 'devinfo')


class GptPlanError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise GptPlanError(message)


def classify(layout):
    """'stock'：原厂安卓布局；'target'：已是 RustSimHub 布局；否则 None（不支持）。"""
    names = tuple(p['name'] for p in layout['partitions'])
    if layout['disk_sectors'] != DISK_SECTORS:
        return None
    if names == EXPECTED_STOCK:
        return 'stock'
    by_name = {p['name']: p for p in layout['partitions']}
    if names == ORDER and all((by_name[n]['first_sector'], by_name[n]['sectors']) == NEW[n][:2] for n in NEW):
        return 'target'
    return None


def _entries(table, count):
    rows = {}
    for i in range(count):
        row = table[i * 128:(i + 1) * 128]
        if row[:16] != bytes(16):
            rows[row[56:128].decode('utf-16-le').split('\0', 1)[0]] = bytes(row)
    return rows


def _entry(name, first, sectors, type_guid):
    encoded = name.encode('utf-16-le')
    return (type_guid.bytes_le + uuid.uuid5(NAMESPACE, name).bytes_le + struct.pack('<QQQ', first, first + sectors - 1, 0)
            + encoded + bytes(72 - len(encoded)))


def _header(stock, current, backup, entries_lba, crc):
    header = bytearray(stock)
    header[24:40] = struct.pack('<QQ', current, backup)
    header[72:80] = struct.pack('<Q', entries_lba)
    header[88:92] = struct.pack('<I', crc)
    header[16:20] = bytes(4)
    header[16:20] = struct.pack('<I', zlib.crc32(header))
    return bytes(header) + bytes(SECTOR - 92)


def build(primary, backup):
    """输入原厂主/备 GPT（各 34/33 扇区），返回 (新主表, 新备表, 新布局)。"""
    layout = gpt.parse_gpt(primary, backup, DISK_SECTORS)
    _require(classify(layout) == 'stock', '不是预期的原厂安卓分区布局')
    head = gpt.parse_header(primary[SECTOR:2 * SECTOR], 1)
    _require(head['count'] >= len(ORDER), '原厂表项数不够')
    old = _entries(primary[2 * SECTOR:], head['count'])
    boot_type = uuid.UUID(bytes_le=old['boot'][:16])
    rows = {name: old[name] for name in PROTECTED}
    for name, (first, sectors, type_guid) in NEW.items():
        rows[name] = _entry(name, first, sectors, type_guid or boot_type)
    table = b''.join(rows[name] for name in ORDER)
    table += bytes(head['count'] * 128 - len(table))
    crc = zlib.crc32(table)
    padded = table + bytes(32 * SECTOR - len(table))
    stock_header = primary[SECTOR:SECTOR + 92]
    new_primary = primary[:SECTOR] + _header(stock_header, 1, DISK_SECTORS - 1, 2, crc) + padded
    new_backup = padded + _header(stock_header, DISK_SECTORS - 1, 1, DISK_SECTORS - gpt.BACKUP_SECTORS, crc)

    try:
        new_layout = gpt.parse_gpt(new_primary, new_backup, DISK_SECTORS)  # 重新解析：CRC、主备一致、无重叠、不越界
    except gpt.GptError as err:
        raise GptPlanError(f'这根棒子的分区位置与目标布局冲突（可能碰到保留分区）：{err}') from None
    _require(classify(new_layout) == 'target', '生成的分区表不符合目标布局')
    new_rows = _entries(new_primary[2 * SECTOR:], head['count'])
    for name in PROTECTED:
        _require(new_rows[name] == old[name], f'{name} 的表项被改动')
    by_index = {p['index']: p for p in new_layout['partitions']}
    _require(by_index[15]['name'] == 'NSHSTATE' and by_index[15]['sectors'] == NSHSTATE_SECTORS,
             'NSHSTATE 必须是第 15 条且扇区数固定')
    keep = [p for p in layout['partitions'] if p['name'] in PROTECTED]
    for name in NEW:
        first, sectors = NEW[name][:2]
        for p in keep:
            _require(first + sectors <= p['first_sector'] or first > p['last_sector'], f'{name} 会碰到保留分区 {p["name"]}')
    return new_primary, new_backup, new_layout

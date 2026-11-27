# SPDX-License-Identifier: GPL-3.0-only
"""Generate an offline UFI001C ARM64 boot-chain GPT candidate from captured tables."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import uuid
import zipfile
import zlib

from . import gpt, gpt_plan

STOCK_TZ_SECTORS = 1024
ARM64_TZ_SECTORS = 2048
TZ_TYPE = uuid.UUID('a053aa7f-40b8-4b1c-ba08-2f68ac71a4f4').bytes_le
CDT_TYPE = uuid.UUID('a19f205f-ccd8-4b6d-8f1e-2d9bc24cffb1').bytes_le
CDT_SECTORS = 4
OPENSTICK_PAYLOADS = {
    'sbl1': ('sbl1.mbn', 524288, '6565a3c61c0f7413f10f1027c7f9b085335aeaabc379338c37c57917ac90c4aa'),
    'rpm': ('rpm.mbn', 524288, '551beae07ab831204c8d8362d2344c37b03ba6bd590984eb44b7f560ee6b94b7'),
    'tz': ('tz.mbn', 605312, '2daaedaafeee5697b6d85aa329cc6af9bc7e661677e4594d75efb54e4b53fea1'),
    'hyp': ('hyp.mbn', 12288, '0680b42298a7d691161e427543211f3ec528b21c901de6e5b13e833e772776cf'),
    'cdt': ('sbc_1.0_8016.bin', 420, '5e2004e3b46d22a3b2b429271ff140ff77f8dd5c0816947579eca98243d69f31'),
}


def require(condition, message):
    if not condition:
        raise gpt.GptError(message)


def build(primary, alternate, backup_start, cdt_start):
    disk = gpt.disk_sectors_from_primary(primary)
    source = gpt.parse_gpt(primary, alternate, disk)
    parts = {row['name']: row for row in source['partitions']}
    require({'tz', 'tzbak', 'sbl1', 'hyp', 'aboot', 'boot'} <= parts.keys(), '缺少 UFI001C 引导链分区')
    require('cdt' not in parts, '现有 CDT 分区需要单独核对')
    tz, tzbak = parts['tz'], parts['tzbak']
    require(tz['sectors'] == tzbak['sectors'] == STOCK_TZ_SECTORS and
            tz['last_sector'] + 1 == tzbak['first_sector'], '只支持原厂相邻的 TZ 主备分区')
    require(backup_start % ARM64_TZ_SECTORS == 0, '备用 TZ 位置须按 1 MiB 对齐')
    backup_last = backup_start + ARM64_TZ_SECTORS - 1
    cdt_last = cdt_start + CDT_SECTORS - 1
    require(cdt_last < backup_start or cdt_start > backup_last, '备用 TZ 与 CDT 重叠')

    first_header = primary[gpt.SECTOR:2 * gpt.SECTOR]
    second_header = alternate[-gpt.SECTOR:]
    header = gpt.parse_header(first_header, 1)
    for name, first, last in (('tzbak', backup_start, backup_last), ('cdt', cdt_start, cdt_last)):
        require(header['first_usable'] <= first <= last <= header['last_usable'], f'{name} 位置越界')
        require(all(last < row['first_sector'] or first > row['last_sector'] for row in parts.values()),
                f'{name} 会占用现有分区')
    require(all(struct.unpack_from('<I', raw, 12)[0] == 92 for raw in (first_header, second_header)),
            '仅支持 92 字节 GPT 头格式')
    table = bytearray(primary[2 * gpt.SECTOR:])
    tz_offset = (tz['index'] - 1) * 128
    tzbak_offset = (tzbak['index'] - 1) * 128
    require(table[tz_offset:tz_offset + 16] == TZ_TYPE, '主 TZ 的分区类型与 UFI001C 原厂格式不一致')
    struct.pack_into('<Q', table, tz_offset + 40, tzbak['last_sector'])
    struct.pack_into('<QQ', table, tzbak_offset + 32, backup_start, backup_last)
    indexes = {row['index'] for row in parts.values()}
    cdt_index = next((i for i in range(1, header['count'] + 1) if i not in indexes), None)
    require(cdt_index is not None, '没有可用于 CDT 的空 GPT 表项')
    cdt_offset = (cdt_index - 1) * 128
    cdt_guid = uuid.uuid5(uuid.UUID(bytes_le=header['disk_guid']), 'RustSimHub UFI001C CDT').bytes_le
    require(all(table[(i - 1) * 128 + 16:(i - 1) * 128 + 32] != cdt_guid for i in indexes), 'CDT GUID 冲突')
    table[cdt_offset:cdt_offset + 128] = (CDT_TYPE + cdt_guid + struct.pack('<QQQ', cdt_start, cdt_last, 0)
                                          + 'cdt'.encode('utf-16-le').ljust(72, b'\0'))
    crc = zlib.crc32(table[:header['count'] * 128])

    new_primary = (primary[:gpt.SECTOR] + gpt_plan._header(first_header[:92], 1, disk - 1, 2, crc) + bytes(table))
    new_alternate = bytes(table) + gpt_plan._header(second_header[:92], disk - 1, 1, disk - gpt.BACKUP_SECTORS, crc)
    parsed = gpt.parse_gpt(new_primary, new_alternate, disk)
    target = {row['name']: row for row in parsed['partitions']}
    require(target.keys() == parts.keys() | {'cdt'} and all(target[n] == parts[n] for n in parts if n not in ('tz', 'tzbak')),
            '迁移候选改变了其他分区')
    return new_primary, new_alternate, {
        'kind': 'ufi001c-arm64-boot-chain-gpt-candidate',
        'source_gpt_sha256': source['gpt_sha256'],
        'candidate_gpt_sha256': parsed['gpt_sha256'],
        'source': {n: parts[n] for n in ('tz', 'tzbak')},
        'candidate': {n: target[n] for n in ('tz', 'tzbak', 'cdt')},
        'live_destination_checks': [target[n] for n in ('tzbak', 'cdt')],
        'hardware_boot_verified': False,
        'write_capable': False,
    }


def stage_payloads(archive_path, layout):
    """Pin OpenStick v1 members and fit them to the checked candidate extents."""
    parts = {row['name']: row for row in layout['partitions']}
    staged = {}
    with zipfile.ZipFile(archive_path) as archive:
        for name, (member, size, digest) in OPENSTICK_PAYLOADS.items():
            with archive.open(f'base/{member}') as source:
                payload = source.read(size + 1)
            require(len(payload) == size and hashlib.sha256(payload).hexdigest() == digest,
                    f'OpenStick v1 文件摘要不匹配：{member}')
            targets = (name,) if name == 'cdt' else (name, name + 'bak')
            padded = payload + bytes(-size % gpt.SECTOR)
            for target in targets:
                require(target in parts and len(padded) <= parts[target]['bytes'], f'{target} 容量不足或缺少')
            staged[name] = (padded, targets, digest)
    return staged


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('primary', type=Path)
    parser.add_argument('alternate', type=Path)
    parser.add_argument('backup_start', type=int)
    parser.add_argument('cdt_start', type=int)
    parser.add_argument('output', type=Path)
    parser.add_argument('--openstick-base', type=Path, help='OpenStick v1 base.zip 或 base-generic.zip')
    args = parser.parse_args()
    primary, alternate, report = build(args.primary.read_bytes(), args.alternate.read_bytes(),
                                       args.backup_start, args.cdt_start)
    staged = {}
    if args.openstick_base is not None:
        layout = gpt.parse_gpt(primary, alternate, gpt.disk_sectors_from_primary(primary))
        staged = stage_payloads(args.openstick_base, layout)
        report['payloads'] = {name: {'file': name + '.candidate.bin', 'targets': targets, 'source_sha256': digest,
                                     'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                              for name, (data, targets, digest) in staged.items()}
    args.output.mkdir(mode=0o700)
    (args.output / 'gpt-primary.candidate.bin').write_bytes(primary)
    (args.output / 'gpt-backup.candidate.bin').write_bytes(alternate)
    for name, (data, _, _) in staged.items():
        (args.output / (name + '.candidate.bin')).write_bytes(data)
    (args.output / 'preflight.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()

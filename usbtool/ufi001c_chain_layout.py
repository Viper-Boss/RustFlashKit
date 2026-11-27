# SPDX-License-Identifier: GPL-3.0-only
"""Generate an offline UFI001C ARM64 boot-chain GPT candidate from captured tables."""

import argparse
import hashlib
import json
from pathlib import Path
import stat
import struct
import uuid
import zipfile
import zlib

from . import backup, gpt, gpt_plan

STOCK_TZ_SECTORS = 1024
ARM64_TZ_SECTORS = 2048
TZ_TYPE = uuid.UUID('a053aa7f-40b8-4b1c-ba08-2f68ac71a4f4').bytes_le
CDT_TYPE = uuid.UUID('a19f205f-ccd8-4b6d-8f1e-2d9bc24cffb1').bytes_le
CDT_SECTORS = 4
# Matches the CDT entry in the OpenStick v1 base and base-generic GPTs.
CDT_ATTRIBUTES = 1 << 60
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
    table[cdt_offset:cdt_offset + 128] = (CDT_TYPE + cdt_guid + struct.pack('<QQQ', cdt_start, cdt_last, CDT_ATTRIBUTES)
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


def rollback_ranges(source, candidate, staged):
    """Cover every planned write with original GPT, partition or blank-space bytes."""
    disk = source['disk_sectors']
    original = {row['name']: row for row in source['partitions']}
    target = {row['name']: row for row in candidate['partitions']}
    ranges = {
        'gpt-primary': (0, gpt.PRIMARY_SECTORS, False),
        'gpt-backup': (disk - gpt.BACKUP_SECTORS, gpt.BACKUP_SECTORS, False),
    }
    future_locations = {'tzbak', 'cdt'}
    writes = [(0, gpt.PRIMARY_SECTORS), (disk - gpt.BACKUP_SECTORS, gpt.BACKUP_SECTORS)]
    for payload, names, _ in staged.values():
        for name in names:
            dest = target[name]
            writes.append((dest['first_sector'], len(payload) // gpt.SECTOR))
            if name in original:
                part = original[name]
                ranges[name] = (part['first_sector'], part['sectors'], False)
            if name in future_locations:
                ranges[f'gap-{name}'] = (dest['first_sector'], dest['sectors'], True)
    ordered = sorted(ranges.values())
    for (start, count, _), (following, _, _) in zip(ordered, ordered[1:]):
        require(start + count <= following, '回退区段重叠')
    for first, count in writes:
        end = first
        for start, length, _ in ordered:
            if start <= end < start + length:
                end = start + length
        require(end >= first + count, f'回退数据未覆盖可能的写入区段 {first}+{count}')
    return {name: {'first_sector': first, 'sectors': count, 'must_be_zero': blank}
            for name, (first, count, blank) in ranges.items()}


def verify_preimages(read, ranges, original_primary, original_alternate):
    """Collect stable preimages through the same sector reader used by EDL."""
    def read_range(name):
        row = ranges[name]
        return backup.checked_read(read, row['first_sector'], row['sectors'])

    captured = {name: read_range(name) for name in ('gpt-primary', 'gpt-backup')}
    require(captured['gpt-primary'] == original_primary and captured['gpt-backup'] == original_alternate,
            '本次回读的主备 GPT 与迁移来源不一致')
    for name in ranges:
        if name not in captured:
            captured[name] = read_range(name)
    for name, row in ranges.items():
        data = captured[name]
        require(data == read_range(name), f'{name} 两次读取不一致')
        if row['must_be_zero']:
            require(data == bytes(len(data)), f'{name} 内有数据')
    return captured


def verify_9008(link, candidate_folder, baseline_folder, archive, output):
    """Rebuild the candidate from pinned inputs, then verify and save live preimages."""
    original_primary = (baseline_folder / 'target-gpt-primary.bin').read_bytes()
    original_alternate = (baseline_folder / 'target-gpt-backup.bin').read_bytes()
    primary = (candidate_folder / 'gpt-primary.candidate.bin').read_bytes()
    alternate = (candidate_folder / 'gpt-backup.candidate.bin').read_bytes()
    disk = gpt.disk_sectors_from_primary(original_primary)
    source = gpt.parse_gpt(original_primary, original_alternate, disk)
    candidate = gpt.parse_gpt(primary, alternate, disk)
    parts = {row['name']: row for row in candidate['partitions']}
    require({'tzbak', 'cdt'} <= parts.keys(), '候选缺少备用 TZ 或 CDT')
    rebuilt_primary, rebuilt_alternate, _ = build(original_primary, original_alternate,
                                                   parts['tzbak']['first_sector'], parts['cdt']['first_sector'])
    require((primary, alternate) == (rebuilt_primary, rebuilt_alternate), '候选 GPT 与原始输入重算结果不一致')
    staged = stage_payloads(archive, candidate)
    for name, (data, _, _) in staged.items():
        require((candidate_folder / (name + '.candidate.bin')).read_bytes() == data, f'{name} 候选镜像与原始输入不一致')
    ranges = rollback_ranges(source, candidate, staged)
    require(all(row['sectors'] <= ARM64_TZ_SECTORS for row in ranges.values()), '回读区段长度超出当前板型范围')
    require(not output.exists(), '现场回读目录已存在')

    baseline = {name: (baseline_folder / (name + '.bin')).read_bytes()
                for name, row in ranges.items() if not row['must_be_zero'] and not name.startswith('gpt-')}
    require(all(len(data) == ranges[name]['sectors'] * gpt.SECTOR for name, data in baseline.items()),
            '原始引导镜像长度与对应分区不一致')
    link.set_disk_sectors(disk)
    captured = verify_preimages(link.read, ranges, original_primary, original_alternate)
    for name, data in baseline.items():
        require(captured[name] == data, f'{name} 实机引导镜像与所存副本不一致')

    output.mkdir(mode=0o700)
    items = {}
    for name, data in captured.items():
        path = output / (name + '.bin')
        backup._write_new(path, data)
        digest = hashlib.sha256(data).hexdigest()
        require(backup.file_sha256(path) == digest, f'{name} 本地回读文件摘要不一致')
        items[name] = dict(ranges[name], file=path.name, sha256=digest)
    backup.write_manifest(output, {'kind': 'ufi001c-9008-read-only-preflight',
                                   'source_gpt_sha256': source['gpt_sha256'],
                                   'candidate_gpt_sha256': candidate['gpt_sha256'],
                                   'items': items, 'writes_performed': False, 'hardware_boot_verified': False})


def transition_images(ranges, captured, candidate, staged, primary, alternate):
    """Overlay candidate bytes on the preimages, preserving each unwritten sector."""
    require(captured.keys() == ranges.keys(), '缺少回读区段')
    dest = {name: bytearray(data) for name, data in captured.items()}
    require(all(len(dest[name]) == row['sectors'] * gpt.SECTOR for name, row in ranges.items()),
            '回读区段长度不一致')
    parts = {row['name']: row for row in candidate['partitions']}
    writes = [(parts[name]['first_sector'], data)
              for data, targets, _ in staged.values() for name in targets]
    writes.extend(((ranges['gpt-primary']['first_sector'], primary),
                   (ranges['gpt-backup']['first_sector'], alternate)))
    previous_end = 0
    for first, data in sorted(writes):
        end = first + len(data) // gpt.SECTOR
        require(len(data) % gpt.SECTOR == 0 and end > first and first >= previous_end,
                '候选写入区段未对齐或重叠')
        previous_end = end
        remaining = len(data)
        for name, row in ranges.items():
            start = row['first_sector']
            lo, hi = max(first, start), min(end, start + row['sectors'])
            if lo < hi:
                offset = (lo - start) * gpt.SECTOR
                src = (lo - first) * gpt.SECTOR
                length = (hi - lo) * gpt.SECTOR
                dest[name][offset:offset + length] = data[src:src + length]
                remaining -= length
        require(remaining == 0, '候选写入区段没有完整的回读覆盖')
    return {name: bytes(data) for name, data in dest.items()}


def pending_transition(read, ranges, captured, candidate_images, *, restore=False):
    """Reconcile a stopped attempt using only sectors equal to the old or new side.

    Return complete range payloads for the selected direction. The ranges stay
    fixed even if the two on-disk GPT copies now describe different layouts.
    """
    require(captured.keys() == ranges.keys() == candidate_images.keys(), '缺少迁移或回读区段')
    desired = captured if restore else candidate_images
    # Put the backup GPT before the primary GPT, and defer changing the active SBL1 until last.
    deferred = ('gpt-backup', 'gpt-primary', 'sbl1')
    order = [name for name in ranges if name not in deferred] + [name for name in deferred if name in ranges]
    pending = []
    for name in order:
        row = ranges[name]
        first, count = row['first_sector'], row['sectors']
        original, target = captured[name], candidate_images[name]
        require(len(original) == len(target) == count * gpt.SECTOR, f'{name} 区段长度不一致')
        data = backup.checked_read(read, first, count)
        require(data == backup.checked_read(read, first, count), f'{name} 当前状态双读不一致')
        if data == desired[name]:
            continue
        if data != original and data != target:
            for offset in range(0, len(data), gpt.SECTOR):
                section = slice(offset, offset + gpt.SECTOR)
                require(data[section] in (original[section], target[section]),
                        f'{name} 含未识别扇区 {first + offset // gpt.SECTOR}')
        pending.append(({'first_sector': first, 'sectors': count, 'sha256': hashlib.sha256(desired[name]).hexdigest()},
                        desired[name]))
    return pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('primary', type=Path)
    parser.add_argument('alternate', type=Path)
    parser.add_argument('backup_start', type=int)
    parser.add_argument('cdt_start', type=int)
    parser.add_argument('output', type=Path)
    parser.add_argument('--openstick-base', type=Path, help='OpenStick v1 base.zip 或 base-generic.zip')
    parser.add_argument('--check-disk-image', type=Path, help='从普通磁盘镜像双读回退区段并保存副本')
    args = parser.parse_args()
    original_primary, original_alternate = args.primary.read_bytes(), args.alternate.read_bytes()
    primary, alternate, report = build(original_primary, original_alternate, args.backup_start, args.cdt_start)
    staged = {}
    captured = {}
    if args.openstick_base is not None:
        disk = gpt.disk_sectors_from_primary(primary)
        source = gpt.parse_gpt(original_primary, original_alternate, disk)
        layout = gpt.parse_gpt(primary, alternate, disk)
        staged = stage_payloads(args.openstick_base, layout)
        report['payloads'] = {name: {'file': name + '.candidate.bin', 'targets': targets, 'source_sha256': digest,
                                     'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                              for name, (data, targets, digest) in staged.items()}
        report['rollback_ranges'] = rollback_ranges(source, layout, staged)
    if args.check_disk_image is not None:
        if not staged:
            parser.error('--check-disk-image 需要 --openstick-base')
        image = args.check_disk_image
        require(stat.S_ISREG(image.stat().st_mode) and image.stat().st_size == disk * gpt.SECTOR,
                '需要与来源容量相同的常规磁盘镜像文件')
        with image.open('rb') as stream:
            def read(start, count):
                stream.seek(start * gpt.SECTOR)
                return stream.read(count * gpt.SECTOR)

            captured = verify_preimages(read, report['rollback_ranges'], original_primary, original_alternate)
            target = transition_images(report['rollback_ranges'], captured, layout, staged, primary, alternate)
            report['disk_image_transition'] = [segment for segment, _ in pending_transition(
                read, report['rollback_ranges'], captured, target)]
        report['disk_image_preimages'] = {name: {'file': 'image-preimages/' + name + '.bin',
                                               'sha256': hashlib.sha256(data).hexdigest()}
                                         for name, data in captured.items()}
    args.output.mkdir(mode=0o700)
    (args.output / 'gpt-primary.candidate.bin').write_bytes(primary)
    (args.output / 'gpt-backup.candidate.bin').write_bytes(alternate)
    for name, (data, _, _) in staged.items():
        (args.output / (name + '.candidate.bin')).write_bytes(data)
    if captured:
        folder = args.output / 'image-preimages'
        folder.mkdir(mode=0o700)
        for name, data in captured.items():
            (folder / (name + '.bin')).write_bytes(data)
    (args.output / 'preflight.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()

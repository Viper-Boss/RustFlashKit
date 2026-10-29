# SPDX-License-Identifier: GPL-3.0-only
"""读分区表、备份分区并校验。只依赖一个 read(start_sector, count) -> bytes 的读取器，便于用模拟磁盘测试。"""
import datetime
import hashlib
import json
import os
import pathlib
import shutil

from . import gpt

CHUNK_SECTORS = 32768  # 16 MiB 一次
MAX_TRACKED = 200000  # 最多保存这么多个不一致扇区的数据（约 100 MiB）
FULL_DISK = '__full_disk__'
MANIFEST = 'manifest.json'


class BackupError(RuntimeError):
    pass


def checked_read(reader, start, count):
    data = reader(start, count)
    if not isinstance(data, (bytes, bytearray)) or len(data) != count * gpt.SECTOR:
        raise BackupError(f'读取扇区 {start}+{count} 返回长度不对')
    return bytes(data)


def read_layout(reader, on_disk_sectors=None):
    """读主 GPT -> 得到磁盘大小 -> 读备份 GPT -> 校验。on_disk_sectors 用于收紧护栏上限。"""
    primary = checked_read(reader, 0, gpt.PRIMARY_SECTORS)
    sectors = gpt.disk_sectors_from_primary(primary)
    if on_disk_sectors is not None:
        on_disk_sectors(sectors)
    backup = checked_read(reader, sectors - gpt.BACKUP_SECTORS, gpt.BACKUP_SECTORS)
    return gpt.parse_gpt(primary, backup, sectors), primary, backup


def hash_range(reader, start, count, sink=None, progress=None, label=''):
    h = hashlib.sha256()
    done = 0
    while done < count:
        n = min(CHUNK_SECTORS, count - done)
        data = checked_read(reader, start + done, n)
        h.update(data)
        if sink is not None:
            sink.write(data)
        done += n
        if progress:
            progress(label, done, count)
    return h.hexdigest()


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def new_backup_dir(base):
    base = pathlib.Path(base)
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    for suffix in [''] + [f'-{i}' for i in range(2, 100)]:
        path = base / (stamp + suffix)
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise BackupError('无法创建新的备份目录')


def _write_new(path, data):
    with open(path, 'xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def write_manifest(folder, manifest):
    tmp = folder / (MANIFEST + '.partial')
    with open(tmp, 'w', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, folder / MANIFEST)


def differing_sectors(a, b):
    return [i for i in range(0, len(a), gpt.SECTOR) if a[i:i + gpt.SECTOR] != b[i:i + gpt.SECTOR]]


def compare_reread(reader, start, count, partial, progress=None, label=''):
    """第二遍读取并与第一遍的本地文件逐扇区比较。返回 (第二遍摘要, {绝对扇区: 第二遍数据}, 不同扇区总数)。
    只保存前 MAX_TRACKED 个不同扇区的数据，超出部分只计数。"""
    h = hashlib.sha256()
    differing, total, done = {}, 0, 0
    with open(partial, 'rb') as first:
        while done < count:
            n = min(CHUNK_SECTORS, count - done)
            data = checked_read(reader, start + done, n)
            h.update(data)
            old = first.read(len(data))
            if old != data:
                for offset in differing_sectors(old, data):
                    total += 1
                    if len(differing) < MAX_TRACKED:
                        differing[start + done + offset // gpt.SECTOR] = data[offset:offset + gpt.SECTOR]
            done += n
            if progress:
                progress(label, done, count)
    return h.hexdigest(), differing, total


def write_mismatch_list(folder, filename, differing, total):
    path = folder / (filename + '.mismatch.txt')
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write(f'# 两次读取不同的绝对扇区（共 {total} 个，列出前 {len(differing)} 个）\n')
        for sector in sorted(differing):
            stream.write(f'{sector}\n')
    return path.name


def runs(sectors):
    """把扇区号排序后合并成连续区段 [(起点, 个数)]。"""
    result = []
    for s in sorted(sectors):
        if result and s == result[-1][0] + result[-1][1] and result[-1][1] < CHUNK_SECTORS:
            result[-1][1] += 1
        else:
            result.append([s, 1])
    return [tuple(r) for r in result]


def best_effort_image(reader, folder, partial, filename, start, differing):
    """对两次读取不同的扇区再读第三次，按扇区多数表决，另存 <名>.besteffort.img。
    三次都不同的扇区保留第一次的数据并记为“无多数”。结果始终标记为未验证。"""
    target = folder / (filename + '.besteffort.img')
    shutil.copyfile(partial, target)
    voted_second = undecided = 0
    with open(partial, 'rb') as first, open(target, 'r+b') as out:
        for run_start, run_count in runs(differing):
            third = checked_read(reader, run_start, run_count)
            for i in range(run_count):
                sector = run_start + i
                first.seek((sector - start) * gpt.SECTOR)
                one = first.read(gpt.SECTOR)
                two = differing[sector]
                three = third[i * gpt.SECTOR:(i + 1) * gpt.SECTOR]
                if three == two:
                    out.seek((sector - start) * gpt.SECTOR)
                    out.write(two)
                    voted_second += 1
                elif three != one:
                    undecided += 1
        out.flush()
        os.fsync(out.fileno())
    return {'file': target.name, 'verified': False, 'sha256': file_sha256(target), 'sectors_voted': len(differing),
            'took_second_read': voted_second, 'no_majority': undecided,
            'note': '多数表决合成，未经验证；不能作为写前备份，演练会拒绝'}


def run_backup(reader, folder, names, reread=True, progress=None, device=None, on_disk_sectors=None, log=print,
               vote=False):
    """备份到全新目录 folder。names 为分区名列表，可含 FULL_DISK。失败时保留 .partial 文件和清单供检查。"""
    folder = pathlib.Path(folder)
    if any(folder.iterdir()):
        raise BackupError('备份目录必须是新的空目录，不覆盖已有备份')
    layout, primary, backup = read_layout(reader, on_disk_sectors)
    _write_new(folder / 'gpt-primary.bin', primary)
    _write_new(folder / 'gpt-backup.bin', backup)
    manifest = {'format': 1, 'tool': 'RustSimHub-UsbTool', 'created': datetime.datetime.now().isoformat(timespec='seconds'),
                'device': device or {}, 'layout': layout, 'reread_verified': bool(reread), 'items': [], 'passed': False}
    write_manifest(folder, manifest)
    by_name = {p['name']: p for p in layout['partitions']}
    for name in names:
        if name == FULL_DISK:
            start, count, filename = 0, layout['disk_sectors'], 'full-disk.img'
        else:
            if name not in by_name:
                raise BackupError(f'分区表中没有 {name}')
            part = by_name[name]
            start, count, filename = part['first_sector'], part['sectors'], f'{name}.img'
        item = {'name': name, 'file': filename, 'first_sector': start, 'sectors': count,
                'bytes': count * gpt.SECTOR, 'status': 'reading'}
        manifest['items'].append(item)
        write_manifest(folder, manifest)
        partial = folder / (filename + '.partial')
        log(f'读取 {name}：扇区 {start} 起 {count} 个')
        with open(partial, 'xb') as sink:
            first = hash_range(reader, start, count, sink, progress, f'{name} 读取')
            sink.flush()
            os.fsync(sink.fileno())
        item['sha256'] = first
        if os.path.getsize(partial) != count * gpt.SECTOR:
            item['status'] = 'failed_length'
            write_manifest(folder, manifest)
            raise BackupError(f'{name} 写入本地长度不对')
        if file_sha256(partial) != first:
            item['status'] = 'failed_local_hash'
            write_manifest(folder, manifest)
            raise BackupError(f'{name} 本地文件回读摘要不一致')
        if reread:
            log(f'二次读取 {name} 校验')
            second, differing, total = compare_reread(reader, start, count, partial, progress, f'{name} 回读校验')
            item['reread_sha256'] = second
            if second != first or total:
                item['status'] = 'failed_reread'
                item['mismatch_sectors'] = total
                item['mismatch_list'] = write_mismatch_list(folder, filename, differing, total)
                if vote and differing and total <= MAX_TRACKED:
                    log(f'{name}：{total} 个扇区两次读取不同，第三次读取这些扇区做多数表决')
                    item['best_effort'] = best_effort_image(reader, folder, partial, filename, start, differing)
                write_manifest(folder, manifest)
                hint = '，已另存多数表决的尽力而为镜像（未验证）' if item.get('best_effort') else ''
                raise BackupError(f'{name} 两次从设备读取的内容不一致：{total} 个扇区不同{hint}。'
                                  '可能是存储芯片读不稳，此备份不能作为可信的写前备份')
        if name == FULL_DISK:
            with open(partial, 'rb') as stream:
                if stream.read(len(primary)) != primary:
                    raise BackupError('全盘镜像中的主 GPT 与单独读取的不一致')
                stream.seek(layout['disk_bytes'] - len(backup))
                if stream.read() != backup:
                    raise BackupError('全盘镜像中的备份 GPT 与单独读取的不一致')
        os.replace(partial, folder / filename)
        item['status'] = 'verified'
        write_manifest(folder, manifest)
    manifest['passed'] = True
    write_manifest(folder, manifest)
    return manifest


def verify_backup(folder):
    """离线复核一个备份目录：清单完整、GPT 文件与清单一致、每个镜像长度与摘要一致。返回 (manifest, errors)。"""
    folder = pathlib.Path(folder)
    errors = []
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding='utf-8'))
    except (OSError, ValueError) as err:
        return None, [f'无法读取备份清单：{err}']
    if manifest.get('format') != 1 or manifest.get('passed') is not True:
        errors.append('备份清单未标记为成功完成')
    layout = manifest.get('layout') or {}
    try:
        primary = (folder / 'gpt-primary.bin').read_bytes()
        backup = (folder / 'gpt-backup.bin').read_bytes()
        reparsed = gpt.parse_gpt(primary, backup, layout.get('disk_sectors', 0))
        if reparsed != layout:
            errors.append('GPT 文件与清单记录不一致')
    except (OSError, gpt.GptError) as err:
        errors.append(f'GPT 文件校验失败：{err}')
    for item in manifest.get('items', []):
        path = folder / item.get('file', '')
        if item.get('status') != 'verified':
            errors.append(f'{item.get("name")} 未完成校验（{item.get("status")}）')
            continue
        if not path.is_file() or path.stat().st_size != item.get('bytes'):
            errors.append(f'{item["name"]} 镜像缺失或长度不对')
            continue
        if file_sha256(path) != item.get('sha256'):
            errors.append(f'{item["name"]} 镜像摘要与清单不一致')
    return manifest, errors

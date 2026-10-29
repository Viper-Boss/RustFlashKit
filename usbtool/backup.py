# SPDX-License-Identifier: GPL-3.0-only
"""读分区表、备份分区并校验。只依赖一个 read(start_sector, count) -> bytes 的读取器，便于用模拟磁盘测试。"""
import datetime
import hashlib
import json
import os
import pathlib

from . import gpt

CHUNK_SECTORS = 32768  # 16 MiB 一次
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


def run_backup(reader, folder, names, reread=True, progress=None, device=None, on_disk_sectors=None, log=print):
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
            second = hash_range(reader, start, count, None, progress, f'{name} 回读校验')
            item['reread_sha256'] = second
            if second != first:
                item['status'] = 'failed_reread'
                write_manifest(folder, manifest)
                raise BackupError(f'{name} 两次从设备读取的内容不一致')
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

# SPDX-License-Identifier: GPL-3.0-only
"""Initial A/B installation through the existing guarded Firehose writer.

The first phase freezes a verified full-disk backup and the exact source/target
GPTs. In the second phase, the device and both GPT copies must still match.
Only a fully verified installation gets the final boot ledger.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from . import ab_gpt, backup, board_id, flashpack, gpt, oneclick, paths, storage_layout, writeplan

DISK_MARGIN = 200 << 20


def require(condition, reason):
    if not condition:
        raise oneclick.FlashStopped(reason)


def load_source(folder, disk):
    manifest, errors = backup.verify_backup(folder)
    require(not errors and manifest['layout']['disk_sectors'] == disk and manifest.get('reread_verified') is True,
            '原盘备份未通过离线复核')
    full = [item for item in manifest['items'] if item['name'] == backup.FULL_DISK]
    require(len(full) == 1 and full[0]['bytes'] == disk * gpt.SECTOR, '必须有完整原盘备份')
    folder = Path(folder)
    return manifest, (folder/'gpt-primary.bin').read_bytes(), (folder/'gpt-backup.bin').read_bytes()


def freeze_receipt(path, contents):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(contents, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def partition_step(name, info, layout, pack_dir):
    part = next(p for p in layout['partitions'] if p['name'] == name)
    require(info['bytes'] == part['bytes'], f'{name} 镜像和设备规划大小不一致')
    return {'name': name, 'target': name, 'expected_gpt_sha256': layout['gpt_sha256'],
            'segments': [{'first_sector': part['first_sector'], 'sectors': part['sectors'],
                          'payload': str((Path(pack_dir)/info['file']).resolve()), 'sha256': info['sha256']}]}


def compatible_aboot(read, layout, pin, progress):
    part = next((p for p in layout['partitions'] if p['name'] == 'aboot'), None)
    if part is None:
        return False
    return backup.hash_range(read, part['first_sector'], part['sectors'], None, progress, '核对现有引导程序') == pin


def run_session(pack_dir, manifest, phase, read, ep_out, ep_in, device,
                log, progress, on_disk_sectors, tool_version, retry):
    serial, loader = device.get('serial'), device.get('loader_sha256')
    require(device.get('chip_ok') is True and isinstance(serial, str) and re.fullmatch(r'[0-9a-fA-F]{1,32}', serial)
            and isinstance(loader, str) and flashpack.HEX64.fullmatch(loader), 'A/B 安装需要读到芯片序列号和加载器摘要')
    layout, primary, alternate = backup.read_layout(read, on_disk_sectors)
    disk = layout['disk_sectors']
    board = board_id.identify(read, layout, progress)
    require(board['family'] == 'UFI003', '无法确认 UFI003 型号，未写入')
    require(compatible_aboot(read, layout, manifest['aboot_sha256'], progress), '现有引导程序未通过兼容核对，未写入')
    folder = oneclick.work_dir(pack_dir) / f'ab-{serial.lower()}'
    folder.mkdir(exist_ok=True)
    receipt = folder/'installation.json'
    complete = folder/'complete.json'
    require(not complete.exists(), '这根棒子已通过该包的安装核对，重新初始化须重新规划')
    restarting = receipt.exists()
    pack_sha = writeplan.file_sha256(Path(pack_dir)/'manifest.json')
    result = {'phase': phase, 'layout_state': 'ab', 'board': board, 'written': [], 'skipped': [], 'reports': []}

    if restarting:
        frozen = json.loads(receipt.read_text(encoding='utf-8'))
        require((frozen['pack_sha256'], frozen['serial'], frozen['loader']) == (pack_sha, serial.lower(), loader),
                '此备份属于不同的棒子、加载器或签名包')
        original, source_primary, source_backup = load_source(frozen['backup'], disk)
        saved_device = original['device']
        require(str(saved_device.get('serial', '')).lower() == serial.lower()
                and saved_device.get('loader_sha256') == loader, '原盘备份属于不同设备或加载器')
        require(original['layout']['gpt_sha256'] == frozen['source_gpt'], '备份的来源 GPT 与安装记录不一致')
    else:
        require(phase == 'prepare', '请先完成 A/B 备份及分区准备')
        require(not writeplan.is_original_device_layout(layout), '已登记的原设备布局禁止改写 GPT')
        source_primary, source_backup = primary, alternate

    try:
        new_primary, new_backup, target, profile = ab_gpt.build(source_primary, source_backup, board['family'])
    except (storage_layout.LayoutError, gpt.GptError) as err:
        raise oneclick.FlashStopped(f'无法规划 A/B 分区，未写入：{err}') from None
    require(profile == manifest['profile_gb'], '刷机包容量档和棒子不一致')
    steps = [partition_step(name, manifest['images'][name], target, pack_dir) for name in flashpack.AB_IMAGES]
    if receipt.exists():
        require(frozen['target_gpt'] == target['gpt_sha256'], 'A/B 目标布局与安装记录不一致')
    if phase == 'prepare':
        require(layout['gpt_sha256'] in (gpt.gpt_digest(source_primary, source_backup), target['gpt_sha256']),
                '当前 GPT 和本次安装记录不匹配')
        if not receipt.exists():
            needed = disk * gpt.SECTOR + sum(m['bytes'] for m in manifest['images'].values()) + DISK_MARGIN
            require(shutil.disk_usage(paths.local_dir()).free >= needed, '备份及写前镜像所需磁盘空间不足')
            output = backup.new_backup_dir(paths.backups_dir())
            original = backup.run_backup(read, output, [backup.FULL_DISK], reread=True, device=device,
                                         on_disk_sectors=on_disk_sectors, log=log, progress=progress)
            require(original['passed'] and original['layout']['gpt_sha256'] == layout['gpt_sha256'],
                    '备份期间分区表已变化')
            load_source(output, disk)
            frozen = {'backup': str(output), 'serial': serial.lower(), 'loader': loader, 'pack_sha256': pack_sha,
                      'source_gpt': layout['gpt_sha256'], 'target_gpt': target['gpt_sha256']}
            freeze_receipt(receipt, frozen)
        result['backup'] = frozen['backup']
        if layout['gpt_sha256'] == target['gpt_sha256']:
            result['skipped'].append('gpt')
        else:
            if restarting:
                full = next(item for item in original['items'] if item['name'] == backup.FULL_DISK)
                current = backup.hash_range(read, 0, disk, None, progress, '核对重新接上的原盘')
                require(current == full['sha256'], '设备内容自原盘备份后变化，未写入')
            segments = []
            for name, start, data in (('primary', 0, new_primary), ('backup', disk-gpt.BACKUP_SECTORS, new_backup)):
                payload = folder / f'gpt-{name}.bin'
                payload.write_bytes(data)
                segments.append({'first_sector': start, 'sectors': len(data)//gpt.SECTOR,
                                 'payload': payload.name, 'sha256': hashlib.sha256(data).hexdigest()})
            step = {'name': 'gpt', 'target': 'gpt', 'expected_gpt_sha256': layout['gpt_sha256'],
                    'expected_gpt_after_sha256': target['gpt_sha256'], 'segments': segments}
            plan = oneclick._save_plan(folder, 'ab-gpt', layout, [step], ab_layout=True)
            result['reports'].append(oneclick._run_plan_step(plan, step, read, ep_out, ep_in, gpt.SECTOR,
                                                             log, progress, on_disk_sectors, tool_version, retry))
            result['written'].append('gpt')
        result['done'] = False
        return result

    require(phase == 'images' and layout['gpt_sha256'] == target['gpt_sha256'], '当前 GPT 与冻结的 A/B 布局不符')
    plan = oneclick._save_plan(folder, 'ab-images', target, steps, ab_layout=True)
    # Read back every candidate before requesting a write. Every individual write
    # is checked again by the existing writer and receives a fresh preimage.
    remaining = [s for s in steps if not oneclick.already_written(read, s['segments'][0], progress)]
    need = sum(s['segments'][0]['sectors'] * gpt.SECTOR for s in remaining) + DISK_MARGIN
    require(shutil.disk_usage(paths.local_dir()).free >= need, '写前镜像所需磁盘空间不足')
    for step in steps:
        name = step['name']
        if step not in remaining:
            result['skipped'].append(name)
            continue
        result['reports'].append(oneclick._run_plan_step(plan, step, read, ep_out, ep_in, flashpack.BLOCK,
                                                         log, progress, on_disk_sectors, tool_version, retry))
        result['written'].append(name)
    require(all(oneclick.already_written(read, step['segments'][0], progress) for step in steps),
            'A/B 最终镜像核对失败')
    freeze_receipt(complete, {'target_gpt': target['gpt_sha256'], 'pack_sha256': pack_sha})
    result['done'] = True
    result['manual_reboot'] = True
    return result

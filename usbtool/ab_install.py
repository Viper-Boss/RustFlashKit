# SPDX-License-Identifier: GPL-3.0-only
"""Initial A/B installation through the existing guarded Firehose writer.

The first phase freezes a verified calibration backup and the exact source/target
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
CALIBRATION_NAMES = frozenset({"modemst1", "modemst2", "fsc", "fsg", "sec", "persist"})


def require(condition, reason):
    if not condition:
        raise oneclick.FlashStopped(reason)


def calibration_partitions(layout):
    return {row['name']: row for row in layout['partitions'] if row['name'] in CALIBRATION_NAMES}


def load_source(folder, disk, accept_unstable=()):
    manifest, errors = backup.verify_backup(folder, accept_unstable=accept_unstable)
    require(not errors and manifest['layout']['disk_sectors'] == disk and manifest.get('reread_verified') is True,
            '设备备份未通过离线复核')
    items = manifest['items']
    if len(items) == 1 and items[0]['name'] == backup.FULL_DISK:
        require(items[0]['first_sector'] == 0 and items[0]['sectors'] == disk, '完整原盘备份的范围不一致')
    else:
        required = calibration_partitions(manifest['layout'])
        require(required and len(required) == len(items) and set(required) == {item['name'] for item in items},
                '设备备份缺少基带或校准分区')
        for item in items:
            source = required[item['name']]
            require(all(item[field] == source[field] for field in ('first_sector', 'sectors', 'bytes')),
                    f'备份范围与分区表不符：{item["name"]}')
    folder = Path(folder)
    return manifest, (folder/'gpt-primary.bin').read_bytes(), (folder/'gpt-backup.bin').read_bytes()


def freeze_receipt(path, contents):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(contents, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def partition_step(name, info, layout, pack_dir):
    part = next(p for p in layout['partitions'] if p['name'] == name)
    if name in {'RSH_AUDIO', 'aboot'}:
        require(info['bytes'] <= part['bytes'], f'{name} 镜像大于目标分区')
    else:
        require(info['bytes'] == part['bytes'], f'{name} 镜像和设备规划大小不一致')
    return {'name': name, 'target': name, 'expected_gpt_sha256': layout['gpt_sha256'],
            'segments': [{'first_sector': part['first_sector'], 'sectors': info['bytes'] // gpt.SECTOR,
                          'payload': str((Path(pack_dir)/info['file']).resolve()), 'sha256': info['sha256']}]}


def current_aboot_sha256(read, layout, progress, sectors=None):
    part = next((p for p in layout['partitions'] if p['name'] == 'aboot'), None)
    if part is None:
        return None
    if sectors is None:
        sectors = part['sectors']
    require(0 < sectors <= part['sectors'], '签名指定的引导镜像大于当前分区')
    return backup.hash_range(read, part['first_sector'], sectors, None, progress, '核对现有引导程序')


def run_session(pack_dir, manifest, phase, read, ep_out, ep_in, device,
                log, progress, on_disk_sectors, tool_version, retry):
    serial, loader = device.get('serial'), device.get('loader_sha256')
    require(device.get('chip_ok') is True and isinstance(serial, str) and re.fullmatch(r'[0-9a-fA-F]{1,32}', serial)
            and isinstance(loader, str) and flashpack.HEX64.fullmatch(loader), 'A/B 安装需要读到芯片序列号和加载器摘要')
    layout, primary, alternate = backup.read_layout(read, on_disk_sectors)
    disk = layout['disk_sectors']
    board = board_id.identify(read, layout, progress)
    require(board['family'] == 'UFI003', '无法确认 UFI003 型号，未写入')
    migration = manifest.get('stock_migration') or {}
    accept_unstable = migration.get('unstable_calibration', ())
    if accept_unstable:
        require(serial.lower() == migration['device_serial'], '校准备份例外不适用于该棒子，未写入')
    aboot_sectors = None
    if migration:
        aboot_sectors = manifest['images']['aboot']['bytes'] // gpt.SECTOR
    observed_aboot = current_aboot_sha256(read, layout, progress, aboot_sectors)
    target_aboot = manifest['aboot_sha256']
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
        original, source_primary, source_backup = load_source(frozen['backup'], disk, accept_unstable)
        saved_device = original['device']
        require(str(saved_device.get('serial', '')).lower() == serial.lower()
                and saved_device.get('loader_sha256') == loader, '原盘备份属于不同设备或加载器')
        require(original['layout']['gpt_sha256'] == frozen['source_gpt'], '备份的来源 GPT 与安装记录不一致')
    else:
        require(phase == 'prepare', '请先完成 A/B 备份及分区准备')
        require(not writeplan.is_original_device_layout(layout), '已登记的原设备布局禁止改写 GPT')
        source_primary, source_backup = primary, alternate

    if migration:
        require(observed_aboot is not None and observed_aboot == current_aboot_sha256(read, layout, progress, aboot_sectors),
                '现有引导程序两次读取不一致，未写入')
        require(gpt.gpt_digest(source_primary, source_backup) == migration['source_gpt_sha256'],
                '原厂分区表与签名迁移包不匹配，未写入')
        allowed_aboot = {migration['source_aboot_sha256']}
        if restarting and phase == 'images':
            allowed_aboot.add(target_aboot)
    else:
        allowed_aboot = {target_aboot}
    require(observed_aboot in allowed_aboot, '现有引导程序未通过兼容核对，未写入')

    try:
        new_primary, new_backup, target, profile = ab_gpt.build(source_primary, source_backup, board['family'])
    except (storage_layout.LayoutError, gpt.GptError) as err:
        raise oneclick.FlashStopped(f'无法规划 A/B 分区，未写入：{err}') from None
    require(profile == manifest['profile_gb'], '刷机包容量档和棒子不一致')
    image_names = flashpack.AB_IMAGES
    if migration:
        image_names = (*image_names[:-1], 'aboot', image_names[-1])
    steps = [partition_step(name, manifest['images'][name], target, pack_dir) for name in image_names]
    if receipt.exists():
        require(frozen['target_gpt'] == target['gpt_sha256'], 'A/B 目标布局与安装记录不一致')
    if phase == 'prepare':
        require(layout['gpt_sha256'] in (gpt.gpt_digest(source_primary, source_backup), target['gpt_sha256']),
                '当前 GPT 和本次安装记录不匹配')
        if not receipt.exists():
            calibration = calibration_partitions(layout)
            needed = sum(row['bytes'] for row in calibration.values()) + DISK_MARGIN
            require(shutil.disk_usage(paths.local_dir()).free >= needed, '校准备份所需磁盘空间不足')
            output = backup.new_backup_dir(paths.backups_dir())
            original = backup.run_backup(read, output, list(calibration), reread=True, device=device,
                                         on_disk_sectors=on_disk_sectors, log=log, progress=progress,
                                         accept_unstable=accept_unstable)
            require(original['passed'] and original['layout']['gpt_sha256'] == layout['gpt_sha256'],
                    '备份期间分区表已变化')
            load_source(output, disk, accept_unstable)
            frozen = {'backup': str(output), 'serial': serial.lower(), 'loader': loader, 'pack_sha256': pack_sha,
                      'source_gpt': layout['gpt_sha256'], 'target_gpt': target['gpt_sha256']}
            freeze_receipt(receipt, frozen)
        result['backup'] = frozen['backup']
        if layout['gpt_sha256'] == target['gpt_sha256']:
            result['skipped'].append('gpt')
        else:
            if restarting:
                for item in original['items']:
                    if item['name'] in accept_unstable:
                        continue
                    current = backup.hash_range(read, item['first_sector'], item['sectors'], None, progress,
                                                f'核对备份 {item["name"]}')
                    require(current == item['sha256'], f'{item["name"]} 内容自备份后变化，未写入')
            segments = []
            for name, start, data in (('primary', 0, new_primary), ('backup', disk-gpt.BACKUP_SECTORS, new_backup)):
                payload = folder / f'gpt-{name}.bin'
                payload.write_bytes(data)
                segments.append({'first_sector': start, 'sectors': len(data)//gpt.SECTOR,
                                 'payload': payload.name, 'sha256': hashlib.sha256(data).hexdigest()})
            step = {'name': 'gpt', 'target': 'gpt', 'expected_gpt_sha256': layout['gpt_sha256'],
                    'expected_gpt_after_sha256': target['gpt_sha256'], 'segments': segments}
            plan = oneclick._save_plan(folder, 'ab-gpt', layout, [step], ab_layout=True, aboot_sectors=aboot_sectors, accept_unstable=accept_unstable)
            result['reports'].append(oneclick._run_plan_step(plan, step, read, ep_out, ep_in, gpt.SECTOR,
                                                             log, progress, on_disk_sectors, tool_version, retry))
            result['written'].append('gpt')
        result['done'] = False
        return result

    require(phase == 'images' and layout['gpt_sha256'] == target['gpt_sha256'], '当前 GPT 与冻结的 A/B 布局不符')
    standard_steps = [step for step in steps if step['target'] != 'aboot']
    plan = oneclick._save_plan(folder, 'ab-images', target, standard_steps, ab_layout=True,
                              aboot_sectors=aboot_sectors, accept_unstable=accept_unstable)
    # Read back every candidate before requesting a write. Every individual write
    # is checked again by the existing writer.
    remaining = [s for s in steps if not oneclick.already_written(read, s['segments'][0], progress)]
    for step in steps:
        name = step['name']
        if step not in remaining:
            result['skipped'].append(name)
            continue
        step_plan = plan
        if name == 'aboot':
            step_plan = oneclick._save_plan(folder, 'ab-aboot', target, [step], ab_layout=True,
                                           accept_unstable=accept_unstable)
        result['reports'].append(oneclick._run_plan_step(step_plan, step, read, ep_out, ep_in, flashpack.BLOCK,
                                                         log, progress, on_disk_sectors, tool_version, retry))
        result['written'].append(name)
    require(all(oneclick.already_written(read, step['segments'][0], progress) for step in steps),
            'A/B 最终镜像核对失败')
    freeze_receipt(complete, {'target_gpt': target['gpt_sha256'], 'pack_sha256': pack_sha})
    result['done'] = True
    result['manual_reboot'] = True
    return result

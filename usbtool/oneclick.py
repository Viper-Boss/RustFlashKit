# SPDX-License-Identifier: GPL-3.0-only
"""一键刷机：把原厂 UFI003 刷成 RustSimHub。全部写入仍走 write_runner.run_step 的计划校验、三次回读和保护区比对。

分两个阶段，因为加载器只认连接时那一次 configure（块大小也在那时定下）：
- prepare（块大小 512）：预检并备份 NV 和启动链；原厂布局时写新分区表。
- images（块大小 16 KiB）：依次写 aboot、boot、NSHSTATE、rootfs；已经写对的跳过。全部通过后请求一次正常重启。
两个阶段之间要拔插重新进 9008。不写 tz、sbl1、rpm、hyp 和任何基带/NV/校准分区。
"""
import hashlib
import json
import pathlib

from . import backup, flashpack, gpt, gpt_plan, paths, write_runner, writeplan

PHASES = (('prepare', gpt.SECTOR), ('images', flashpack.BLOCK))
BACKUP_NAMES = ('sbl1', 'sbl1bak', 'aboot', 'abootbak', 'rpm', 'rpmbak', 'tz', 'tzbak', 'hyp', 'hypbak',
                'modem', 'modemst1', 'modemst2', 'fsc', 'fsg', 'sec', 'persist')


class FlashStopped(RuntimeError):
    """停止刷机，已写入的内容不回退。结果留在报告和日志里。"""


def work_dir(pack_dir):
    path = paths.local_dir() / 'flash' / pathlib.Path(pack_dir).name
    path.mkdir(parents=True, exist_ok=True)
    return path


def protected_ranges(layout):
    names = writeplan.MUST_PROTECT | writeplan.NEVER_WRITE | {'tz'}  # 一键刷机不写 tz
    return [{'name': p['name'], 'first_sector': p['first_sector'], 'sectors': p['sectors']}
            for p in layout['partitions'] if p['name'].lower() in names]


def _save_plan(folder, name, layout, steps):
    plan = {'format': 1, 'kind': 'usbtool-write-plan', 'description': name, 'protected': protected_ranges(layout), 'steps': steps}
    path = folder / f'{name}-plan.json'
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    return writeplan.load_plan(path)


def gpt_plan_file(folder, layout, primary, backup_gpt):
    new_primary, new_backup, new_layout = gpt_plan.build(primary, backup_gpt)
    (folder / 'gpt-primary.bin').write_bytes(new_primary)
    (folder / 'gpt-backup.bin').write_bytes(new_backup)
    disk = layout['disk_sectors']
    step = {'name': 'gpt', 'target': 'gpt', 'expected_gpt_sha256': layout['gpt_sha256'],
            'expected_gpt_after_sha256': new_layout['gpt_sha256'],
            'segments': [{'first_sector': 0, 'sectors': gpt.PRIMARY_SECTORS, 'payload': 'gpt-primary.bin',
                          'sha256': hashlib.sha256(new_primary).hexdigest()},
                         {'first_sector': disk - gpt.BACKUP_SECTORS, 'sectors': gpt.BACKUP_SECTORS,
                          'payload': 'gpt-backup.bin', 'sha256': hashlib.sha256(new_backup).hexdigest()}]}
    return _save_plan(folder, 'gpt', layout, [step])


def images_plan_file(folder, pack_dir, manifest, layout):
    by_name = {p['name']: p for p in layout['partitions']}
    steps = []
    for image in flashpack.IMAGES:
        info = manifest['images'][image]
        steps.append({'name': image, 'target': image, 'expected_gpt_sha256': layout['gpt_sha256'],
                      'segments': [{'first_sector': by_name[image]['first_sector'], 'sectors': info['bytes'] // gpt.SECTOR,
                                    'payload': str((pathlib.Path(pack_dir) / info['file']).resolve()),
                                    'sha256': info['sha256']}]})
    return _save_plan(folder, 'images', layout, steps)


def _preflight_backup(folder, read, layout, device, log, progress, on_disk_sectors):
    """每根棒子只备份一次。读不稳或 tz 与 tzbak 不一致时拒绝刷机：这样的棒子写了也靠不住。"""
    serial = device.get('serial') or 'unknown'
    record = folder / f'backup-{serial}.json'
    if record.exists():
        previous = json.loads(record.read_text(encoding='utf-8')).get('folder')
        if previous and pathlib.Path(previous).is_dir() and not backup.verify_backup(previous)[1]:
            log(f'这根棒子已有校验过的备份：{previous}')
            return previous
    names = [n for n in BACKUP_NAMES if n in {p['name'] for p in layout['partitions']}]
    target = backup.new_backup_dir(paths.backups_dir())
    log(f'备份 NV 和启动链（每个分区读两遍）到 {target}')
    try:
        manifest = backup.run_backup(read, target, names, reread=True, progress=progress, device=device,
                                     on_disk_sectors=on_disk_sectors, log=log)
    except backup.BackupError as err:
        raise FlashStopped(f'预检未通过，没有写入任何内容：{err}') from None
    sums = {i['name']: i['sha256'] for i in manifest['items']}
    if 'tz' in sums and 'tzbak' in sums and sums['tz'] != sums['tzbak']:
        raise FlashStopped('预检未通过，没有写入任何内容：tz 与 tzbak 不一致，tz 可能已损坏，这根棒子不支持一键刷机')
    record.write_text(json.dumps({'folder': str(target)}, ensure_ascii=False), encoding='utf-8')
    return str(target)


def _run_plan_step(plan, step, read, ep_out, ep_in, payload_size, log, progress, on_disk_sectors, tool_version, retry):
    digest = writeplan.step_digest(plan, step)
    marker = write_runner.attempts_dir() / f'{digest}.json'
    if marker.exists() and not retry:
        raise FlashStopped(f'{step["name"]} 上次写入没有成功。检查报告后，需要你再次确认才会重试')
    marker.unlink(missing_ok=True)
    report, folder = write_runner.run_step(plan, step['name'], digest, read, ep_out, ep_in, log=log, progress=progress,
                                           on_disk_sectors=on_disk_sectors, tool_version=tool_version,
                                           preconfigured_payload=payload_size)
    if report['status'] != 'verified':
        raise FlashStopped(f'{step["name"]} 写入后核对未通过（{report["status"]}），已停止。报告：{folder}')
    return str(folder)


def already_written(read, segment, progress=None):
    return backup.hash_range(read, segment['first_sector'], segment['sectors'], None, progress,
                             f'核对 {segment["first_sector"]}') == segment['sha256']


def run_session(pack_dir, phase, read, ep_out, ep_in, device, log=print, progress=None, on_disk_sectors=None,
                tool_version='', retry=False):
    """执行一个阶段。返回 {'phase', 'done', 'written', 'skipped', ...}；失败抛 FlashStopped 或写入异常。"""
    manifest = flashpack.check_imported(pack_dir, deep=True)
    if manifest is None:
        raise FlashStopped('刷机包校验失败，请重新导入')
    if device.get('chip_ok') is not True:
        raise FlashStopped('芯片不是已确认的 MSM8916，停止')
    folder = work_dir(pack_dir)
    layout, primary, backup_gpt = backup.read_layout(read, on_disk_sectors)
    state = gpt_plan.classify(layout)
    if state is None:
        raise FlashStopped('这根棒子的分区表既不是支持的原厂布局，也不是 RustSimHub 布局，没有写入任何内容')
    if writeplan.is_original_device_layout(layout):
        raise FlashStopped('这是原设备的分区布局，一键刷机不会动它')
    result = {'phase': phase, 'layout_state': state, 'written': [], 'skipped': [], 'reports': []}
    payload = dict(PHASES)[phase]
    if phase == 'prepare':
        result['backup'] = _preflight_backup(folder, read, layout, device, log, progress, on_disk_sectors)
        if state == 'stock':
            try:
                plan = gpt_plan_file(folder, layout, primary, backup_gpt)
            except gpt_plan.GptPlanError as err:
                raise FlashStopped(f'没有写入任何内容：{err}') from None
            log('写入新分区表')
            result['reports'].append(_run_plan_step(plan, plan['steps'][0], read, ep_out, ep_in, payload, log, progress,
                                                    on_disk_sectors, tool_version, retry))
            result['written'].append('gpt')
        else:
            result['skipped'].append('gpt')
        result['done'] = False
        return result
    if state != 'target':
        raise FlashStopped('分区表还不是 RustSimHub 布局，请从头开始一键刷机')
    plan = images_plan_file(folder, pack_dir, manifest, layout)
    for step in plan['steps']:
        segment = step['segments'][0]
        if already_written(read, segment, progress):
            log(f'{step["name"]} 已是目标内容，跳过')
            result['skipped'].append(step['name'])
            continue
        log(f'写入 {step["name"]}')
        result['reports'].append(_run_plan_step(plan, step, read, ep_out, ep_in, payload, log, progress,
                                                on_disk_sectors, tool_version, retry))
        result['written'].append(step['name'])
    result['done'] = True
    return result

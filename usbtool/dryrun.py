# SPDX-License-Identifier: GPL-3.0-only
"""刷写演练：只核对，不连接设备、不写入。"""
import datetime
import hashlib
import json
import os
import pathlib
import struct

from . import backup, policy, sparse


def _image_checks(partition, path):
    """镜像内容的基础识别，返回 (展开后字节数, 是否 sparse, 提示列表)。"""
    notes = []
    if sparse.is_sparse(path):
        return sparse.expanded_size(path), True, notes
    size = os.path.getsize(path)
    with open(path, 'rb') as stream:
        head = stream.read(1084)
    if partition == 'boot' and head[:8] != b'ANDROID!':
        notes.append('boot 镜像开头不是 ANDROID!，可能不是 Android boot 格式')
    if partition in ('rootfs', 'NSHSTATE') and (len(head) < 1082 or struct.unpack_from('<H', head, 1080)[0] != 0xEF53):
        notes.append(f'{partition} 镜像不是 ext2/3/4 原始镜像（未找到超级块魔数）')
    if size % 512:
        notes.append('镜像长度不是 512 字节整数倍，写入时需要补零到整扇区')
    return size, False, notes


def run_dryrun(backup_dir, writes):
    """writes: [{'partition': 名字, 'image': 路径, 'sha256': 可选期望摘要}]。返回报告字典。"""
    errors, warnings, items = [], [], []
    manifest, backup_errors = backup.verify_backup(backup_dir)
    if manifest is None:
        return {'passed': False, 'errors': backup_errors, 'warnings': [], 'items': [], 'writes_performed': False}
    errors += [f'备份目录：{e}' for e in backup_errors]
    layout = manifest['layout']
    warnings += policy.ufi003_warnings(layout)
    backed_up = {item['name'] for item in manifest.get('items', []) if item.get('status') == 'verified'}
    if backup.FULL_DISK not in backed_up:
        warnings.append('该备份目录没有全盘备份；建议先做一次全盘备份再刷写')
    if not manifest.get('reread_verified'):
        warnings.append('该备份没有做二次回读校验')
    if not writes:
        errors.append('没有选择任何刷写项')
    seen = set()
    by_name = {p['name']: p for p in layout['partitions']}
    for write in writes:
        name, image = write.get('partition', ''), write.get('image', '')
        entry = {'partition': name, 'image': os.path.basename(image)}
        items.append(entry)
        if name in seen:
            errors.append(f'{name} 重复出现在刷写计划中')
        seen.add(name)
        problems = policy.target_errors(name, layout)
        errors += problems
        if problems:
            continue
        part = by_name[name]
        entry.update(first_sector=part['first_sector'], sectors=part['sectors'], partition_bytes=part['bytes'])
        if name not in backed_up:
            errors.append(f'{name} 没有已校验的写前备份（同一备份目录内）')
        if not os.path.isfile(image):
            errors.append(f'{name} 的镜像文件不存在：{image}')
            continue
        try:
            size, is_sparse, notes = _image_checks(name, image)
        except sparse.SparseError as err:
            errors.append(f'{name} 的 sparse 镜像无效：{err}')
            continue
        warnings += [f'{name}：{n}' for n in notes]
        digest = backup.file_sha256(image)
        entry.update(image_sha256=digest, image_bytes=os.path.getsize(image), expanded_bytes=size, sparse=is_sparse)
        expected = (write.get('sha256') or '').strip().lower()
        if expected and expected != digest:
            errors.append(f'{name} 镜像摘要与期望值不一致')
        if size > part['bytes']:
            errors.append(f'{name} 镜像（展开后 {size} 字节）大于分区（{part["bytes"]} 字节）')
        if size == 0:
            errors.append(f'{name} 镜像为空')
    plan = {'gpt_sha256': layout['gpt_sha256'], 'writes': items}
    plan_sha = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    return {'format': 1, 'created': datetime.datetime.now().isoformat(timespec='seconds'),
            'mode': 'dry-run', 'writes_performed': False, 'passed': not errors,
            'backup_dir': str(backup_dir), 'gpt_sha256': layout['gpt_sha256'], 'plan_sha256': plan_sha,
            'errors': errors, 'warnings': warnings, 'items': items}


def save_report(report, base):
    folder = backup.new_backup_dir(pathlib.Path(base))
    path = folder / 'dryrun-report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def format_report(report):
    lines = ['演练结果：' + ('通过（未写入设备）' if report['passed'] else '未通过（未写入设备）')]
    if report.get('plan_sha256'):
        lines.append(f'计划摘要：{report["plan_sha256"]}')
    for item in report.get('items', []):
        if 'image_sha256' in item:
            lines.append(f'  {item["partition"]} ← {item["image"]}  展开 {item["expanded_bytes"]} / 分区 {item["partition_bytes"]} 字节'
                         f'  sha256 {item["image_sha256"][:16]}…')
    lines += ['错误：' + e for e in report.get('errors', [])]
    lines += ['提示：' + w for w in report.get('warnings', [])]
    return '\n'.join(lines)

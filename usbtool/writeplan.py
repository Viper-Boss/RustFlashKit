# SPDX-License-Identifier: GPL-3.0-only
"""受限写入计划：加载、校验、步骤摘要。不打开 USB。

规则（硬编码，计划文件不能放宽）：
- 只能写计划里点名的一步；目标只能是 PLAN_TARGETS 之一。
- 基带/NV/校准及其余启动链分区（NEVER_WRITE）永远不能作为目标，重写 GPT 时它们的 128 字节表项必须原样保留。
- tz、aboot、GPT 这类启动链写入只允许在“不是原设备布局”的设备上进行（原设备布局见 policy.UFI003_LAYOUT）。
- 每一步都绑定写入前设备 GPT 的摘要；GPT 步骤还绑定写入后的新摘要。
- 唯一例外是“刷新”目标 modem（用户 2026-11-09 明确批准，用来修复读不稳的基带固件区）：只能整分区重写，
  载荷与设备当前内容最多相差 REFRESH_MAX_DIFF 个扇区（也就是把原数据原样写回，只纠正读不稳的位置），
  不能在原设备布局上做。NV/校准分区（fsg、modemst1、modemst2、fsc、sec、persist）仍然永远禁写。
"""
import hashlib
import json
import os
import re

from . import gpt, policy

PLAN_TARGETS = frozenset({'tz', 'aboot', 'boot', 'NSHSTATE', 'rootfs', 'gpt', 'modem'})
REFRESH_TARGETS = frozenset({'modem'})
REFRESH_MAX_DIFF = 64
BOOT_CHAIN = frozenset({'tz', 'aboot', 'gpt'})
NEVER_WRITE = frozenset(policy.DENIED_NAMES - {'tz', 'aboot'})  # 小写比较
MUST_PROTECT = frozenset({'modem', 'fsg', 'modemst1', 'modemst2', 'fsc', 'sec', 'persist'})
HEX64 = re.compile(r'^[0-9a-f]{64}$')
STEP_NAME = re.compile(r'^[A-Za-z0-9_-]{1,32}$')


class PlanError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise PlanError(message)


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(4 << 20), b''):
            h.update(block)
    return h.hexdigest()


def load_plan(path):
    path = os.path.abspath(path)
    with open(path, 'rb') as stream:
        raw = stream.read()
    _require(len(raw) <= 1 << 20, '计划文件过大')
    plan = json.loads(raw.decode('utf-8'))
    _require(isinstance(plan, dict) and plan.get('format') == 1 and plan.get('kind') == 'usbtool-write-plan',
             '不是受支持的写入计划（需要 format=1、kind=usbtool-write-plan）')
    _require(set(plan) <= {'format', 'kind', 'description', 'protected', 'steps'}, '计划含有未知字段')
    _require(isinstance(plan.get('steps'), list) and plan['steps'], '计划没有步骤')
    _require(isinstance(plan.get('protected'), list) and plan['protected'], '计划没有保护区段')
    names = set()
    for step in plan['steps']:
        _require(isinstance(step, dict) and set(step) <= {'name', 'target', 'expected_gpt_sha256',
                                                          'expected_gpt_after_sha256', 'segments'}, '步骤含有未知字段')
        _require(isinstance(step.get('name'), str) and STEP_NAME.match(step['name']) and step['name'] not in names,
                 '步骤名无效或重复')
        names.add(step['name'])
        _require(step.get('target') in PLAN_TARGETS, f'步骤 {step["name"]} 的目标不在允许名单 {sorted(PLAN_TARGETS)} 中')
        _require(isinstance(step.get('expected_gpt_sha256'), str) and HEX64.match(step['expected_gpt_sha256']),
                 f'步骤 {step["name"]} 缺少写入前 GPT 摘要')
        segments = step.get('segments')
        _require(isinstance(segments, list) and segments, f'步骤 {step["name"]} 没有写入段')
        for seg in segments:
            _require(isinstance(seg, dict) and set(seg) == {'first_sector', 'sectors', 'payload', 'sha256'},
                     '写入段字段必须正好是 first_sector、sectors、payload、sha256')
            _require(_int(seg['first_sector']) and seg['first_sector'] >= 0 and _int(seg['sectors']) and seg['sectors'] > 0,
                     '写入段扇区无效')
            _require(isinstance(seg['payload'], str) and isinstance(seg['sha256'], str) and HEX64.match(seg['sha256']),
                     '写入段载荷或摘要无效')
    for row in plan['protected']:
        _require(isinstance(row, dict) and set(row) == {'name', 'first_sector', 'sectors'} and isinstance(row['name'], str)
                 and _int(row['first_sector']) and row['first_sector'] >= 0 and _int(row['sectors']) and row['sectors'] > 0,
                 '保护区段字段必须正好是 name、first_sector、sectors')
    plan['_path'] = path
    plan['_sha256'] = hashlib.sha256(raw).hexdigest()
    return plan


def find_step(plan, name):
    matches = [s for s in plan['steps'] if s['name'] == name]
    _require(len(matches) == 1, f'计划中没有步骤 {name}')
    return matches[0]


def step_digest(plan, step):
    """计划文件摘要 + 步骤内容 + 各载荷摘要；用户批准时核对它，真实写入要求 --confirm 等于它。"""
    body = json.dumps({'plan_sha256': plan['_sha256'], 'step': step}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def payload_path(plan, seg):
    path = seg['payload']
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(plan['_path']), path)
    return os.path.abspath(path)


def overlaps(a_first, a_count, b_first, b_count):
    return a_first < b_first + b_count and b_first < a_first + a_count


def is_original_device_layout(layout):
    return not policy.ufi003_warnings(layout)


def gpt_entries_by_name(primary):
    """从主 GPT 取出 {名字: 128 字节原始表项}。"""
    header = gpt.parse_header(primary[gpt.SECTOR:2 * gpt.SECTOR], 1)
    rows = {}
    for i in range(header['count']):
        row = primary[2 * gpt.SECTOR + i * 128:2 * gpt.SECTOR + (i + 1) * 128]
        if row[:16] == bytes(16):
            continue
        rows[row[56:128].decode('utf-16-le').split('\0', 1)[0]] = bytes(row)
    return rows


def validate_step(plan, step, layout, current_primary=None, check_payloads=True):
    """对照设备当前（或备份中的）布局校验一步。返回 {'segments': [...], 'protected': [...], 'new_layout': ...}。"""
    target = step['target']
    _require(layout['gpt_sha256'] == step['expected_gpt_sha256'],
             f'设备当前 GPT 摘要与步骤 {step["name"]} 绑定的不一致，停止（可能是别的设备或上一步状态不对）')
    if target in BOOT_CHAIN or target in REFRESH_TARGETS:
        _require(not is_original_device_layout(layout), '这台设备是原设备的分区布局，启动链（tz/aboot/GPT）和 modem 写入一律拒绝')
    by_name = {}
    for p in layout['partitions']:
        by_name.setdefault(p['name'], []).append(p)
    disk = layout['disk_sectors']

    # 保护区：计划给出的 + 工具从当前布局自己算出的禁写分区，取并集
    protected = [dict(row) for row in plan['protected']]
    for row in protected:
        _require(row['first_sector'] + row['sectors'] <= disk, f'保护区段 {row["name"]} 超出磁盘')
    present = {p['name'].lower() for p in layout['partitions']}
    covered = {row['name'].lower() for row in protected}
    refreshing = {target.lower()} if target in REFRESH_TARGETS else set()
    missing = sorted((MUST_PROTECT & present) - covered - refreshing)
    _require(not missing, f'计划的保护区段缺少：{", ".join(missing)}')
    listed = {(row['first_sector'], row['sectors']) for row in protected}
    for p in layout['partitions']:
        if p['name'].lower() in refreshing:
            continue
        if p['name'].lower() in NEVER_WRITE and (p['first_sector'], p['sectors']) not in listed:
            protected.append({'name': p['name'], 'first_sector': p['first_sector'], 'sectors': p['sectors'], 'implicit': True})

    segments = step['segments']
    if target == 'gpt':
        _require(len(segments) == 2 and (segments[0]['first_sector'], segments[0]['sectors']) == (0, gpt.PRIMARY_SECTORS)
                 and (segments[1]['first_sector'], segments[1]['sectors']) == (disk - gpt.BACKUP_SECTORS, gpt.BACKUP_SECTORS),
                 'GPT 步骤必须正好是主 GPT（扇区 0 起 34 个）和备份 GPT（最后 33 个扇区）两段')
        _require(isinstance(step.get('expected_gpt_after_sha256'), str) and HEX64.match(step['expected_gpt_after_sha256']),
                 'GPT 步骤必须给出写入后的新 GPT 摘要')
    elif target in REFRESH_TARGETS:
        _require(len(by_name.get(target, [])) == 1, f'当前分区表中 {target} 出现 {len(by_name.get(target, []))} 次，需要恰好 1 次')
        part = by_name[target][0]
        _require(len(segments) == 1 and (segments[0]['first_sector'], segments[0]['sectors']) == (part['first_sector'], part['sectors']),
                 f'{target} 刷新必须整分区重写（扇区 {part["first_sector"]} 起 {part["sectors"]} 个）')
        _require('expected_gpt_after_sha256' not in step, '只有 GPT 步骤可以改变分区表')
    else:
        _require(target.lower() not in NEVER_WRITE, f'{target} 永远禁写')
        _require(len(by_name.get(target, [])) == 1, f'当前分区表中 {target} 出现 {len(by_name.get(target, []))} 次，需要恰好 1 次')
        part = by_name[target][0]
        _require(len(segments) == 1 and segments[0]['first_sector'] == part['first_sector']
                 and segments[0]['sectors'] <= part['sectors'],
                 f'{target} 的写入段必须从分区起点开始且不超过分区（{part["sectors"]} 扇区）')
        _require('expected_gpt_after_sha256' not in step, '只有 GPT 步骤可以改变分区表')
    for seg in segments:
        _require(seg['first_sector'] + seg['sectors'] <= disk, '写入段超出磁盘')
        for row in protected:
            _require(not overlaps(seg['first_sector'], seg['sectors'], row['first_sector'], row['sectors']),
                     f'写入段与保护区段 {row["name"]} 重叠')

    payloads = []
    for seg in segments:
        path = payload_path(plan, seg)
        info = {**seg, 'path': path}
        if check_payloads:
            _require(os.path.isfile(path), f'载荷不存在：{seg["payload"]}')
            _require(os.path.getsize(path) == seg['sectors'] * gpt.SECTOR,
                     f'载荷 {seg["payload"]} 长度必须正好是 {seg["sectors"]}×512 字节')
            _require(file_sha256(path) == seg['sha256'], f'载荷 {seg["payload"]} 的 SHA256 与计划不一致')
        payloads.append(info)

    new_layout = None
    if target == 'gpt' and check_payloads:
        primary = open(payloads[0]['path'], 'rb').read()
        backup_part = open(payloads[1]['path'], 'rb').read()
        try:
            new_layout = gpt.parse_gpt(primary, backup_part, disk)
        except gpt.GptError as err:
            raise PlanError(f'新 GPT 无效：{err}') from None
        _require(new_layout['gpt_sha256'] == step['expected_gpt_after_sha256'], '新 GPT 的摘要与计划给出的写入后摘要不一致')
        if current_primary is not None:
            old_rows, new_rows = gpt_entries_by_name(current_primary), gpt_entries_by_name(primary)
            for name, row in old_rows.items():
                if name.lower() in NEVER_WRITE or name.lower() in MUST_PROTECT:
                    _require(new_rows.get(name) == row, f'新 GPT 改动了必须原样保留的表项：{name}')
        _require(not is_original_device_layout(new_layout), '新 GPT 与原设备布局相同，拒绝')
    return {'segments': payloads, 'protected': protected, 'new_layout': new_layout}


def dry_run(plan, step_name, layout, current_primary=None):
    """离线核对：返回报告字典（不抛异常）。"""
    report = {'mode': 'write-dryrun', 'writes_performed': False, 'plan_sha256': plan['_sha256'], 'step': step_name,
              'passed': False, 'errors': []}
    try:
        step = find_step(plan, step_name)
        checked = validate_step(plan, step, layout, current_primary)
        report.update(passed=True, step_digest=step_digest(plan, step), target=step['target'],
                      segments=[{k: s[k] for k in ('first_sector', 'sectors', 'sha256', 'payload')} for s in checked['segments']],
                      protected=len(checked['protected']))
    except (PlanError, OSError, ValueError) as err:
        report['errors'].append(str(err))
    return report



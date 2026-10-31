# SPDX-License-Identifier: GPL-3.0-only
"""执行受限写入计划中的一步。读取一律走只读护栏下的 reader；写入只走 firehose_write.WriteSession。

结果状态：
- verified：三次回读都等于载荷，读得稳的保护扇区全部未变，GPT 摘要符合预期；
- written_unprovable：写入与回读都对，但有保护扇区本来就读不稳，无法证明它们未变；
- readback_mismatch / protected_changed / gpt_mismatch：写入后核对失败；
- write_failed：写入过程中失败（可能已部分写入）。
任何非 verified 结果都不自动重试、不重启设备；尝试标记保留，同一步不能被自动再次执行。
"""
import datetime
import hashlib
import json
import os
import pathlib

from . import backup, gpt, paths, writeplan
from .firehose_write import WriteFailed, WriteSession

READBACK_PASSES = 3


class WriteRefused(RuntimeError):
    pass


def sector_digests(reader, first, count, progress=None, label=''):
    """读一段，返回 (整段 SHA256, 每扇区 SHA1 列表)。"""
    whole = hashlib.sha256()
    per = []
    done = 0
    while done < count:
        n = min(backup.CHUNK_SECTORS, count - done)
        data = backup.checked_read(reader, first + done, n)
        whole.update(data)
        per.extend(hashlib.sha1(data[i:i + gpt.SECTOR]).digest() for i in range(0, len(data), gpt.SECTOR))
        done += n
        if progress:
            progress(label, done, count)
    return whole.hexdigest(), per


KEEP_READBACK_BYTES = 16 << 20  # 不超过这个大小的段，另存每次回读的原始数据，供离线分析


def readback_against_payload(reader, seg, folder, progress=None):
    """回读 READBACK_PASSES 次，每次逐扇区与载荷文件比较。
    返回每次的摘要、不符扇区数，以及“每个扇区是否多数（≥2 次）等于载荷”。"""
    keep = seg['sectors'] * gpt.SECTOR <= KEEP_READBACK_BYTES
    bad_counts = {}
    passes, mismatched = [], []
    for k in range(READBACK_PASSES):
        h = hashlib.sha256()
        bad = 0
        sink = open(folder / f'readback-{seg["first_sector"]}-{k + 1}.bin', 'xb') if keep else None
        try:
            with open(seg['path'], 'rb') as payload:
                done = 0
                while done < seg['sectors']:
                    n = min(backup.CHUNK_SECTORS, seg['sectors'] - done)
                    data = backup.checked_read(reader, seg['first_sector'] + done, n)
                    h.update(data)
                    if sink:
                        sink.write(data)
                    want = payload.read(len(data))
                    if data != want:
                        for offset in backup.differing_sectors(data, want):
                            sector = seg['first_sector'] + done + offset // gpt.SECTOR
                            bad_counts[sector] = bad_counts.get(sector, 0) + 1
                            bad += 1
                    done += n
                    if progress:
                        progress(f'回读 {seg["first_sector"]} 第 {k + 1} 次', done, seg['sectors'])
        finally:
            if sink:
                sink.close()
        passes.append(h.hexdigest())
        mismatched.append(bad)
    minority = sorted(s for s, c in bad_counts.items() if c * 2 > READBACK_PASSES)
    return {'first_sector': seg['first_sector'], 'expected': seg['sha256'], 'passes': passes,
            'mismatched_sectors_per_pass': mismatched, 'sectors_ever_mismatched': len(bad_counts),
            'sectors_majority_wrong': len(minority), 'majority_wrong_list': minority[:200],
            'ok': all(p == seg['sha256'] for p in passes), 'majority_ok': not minority,
            'raw_reads_saved': keep}


def compare_protected(before1, before2, after):
    """逐扇区：读得稳且变了 -> changed；本来不稳且与两次写前读数都不同 -> unprovable。"""
    changed = unprovable = unstable = 0
    for a, b, c in zip(before1, before2, after):
        if a != b:
            unstable += 1
            if c != a and c != b:
                unprovable += 1
        elif c != a:
            changed += 1
    return {'changed_sectors': changed, 'unstable_sectors': unstable, 'unprovable_sectors': unprovable}


def attempts_dir():
    path = paths.local_dir() / 'write-attempts'
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_output_dir(step_name):
    base = paths.local_dir() / 'writes'
    folder = backup.new_backup_dir(base)
    marker = folder / 'step.txt'
    marker.write_text(step_name, encoding='utf-8')
    return folder


def write_report(folder, report):
    tmp = folder / 'report.json.partial'
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, folder / 'report.json')


def run_step(plan, step_name, confirm, reader, ep_out, ep_in, log=print, progress=None, on_disk_sectors=None,
             tool_version=''):
    step = writeplan.find_step(plan, step_name)
    digest = writeplan.step_digest(plan, step)
    if not (isinstance(confirm, str) and len(confirm) >= 16 and digest.startswith(confirm.lower())):
        raise WriteRefused(f'--confirm 与步骤摘要不符。该步骤摘要：{digest}（需要用户核对后批准）')
    folder = new_output_dir(step_name)
    report = {'format': 1, 'mode': 'write', 'created': datetime.datetime.now().isoformat(timespec='seconds'),
              'tool_version': tool_version, 'plan_sha256': plan['_sha256'], 'step': step_name, 'step_digest': digest,
              'target': step['target'], 'status': 'preflight', 'passed': False, 'reset_sent': False}
    write_report(folder, report)
    log(f'输出目录：{folder}')

    # 1. 当前 GPT 与计划绑定核对
    layout, primary, backup_gpt = backup.read_layout(reader, on_disk_sectors)
    (folder / 'gpt-before-primary.bin').write_bytes(primary)
    (folder / 'gpt-before-backup.bin').write_bytes(backup_gpt)
    report['gpt_before_sha256'] = layout['gpt_sha256']
    checked = writeplan.validate_step(plan, step, layout, primary)
    segments = checked['segments']
    protected = checked['protected']
    log(f'计划核对通过：{len(segments)} 段，{len(protected)} 个保护区段')

    # 2. 写前读两遍保护区（判断哪些扇区读得稳）
    pre = []
    for row in protected:
        one = sector_digests(reader, row['first_sector'], row['sectors'], progress, f'保护区 {row["name"]} 写前读 1')
        two = sector_digests(reader, row['first_sector'], row['sectors'], progress, f'保护区 {row["name"]} 写前读 2')
        pre.append((one, two))
    report['protected_before'] = [{'name': r['name'], 'first_sector': r['first_sector'], 'sectors': r['sectors'],
                                   'sha256_read1': a[0], 'sha256_read2': b[0], 'stable': a[0] == b[0]}
                                  for r, (a, b) in zip(protected, pre)]

    # 3. 目标区前像（回退用证据）
    report['preimage'] = []
    for i, seg in enumerate(segments):
        path = folder / f'preimage-{i}-{seg["first_sector"]}.bin'
        with open(path, 'xb') as sink:
            sha = backup.hash_range(reader, seg['first_sector'], seg['sectors'], sink, progress, f'前像 {seg["first_sector"]}')
        report['preimage'].append({'file': path.name, 'first_sector': seg['first_sector'], 'sectors': seg['sectors'],
                                   'sha256': sha})
    report['status'] = 'preflight_done'
    write_report(folder, report)

    # 4. 消耗尝试：同一步不能被自动再次执行
    marker = attempts_dir() / f'{digest}.json'
    try:
        with open(marker, 'x', encoding='utf-8') as stream:
            json.dump({'step': step_name, 'output': str(folder), 'created': report['created']}, stream)
    except FileExistsError:
        report['status'] = 'refused_attempt_used'
        write_report(folder, report)
        raise WriteRefused(f'这一步已经尝试过（{marker}）。不会自动重试；检查上次结果后，如确需再试，'
                           '由人删除该标记并重新批准。') from None
    report['attempt_marker'] = str(marker)

    # 5. 写入
    report['status'] = 'writing'
    write_report(folder, report)
    session = WriteSession(ep_out, ep_in, segments, log=log, progress=progress)
    try:
        session.configure()
        for seg in segments:
            log(f'写入扇区 {seg["first_sector"]} 起 {seg["sectors"]} 个')
            with open(seg['path'], 'rb') as stream:
                session.program_next(stream)
    except WriteFailed as err:
        report.update(status='write_failed', failed_phase=err.phase, error=str(err), transcript=session.transcript)
        write_report(folder, report)
        raise
    report['transcript'] = session.transcript

    # 6. 目标区回读三次
    report['status'] = 'verifying'
    write_report(folder, report)
    readback = [readback_against_payload(reader, seg, folder, progress) for seg in segments]
    report['readback'] = readback

    # 7. 保护区写后核对
    results = []
    for row, ((_s1, d1), (_s2, d2)) in zip(protected, pre):
        _sha, after = sector_digests(reader, row['first_sector'], row['sectors'], progress, f'保护区 {row["name"]} 写后读')
        changed = [row['first_sector'] + i for i, (a, b, c) in enumerate(zip(d1, d2, after)) if a == b and c != a]
        results.append({'name': row['name'], **compare_protected(d1, d2, after), 'changed_list': changed[:200]})
    report['protected_after'] = results

    # 8. GPT 核对
    new_layout, _p, _b = backup.read_layout(reader)
    report['gpt_after_sha256'] = new_layout['gpt_sha256']
    expected_gpt = step.get('expected_gpt_after_sha256') or step['expected_gpt_sha256']

    if not all(r['majority_ok'] for r in readback):
        report['status'] = 'readback_mismatch'
    elif any(r['changed_sectors'] for r in results):
        report['status'] = 'protected_changed'
    elif new_layout['gpt_sha256'] != expected_gpt:
        report['status'] = 'gpt_mismatch'
    elif not all(r['ok'] for r in readback):
        report['status'] = 'written_majority'  # 每个扇区多数回读等于载荷，但有读数在跳；不算通过
    elif any(r['unprovable_sectors'] for r in results):
        report['status'] = 'written_unprovable'
    else:
        report['status'] = 'verified'
        report['passed'] = True
    write_report(folder, report)
    log(f'结果：{report["status"]}（未自动重启设备）')
    return report, folder


def dry_run_plan(plan, backup_dir, only_step=None):
    """离线演练：用备份目录里的 GPT 作为“当前设备”，按顺序核对各步；GPT 步骤之后改用它的新表。"""
    folder = pathlib.Path(backup_dir)
    primary = (folder / 'gpt-primary.bin').read_bytes()
    backup_part = (folder / 'gpt-backup.bin').read_bytes()
    layout = gpt.parse_gpt(primary, backup_part, gpt.disk_sectors_from_primary(primary))
    reports = []
    for step in plan['steps']:
        if only_step is None or step['name'] == only_step:
            reports.append(writeplan.dry_run(plan, step['name'], layout, primary))
        if step['target'] == 'gpt':
            try:
                checked = writeplan.validate_step(plan, step, layout, primary)
            except (writeplan.PlanError, OSError):
                break
            seg0, seg1 = checked['segments']
            primary = open(seg0['path'], 'rb').read()
            layout = checked['new_layout']
    if only_step is not None and not reports:
        reports.append({'step': only_step, 'passed': False, 'errors': [f'计划中没有步骤 {only_step}'], 'writes_performed': False})
    return reports

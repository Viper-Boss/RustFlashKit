# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import json
import xml.etree.ElementTree as ET

import pytest

from conftest import FakeDisk
from usbtool import backup, cli, firehose_write, gpt, policy, write_runner, writeplan, worker

SECTORS = 8192
STICK = [('sbl1', 34, 99), ('modem', 100, 1099), ('fsg', 1100, 1199), ('modemst1', 1200, 1299),
         ('modemst2', 1300, 1399), ('fsc', 1400, 1409), ('tz', 1410, 1509), ('aboot', 1510, 1609),
         ('boot', 1610, 1865), ('persist', 1866, 1965), ('rootfs', 1966, 6999), ('NSHSTATE', 7000, 8100)]
PROTECTED = [{'name': n, 'first_sector': a, 'sectors': b - a + 1} for n, a, b in STICK
             if n in ('modem', 'fsg', 'modemst1', 'modemst2', 'fsc', 'persist')]


class Timeout(Exception):
    def __str__(self):
        return 'Operation timed out'


class FakeTarget:
    """模拟 Firehose 加载器：configure / program / 原始数据 / ACK，并把数据真正写进 FakeDisk。"""

    def __init__(self, disk):
        self.disk = disk
        self.queue = []
        self.raw = None
        self.writes_enabled = False
        self.fault = None
        self.max_payload = None
        self.configures = 0
        self.pending_zlp = False
        self.bad_stream = False
        self.out = self.Out(self)
        self.inp = self.In(self)

    def reply(self, value='ACK', rawmode=None, log=None):
        body = f'<log value="{log}" />' if log else ''
        attr = f' rawmode="{rawmode}"' if rawmode else ''
        self.queue.append(f'<?xml version="1.0" ?><data>{body}<response value="{value}"{attr} /></data>'.encode())

    class Out:
        def __init__(self, t):
            self.t = t

        def write(self, data, timeout=None):
            t = self.t
            if t.raw is None:
                if not data:
                    return 0
                cmd = ET.fromstring(data)[0]
                if cmd.tag == 'configure':
                    t.configures += 1
                    if t.configures == 1:  # 模拟真机（2026-11-01）：加载器只认第一次 configure，之后的照样回 ACK
                        t.writes_enabled = cmd.get('SkipWrite') == '0'
                        t.max_payload = int(cmd.get('MaxPayloadSizeToTargetInBytes'))
                    t.reply('ACK')
                elif cmd.tag == 'program':
                    if t.fault == 'nak_program':
                        t.reply('NAK', log='ACK would be nice')  # 日志里的 ACK 不能被当成 ACK
                        return len(data)
                    if t.fault == 'silent':
                        return len(data)
                    t.raw = [int(cmd.get('start_sector')), int(cmd.get('num_partition_sectors')), bytearray()]
                    t.pending_zlp = t.bad_stream = False
                    t.reply('ACK', 'false' if t.fault == 'no_rawmode' else 'true')
                return len(data)
            if not data:
                t.pending_zlp = True
                return 0
            if t.fault == 'short_write':
                return len(data) - 512
            start, count, buf = t.raw
            # 模拟真机现象（2026-10-30）：加载器每次请求 min(MaxPayload, 剩余)；上一块后的零长度包会被下一次请求读走。
            # 请求等于 MaxPayload 时无害（tz 步骤实测正常），请求小于 MaxPayload 时读到 0 字节 -> 出错、不写入。
            request = min(t.max_payload, count * 512 - len(buf))
            if t.pending_zlp and request < t.max_payload:
                t.bad_stream = True
            t.pending_zlp = False
            if len(data) != request:
                t.bad_stream = True
            buf.extend(data)
            if len(buf) >= count * 512:
                if t.bad_stream:
                    t.raw = None
                    t.reply('NAK', 'false', log='Read back verify failed at sector 1703271967,num sectors 0')
                    return len(data)
                if t.fault == 'corrupt':
                    buf[100] ^= 1
                if t.writes_enabled:
                    t.disk.data[start * 512:(start + count) * 512] = buf
                if t.fault == 'collateral':
                    t.disk.data[150 * 512] ^= 0xFF  # 意外改动 modem 区域
                t.raw = None
                t.reply('NAK' if t.fault == 'final_nak' else 'ACK', 'false')
            return len(data)

    class In:
        def __init__(self, t):
            self.t = t

        def read(self, size, timeout=None):
            if not self.t.queue:
                raise Timeout()
            return self.t.queue.pop(0)


@pytest.fixture
def stick():
    return FakeDisk(SECTORS, STICK, seed=3)


def current_gpt(disk):
    primary = disk.range(0, 33)
    back = disk.range(SECTORS - 33, SECTORS - 1)
    return gpt.parse_gpt(primary, back, SECTORS), primary


def make_plan(tmp_path, disk, steps):
    layout, _ = current_gpt(disk)
    plan = {'format': 1, 'kind': 'usbtool-write-plan', 'protected': PROTECTED, 'steps': []}
    for name, target, first, payload in steps:
        path = tmp_path / f'{name}.bin'
        path.write_bytes(payload)
        plan['steps'].append({'name': name, 'target': target, 'expected_gpt_sha256': layout['gpt_sha256'],
                              'segments': [{'first_sector': first, 'sectors': len(payload) // 512, 'payload': path.name,
                                            'sha256': hashlib.sha256(payload).hexdigest()}]})
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(plan), encoding='utf-8')
    return path


def tz_payload():
    return bytes(range(256)) * 2 * 100  # 100 扇区


def run(plan_path, disk, step='tz', fault=None, confirm=None):
    plan = writeplan.load_plan(plan_path)
    target = FakeTarget(disk)
    target.fault = fault
    digest = writeplan.step_digest(plan, writeplan.find_step(plan, step))
    return write_runner.run_step(plan, step, confirm or digest, disk.read, target.out, target.inp, log=lambda m: None)


def test_tz_step_written_and_verified(stick, tmp_path):
    plan = make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())])
    before_modem = stick.range(100, 1099)
    report, folder = run(plan, stick)
    assert report['status'] == 'verified' and report['passed'] and not report['reset_sent']
    assert stick.range(1410, 1509) == tz_payload() and stick.range(100, 1099) == before_modem
    assert all(r['changed_sectors'] == 0 for r in report['protected_after'])
    assert (folder / report['preimage'][0]['file']).stat().st_size == 100 * 512
    with pytest.raises(write_runner.WriteRefused, match='已经尝试过'):
        run(plan, stick)  # 同一步不能自动再执行


def test_wrong_confirm_refused_before_touching_device(stick, tmp_path):
    plan = make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())])
    reads = len(stick.reads)
    with pytest.raises(write_runner.WriteRefused, match='步骤摘要'):
        run(plan, stick, confirm='0' * 16)
    assert len(stick.reads) == reads


def test_gpt_binding_mismatch_refused(stick, tmp_path):
    plan_path = make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())])
    data = json.loads(plan_path.read_text(encoding='utf-8'))
    data['steps'][0]['expected_gpt_sha256'] = 'ab' * 32
    plan_path.write_text(json.dumps(data), encoding='utf-8')
    before = bytes(stick.data)
    with pytest.raises(writeplan.PlanError, match='GPT 摘要'):
        run(plan_path, stick)
    assert bytes(stick.data) == before


def test_original_device_layout_refuses_boot_chain(stick, tmp_path, monkeypatch):
    layout, _ = current_gpt(stick)
    by = {p['name']: p for p in layout['partitions']}
    monkeypatch.setattr(policy, 'UFI003_LAYOUT', {by[n]['index']: (n, by[n]['bytes']) for n in ('boot', 'rootfs', 'NSHSTATE')})
    plan = make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())])
    with pytest.raises(writeplan.PlanError, match='原设备'):
        run(plan, stick)


@pytest.mark.parametrize('change,match', [
    (lambda p: p['steps'][0].update(target='sbl1'), '允许名单'),
    (lambda p: p['steps'][0].update(target='modem'), '允许名单'),
    (lambda p: p.update(protected=[r for r in p['protected'] if r['name'] != 'modemst2']), 'modemst2'),
    (lambda p: p['steps'][0]['segments'][0].update(first_sector=1411), '分区起点'),
    (lambda p: p['steps'][0]['segments'][0].update(sha256='00' * 32), 'SHA256'),
    (lambda p: p.update(extra=1), '未知字段'),
    (lambda p: p['steps'][0].update(expected_gpt_after_sha256='ab' * 32), '只有 GPT'),
])
def test_plan_rules(stick, tmp_path, change, match):
    plan_path = make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())])
    data = json.loads(plan_path.read_text(encoding='utf-8'))
    change(data)
    plan_path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(writeplan.PlanError, match=match):
        plan = writeplan.load_plan(plan_path)
        layout, primary = current_gpt(stick)
        writeplan.validate_step(plan, plan['steps'][0], layout, primary)


def test_oversized_segment_refused(stick, tmp_path):
    plan = make_plan(tmp_path, stick, [('tz', 'tz', 1410, bytes(512 * 101))])
    with pytest.raises(writeplan.PlanError, match='不超过分区'):
        run(plan, stick)


@pytest.mark.parametrize('fault', ['nak_program', 'no_rawmode', 'short_write', 'final_nak'])
def test_transport_failures_stop_without_retry(stick, tmp_path, fault):
    plan = make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())])
    with pytest.raises(firehose_write.WriteFailed):
        run(plan, stick, fault=fault)
    reports = list((write_runner.paths.local_dir() / 'writes').glob('*/report.json'))
    report = json.loads(reports[0].read_text(encoding='utf-8'))
    assert report['status'] == 'write_failed' and report['transcript'] and not report['passed']
    with pytest.raises(write_runner.WriteRefused, match='已经尝试过'):
        run(plan, stick)


def test_silent_target_times_out():
    target = FakeTarget(FakeDisk(SECTORS, STICK))
    target.fault = 'silent'
    session = firehose_write.WriteSession(target.out, target.inp, [{'first_sector': 1410, 'sectors': 1, 'sha256': '0' * 64}],
                                          reply_timeout=0.3)
    session.configure()
    with pytest.raises(firehose_write.WriteFailed, match='明确响应'):
        session.program_next(open(__file__, 'rb'))


def test_readback_mismatch_reported(stick, tmp_path):
    plan = make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())])
    report, _ = run(plan, stick, fault='corrupt')
    assert report['status'] == 'readback_mismatch' and not report['passed']


def test_collateral_change_detected(stick, tmp_path):
    plan = make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())])
    report, _ = run(plan, stick, fault='collateral')
    assert report['status'] == 'protected_changed'
    assert next(r for r in report['protected_after'] if r['name'] == 'modem')['changed_sectors'] == 1


def test_unstable_protected_is_unprovable_not_pass(stick, tmp_path):
    stick.flaky = {500: {2, 3}}  # modem 内一个扇区：写前第 2 次和写后读数都翻转 -> 不稳定且写后与写前第 1 次不同
    plan = make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())])
    report, _ = run(plan, stick)
    modem = next(r for r in report['protected_after'] if r['name'] == 'modem')
    assert modem['unstable_sectors'] == 1
    assert modem['unprovable_sectors'] == 1 and report['status'] == 'written_unprovable' and not report['passed']


def test_compare_protected_classification():
    a, b = [b'1', b'2', b'3', b'4'], [b'1', b'X', b'3', b'Y']
    after = [b'1', b'2', b'Z', b'Q']
    assert write_runner.compare_protected(a, b, after) == {'changed_sectors': 1, 'unstable_sectors': 2, 'unprovable_sectors': 1}


def build_new_gpt(disk, move_modem=False):
    # 原地替换表项（保持序号，夹具的 GUID 随序号生成），保留分区的 128 字节表项因此不变
    new = {'boot': ('boot', 1966, 2477), 'NSHSTATE': ('NSHSTATE', 2478, 2999), 'rootfs': ('rootfs', 3000, 8100)}
    if move_modem:
        new['modem'] = ('modem', 101, 1099)
    return gpt.build_gpt(SECTORS, [new.get(p[0], p) for p in STICK])


def gpt_plan(tmp_path, disk, move_modem=False):
    layout, _ = current_gpt(disk)
    primary, back = build_new_gpt(disk, move_modem)
    (tmp_path / 'gpt-p.bin').write_bytes(primary)
    (tmp_path / 'gpt-b.bin').write_bytes(back)
    plan = {'format': 1, 'kind': 'usbtool-write-plan', 'protected': PROTECTED, 'steps': [{
        'name': 'gpt', 'target': 'gpt', 'expected_gpt_sha256': layout['gpt_sha256'],
        'expected_gpt_after_sha256': gpt.gpt_digest(primary, back),
        'segments': [{'first_sector': 0, 'sectors': 34, 'payload': 'gpt-p.bin', 'sha256': hashlib.sha256(primary).hexdigest()},
                     {'first_sector': SECTORS - 33, 'sectors': 33, 'payload': 'gpt-b.bin',
                      'sha256': hashlib.sha256(back).hexdigest()}]}]}
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(plan), encoding='utf-8')
    return path


def test_gpt_step_end_to_end(stick, tmp_path):
    report, _ = run(gpt_plan(tmp_path, stick), stick, step='gpt')
    assert report['status'] == 'verified'
    layout, _ = current_gpt(stick)
    assert {p['name'] for p in layout['partitions']} >= {'modem', 'fsg', 'boot', 'rootfs', 'NSHSTATE'}
    assert report['gpt_after_sha256'] == layout['gpt_sha256'] != report['gpt_before_sha256']


def test_gpt_rewrite_moving_nv_partition_refused(stick, tmp_path):
    plan = writeplan.load_plan(gpt_plan(tmp_path, stick, move_modem=True))
    layout, primary = current_gpt(stick)
    with pytest.raises(writeplan.PlanError, match='modem'):
        writeplan.validate_step(plan, plan['steps'][0], layout, primary)


def test_decode_reply_rules():
    reply, logs = firehose_write.decode_reply(b'<?xml version="1.0" ?><data><log value="ACK" /></data>')
    assert reply is None and logs == ['ACK']
    with pytest.raises(ValueError):
        firehose_write.decode_reply(b'<data><response value="ACK" /></data><data><response value="ACK" /></data>')
    with pytest.raises(ValueError):
        firehose_write.decode_reply(b'<!DOCTYPE x><data><response value="ACK" /></data>')
    with pytest.raises(ValueError):
        firehose_write.decode_reply(b'<data><response value="MAYBE" /></data>')


def test_cli_write_dryrun_chains_gpt_change(stick, tmp_path, capsys):
    folder = backup.new_backup_dir(tmp_path / 'b')
    backup.run_backup(stick.read, folder, ['tz'], reread=False, log=lambda m: None)
    plan_path = gpt_plan(tmp_path, stick)
    data = json.loads(plan_path.read_text(encoding='utf-8'))
    payload = tz_payload()
    (tmp_path / 'tz.bin').write_bytes(payload)
    data['steps'].append({'name': 'tz', 'target': 'tz', 'expected_gpt_sha256': data['steps'][0]['expected_gpt_after_sha256'],
                          'segments': [{'first_sector': 1410, 'sectors': 100, 'payload': 'tz.bin',
                                        'sha256': hashlib.sha256(payload).hexdigest()}]})
    plan_path.write_text(json.dumps(data), encoding='utf-8')
    reads = len(stick.reads)
    assert cli.main(['write-dryrun', '--plan', str(plan_path), '--backup-dir', str(folder)]) == 0
    out = capsys.readouterr().out
    assert out.count('[通过]') == 2 and '步骤摘要' in out and len(stick.reads) == reads


@pytest.mark.parametrize('job', [
    {'action': 'write', 'mode': 'serial', 'plan': 'p', 'step': 's', 'confirm': 'c'},
    {'action': 'write', 'mode': 'usb', 'plan': 'p', 'step': 's', 'confirm': 'c', 'reset_after': True},
    {'action': 'write', 'mode': 'usb', 'plan': 'p', 'step': 's'},
])
def test_worker_write_job_rules(job):
    with pytest.raises(ValueError):
        worker.validate_job(job)


def test_keep_reads_saves_second_pass(stick, tmp_path):
    folder = backup.new_backup_dir(tmp_path / 'k')
    manifest = backup.run_backup(stick.read, folder, ['tz'], keep_reads=True, log=lambda m: None)
    item = manifest['items'][0]
    assert (folder / item['read2_file']).read_bytes() == stick.range(1410, 1509)


@pytest.mark.parametrize('sizes,expected', [
    ([34 * 512, 33 * 512], 512), ([524288], 16384), ([1 << 20], 16384),
    ([135249408], 512), ([2816454656], 4096), ([3 * 512], 512),
])
def test_chunk_size_divides_every_segment(sizes, expected):
    segs = [{'sectors': n // 512} for n in sizes]
    assert firehose_write.chunk_size(segs) == expected


def test_old_fixed_16k_chunks_reproduce_real_gpt_nak(stick, tmp_path):
    """按改之前的写法（MaxPayload 16384，GPT 主表 16384+1024，每块后补零长度包）手动发包，
    模拟目标重现真机现象：最后收到 NAK，什么都没写。新写法同一步可以正常写入。"""
    primary, _back = build_new_gpt(stick)
    target = FakeTarget(stick)
    before = bytes(stick.data)
    target.out.write(firehose_write.configure_xml(16384))
    assert b'ACK' in target.inp.read(4096)
    target.out.write(firehose_write.program_xml(0, 34))
    assert b'rawmode="true"' in target.inp.read(4096)
    for block in (primary[:16384], primary[16384:]):
        target.out.write(block)
        target.out.write(b'')
    reply, logs = firehose_write.decode_reply(target.inp.read(4096))
    assert reply['value'] == 'NAK' and 'Read back verify failed' in logs[0]
    assert bytes(stick.data) == before
    report, _ = run(gpt_plan(tmp_path, stick), stick, step='gpt')
    assert report['status'] == 'verified' and report['transcript'][0]['max_payload'] == 512


def test_flaky_readback_with_correct_majority_is_written_majority(stick, tmp_path):
    # tz 区域（1410..1509）里一个扇区在三次回读中只有一次读错：多数正确 -> written_majority，不算通过
    plan = make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())])
    stick.flaky = {1420: {4}}  # 第 1 次：前像；第 2-4 次：三次回读
    report, folder = run(plan, stick)
    rb = report['readback'][0]
    assert report['status'] == 'written_majority' and not report['passed']
    assert rb['majority_ok'] and not rb['ok'] and rb['mismatched_sectors_per_pass'] == [0, 0, 1]
    assert rb['raw_reads_saved'] and (folder / 'readback-1410-3.bin').exists()


def test_majority_wrong_is_readback_mismatch(stick, tmp_path):
    plan = make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())])
    stick.flaky = {1420: {2, 3}}  # 两次回读读错 -> 多数错误
    report, _ = run(plan, stick)
    rb = report['readback'][0]
    assert report['status'] == 'readback_mismatch' and rb['majority_wrong_list'] == [1420]


def test_second_configure_ignored_reproduces_not_written(stick, tmp_path):
    """真机 2026-11-01：连接时先 SkipWrite=1，再发 SkipWrite=0，加载器都回 ACK 但没写。旧写法应判为 not_written。"""
    plan = writeplan.load_plan(make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())]))
    target = FakeTarget(stick)
    target.out.write(b'<?xml version="1.0" ?><data><configure MemoryName="eMMC" SkipWrite="1" '
                     b'MaxPayloadSizeToTargetInBytes="2048" /></data>')  # 与本步块大小相同，只差 SkipWrite
    target.inp.read(4096)
    before = stick.range(1410, 1509)
    digest = writeplan.step_digest(plan, plan['steps'][0])
    report, _ = write_runner.run_step(plan, 'tz', digest, stick.read, target.out, target.inp, log=lambda m: None)
    assert report['status'] == 'not_written' and stick.range(1410, 1509) == before
    assert report['readback'][0]['looks_unwritten']


def test_preconfigured_write_session_writes(stick, tmp_path):
    plan = writeplan.load_plan(make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())]))
    chunk = firehose_write.chunk_size(plan['steps'][0]['segments'])
    target = FakeTarget(stick)
    target.out.write(firehose_write.configure_xml(chunk))  # 连接时唯一一次 configure
    target.inp.read(4096)
    digest = writeplan.step_digest(plan, plan['steps'][0])
    report, _ = write_runner.run_step(plan, 'tz', digest, stick.read, target.out, target.inp, log=lambda m: None,
                                      preconfigured_payload=chunk)
    assert report['status'] == 'verified' and target.configures == 1
    assert report['transcript'][0]['note'].startswith('连接时')


def test_preconfigured_payload_must_match_step_chunk():
    with pytest.raises(firehose_write.WriteFailed, match='块大小'):
        firehose_write.WriteSession(None, None, [{'sectors': 34, 'first_sector': 0, 'sha256': '0' * 64}],
                                    preconfigured_payload=16384)


def test_guard_write_configure_rules():
    from usbtool import guard
    head = '<?xml version="1.0" ?><data><configure MemoryName="eMMC" '
    p = guard.ReadOnlyPolicy(write_payload=512)
    p.check(head + 'SkipWrite="0" MaxPayloadSizeToTargetInBytes="512" /></data>')
    for bad in ('SkipWrite="1" MaxPayloadSizeToTargetInBytes="512"', 'SkipWrite="0" MaxPayloadSizeToTargetInBytes="16384"'):
        with pytest.raises(guard.RefusedOperation):
            p.check(head + bad + ' /></data>')
    with pytest.raises(guard.RefusedOperation):
        guard.ReadOnlyPolicy().check(head + 'SkipWrite="0" MaxPayloadSizeToTargetInBytes="512" /></data>')


class StaleFirehoseLink:
    def device_info(self):
        return {'msm_id': None}

    def endpoints(self):
        raise AssertionError('不应走到写入')

    def close(self):
        pass


def test_worker_write_refuses_stale_firehose_and_bad_confirm(stick, tmp_path):
    plan_path = make_plan(tmp_path, stick, [('tz', 'tz', 1410, tz_payload())])
    plan = writeplan.load_plan(plan_path)
    digest = writeplan.step_digest(plan, plan['steps'][0])
    loader = tmp_path / 'l.mbn'
    loader.write_bytes(b'x')
    job = {'action': 'write', 'mode': 'usb', 'loader': str(loader), 'allow_unknown_loader': True,
           'plan': str(plan_path), 'step': 'tz', 'confirm': digest}
    made = []
    with pytest.raises(ValueError, match='拔插'):
        worker.run(job, link_factory=lambda *a, **k: made.append(k) or StaleFirehoseLink())
    assert made[0]['write_payload'] == firehose_write.chunk_size(plan['steps'][0]['segments'])
    made.clear()
    with pytest.raises(ValueError, match='未连接设备'):
        worker.run({**job, 'confirm': '0' * 64}, link_factory=lambda *a, **k: made.append(k) or StaleFirehoseLink())
    assert made == []

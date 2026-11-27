# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import json
from types import SimpleNamespace
import zipfile

import pytest

from conftest import FakeDisk
from test_write import FakeTarget
from usbtool import backup, cli, detect, firehose_write, gpt, policy, ufi001c_chain_layout, worker, writeplan


class FakeLink:
    def __init__(self, disk, chip=policy.MSM8916_ID):
        self.disk, self.chip = disk, chip
        self.sectors, self.resets, self.closed = None, 0, False

    def device_info(self):
        return {'hwid': '007050e100000000', 'msm_id': f'0x{self.chip:06x}', 'serial': None, 'chip_ok': self.chip == policy.MSM8916_ID}

    def set_disk_sectors(self, n):
        self.sectors = n

    def read(self, start, count):
        return self.disk.read(start, count)

    def reset(self):
        self.resets += 1

    def close(self):
        self.closed = True


def events(capsys):
    out = capsys.readouterr().out.splitlines()
    return [worker.parse_line(l) for l in out if l.startswith(worker.PREFIX)]


@pytest.fixture
def loader(tmp_path):
    path = tmp_path / 'loader.mbn'
    path.write_bytes(b'not a real loader')
    return path


def test_probe_job(disk, loader, capsys):
    link = FakeLink(disk)
    job = {'action': 'probe', 'mode': 'serial', 'port': 'COM5', 'loader': str(loader), 'allow_unknown_loader': True}
    worker.run(job, link_factory=lambda *a, **k: link)
    result = [e for e in events(capsys) if e['type'] == 'result'][0]
    assert [p['name'] for p in result['layout']['partitions']][4] == 'boot'
    assert link.sectors == 8192 and link.closed and link.resets == 0
    assert result['warnings']  # 合成盘不是 UFI003 布局


def test_backup_job_with_reset(disk, loader, tmp_path, capsys):
    link = FakeLink(disk)
    job = {'action': 'backup', 'mode': 'usb', 'loader': str(loader), 'allow_unknown_loader': True,
           'partitions': ['boot'], 'reread': True, 'reset_after': True, 'backup_base': str(tmp_path / 'b')}
    worker.run(job, link_factory=lambda *a, **k: link)
    result = [e for e in events(capsys) if e['type'] == 'result'][0]
    assert result['manifest']['passed'] and link.resets == 1


def test_failed_backup_does_not_reset(disk, loader, tmp_path):
    disk.short = True
    link = FakeLink(disk)
    job = {'action': 'backup', 'mode': 'usb', 'loader': str(loader), 'allow_unknown_loader': True,
           'partitions': ['boot'], 'reset_after': True, 'backup_base': str(tmp_path / 'b')}
    with pytest.raises(Exception):
        worker.run(job, link_factory=lambda *a, **k: link)
    assert link.resets == 0 and link.closed


def test_chain_migration_cli_requires_matching_readbacks_and_verifies_both_directions(loader, tmp_path, monkeypatch):
    disk = FakeDisk(8192, [('sbl1', 64, 127), ('aboot', 128, 191), ('boot', 192, 255), ('hyp', 256, 319),
                           ('tz', 512, 1535), ('tzbak', 1536, 2559)])
    disk.data[4096 * 512:6148 * 512] = bytes(2052 * 512)
    _, primary, alternate = backup.read_layout(disk.read)
    monkeypatch.setattr(ufi001c_chain_layout, 'TZ_TYPE', writeplan.gpt_entries_by_name(primary)['tz'][:16])
    new_primary, new_alternate, _ = ufi001c_chain_layout.build(primary, alternate, 4096, 6144)
    payload = b'T' * (1183 * 512)
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(ufi001c_chain_layout, 'OPENSTICK_PAYLOADS', {'tz': ('tz.mbn', len(payload), digest)})
    archive = tmp_path / 'base.zip'
    with zipfile.ZipFile(archive, 'w') as file:
        file.writestr('base/tz.mbn', payload)
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    (candidate / 'gpt-primary.candidate.bin').write_bytes(new_primary)
    (candidate / 'gpt-backup.candidate.bin').write_bytes(new_alternate)
    (candidate / 'tz.candidate.bin').write_bytes(payload)
    baseline = tmp_path / 'baseline'
    baseline.mkdir()
    (baseline / 'target-gpt-primary.bin').write_bytes(primary)
    (baseline / 'target-gpt-backup.bin').write_bytes(alternate)
    (baseline / 'tz.bin').write_bytes(disk.read(512, 1024))
    (baseline / 'tzbak.bin').write_bytes(disk.read(1536, 1024))
    output = tmp_path / 'readback'
    monkeypatch.setitem(policy.KNOWN_LOADERS, backup.file_sha256(loader), 'test')
    monkeypatch.setattr(detect, 'usb_candidates', lambda: [{'port': '1-2'}])
    links = []
    serial = 'aa12'
    fault = None

    def connect(*args, **kwargs):
        link = FakeLink(disk)
        link.device_info = lambda: dict(FakeLink.device_info(link), serial=serial)
        chunk = kwargs.get('write_payload')
        if chunk is not None:
            assert chunk == 512
            target = FakeTarget(disk)
            target.out.write(firehose_write.configure_xml(chunk))
            target.inp.read(4096)
            target.fault = fault
            link.endpoints = lambda: (target.out, target.inp)
        links.append(link)
        return link

    def run_job(job, timeout):
        assert timeout == (cli.PROBE_TIMEOUT if job['action'] == 'chain-preflight' else None)
        worker.run(job, link_factory=connect)
        return 0

    monkeypatch.setattr(cli, '_run_job', run_job)
    args = ['chain-preflight', '--candidate', str(candidate), '--baseline', str(baseline), '--openstick-base',
            str(archive), '--loader', str(loader), '--output', str(output)]
    (candidate / 'tz.candidate.bin').write_bytes(b'tampered')
    disk.reads.clear()
    with pytest.raises(gpt.GptError, match='候选镜像与原始输入不一致'):
        cli.main(args)
    assert not disk.reads and not output.exists()
    (candidate / 'tz.candidate.bin').write_bytes(payload)
    disk.data[512 * 512] ^= 1
    with pytest.raises(gpt.GptError, match='实机引导镜像与所存副本不一致'):
        cli.main(args)
    assert not output.exists()
    disk.data[512 * 512] ^= 1
    assert cli.main(args) == 0
    assert all(link.closed and link.resets == 0 for link in links)
    receipt = json.loads((output / 'manifest.json').read_text())
    assert receipt['writes_performed'] is False and receipt['hardware_boot_verified'] is False
    assert receipt['items']['gap-tzbak']['must_be_zero']
    assert receipt['items']['tz']['sha256'] == backup.file_sha256(baseline / 'tz.bin')

    before = bytes(disk.data)
    digest = backup.file_sha256(output / 'manifest.json')

    def migrate(direction, folder, confirm=digest):
        return cli.main(['chain-migrate', direction, *args[1:-2], '--output', str(tmp_path / folder),
                         '--readback', str(output), '--confirm', confirm])

    connections = len(links)
    with pytest.raises(gpt.GptError, match='摘要与确认不一致'):
        migrate('apply', 'bad-digest', '0' * 64)
    assert len(links) == connections
    serial = 'bb34'
    with pytest.raises(gpt.GptError, match='不同的棒子'):
        migrate('apply', 'other-device')
    assert bytes(disk.data) == before
    serial = 'aa12'
    fault = 'final_nak'
    with pytest.raises(firehose_write.WriteFailed):
        migrate('apply', 'lost-ack')
    assert json.loads((tmp_path / 'lost-ack/manifest.json').read_text())['status'] == 'stopped'
    fault = None
    assert migrate('apply', 'resume') == 0
    assert backup.read_layout(disk.read)[1:] == (new_primary, new_alternate)
    assert migrate('restore', 'undo') == 0
    assert bytes(disk.data) == before
    assert all(link.closed and link.resets == 0 for link in links)


def test_unknown_loader_refused_without_confirmation(disk, loader):
    job = {'action': 'probe', 'mode': 'usb', 'loader': str(loader)}
    with pytest.raises(Exception, match='已知名单'):
        worker.run(job, link_factory=lambda *a, **k: FakeLink(disk))


@pytest.mark.parametrize('job', [
    {'action': 'write', 'mode': 'usb'}, {'action': 'probe', 'mode': 'tcp'},
    {'action': 'backup', 'mode': 'usb', 'partitions': []},
])
def test_invalid_jobs_refused(job):
    with pytest.raises(ValueError):
        worker.validate_job(job)


def test_main_reports_error_event(tmp_path, capsys):
    path = tmp_path / 'job.json'
    path.write_text(json.dumps({'action': 'erase', 'mode': 'usb'}), encoding='utf-8')
    assert worker.main(str(path)) == 1
    assert events(capsys)[-1]['type'] == 'error'


def test_parse_line_plain_text():
    assert worker.parse_line('Device detected :)\n') == {'type': 'log', 'message': 'Device detected :)'}


def test_detect_prefers_serial():
    ports = lambda: [SimpleNamespace(vid=0x05C6, pid=0x9008, device='COM7', description='Qualcomm HS-USB QDLoader 9008'),
                     SimpleNamespace(vid=0x1234, pid=0x9008, device='COM3', description='other')]
    devices, notes = detect.detect(list_ports=ports, find=lambda: [SimpleNamespace(bus=1, address=4)])
    assert devices == [{'mode': 'serial', 'port': 'COM7', 'description': 'Qualcomm HS-USB QDLoader 9008'}]
    assert notes


def test_detect_usb_only():
    devices, _ = detect.detect(list_ports=lambda: [], find=lambda: [SimpleNamespace(bus=2, address=9)])
    assert devices == [{'mode': 'usb', 'port': '2-9', 'description': 'WinUSB/libusb'}]


def test_detect_survives_enumeration_errors():
    def boom():
        raise OSError('no backend')
    devices, notes = detect.detect(list_ports=boom, find=boom)
    assert devices == [] and len(notes) == 2


def test_selfcheck_patches_real_edl(capsys):
    from usbtool import cli
    assert cli.main(['selfcheck']) == 0
    assert '已禁用' in capsys.readouterr().out


def test_watchdog_expires_without_progress():
    import threading
    fired = threading.Event()
    seen = []
    dog = worker.Watchdog(on_expire=lambda phase, limit: (seen.append((phase, limit)), fired.set()), interval=0.02)
    dog.arm('connect', 0.1)
    assert fired.wait(2) and seen == [('connect', 0.1)]
    dog.stop()


def test_watchdog_kick_keeps_it_alive():
    import time
    fired = []
    dog = worker.Watchdog(on_expire=lambda *a: fired.append(a), interval=0.02)
    dog.arm('read', 0.3)
    for _ in range(10):
        time.sleep(0.05)
        dog.kick()
    dog.stop()
    assert fired == []


def test_serial_read_timeout_is_capped():
    import sys
    from usbtool import edl_link
    edl_link.SERIAL_READ_CAP[0] = 5.0
    assert edl_link.capped(None) == 5.0 and edl_link.capped(1500) == 5.0 and edl_link.capped(1) == 1
    old = sys.argv
    sys.argv = ['edl.py', 'printgpt']
    try:
        from edlclient.Library.Connection.seriallib import serial_class
    finally:
        sys.argv = old
    seen = []
    original, original_read = serial_class.usbread, serial_class.read
    try:
        serial_class.usbread = lambda self, resplen=None, timeout=0: seen.append(timeout)
        edl_link.cap_serial_reads()
        edl_link.cap_serial_reads()  # 重复调用不叠加
        serial_class.usbread(None, 4, 1500)
        assert seen == [5.0]
    finally:
        serial_class.usbread, serial_class.read = original, original_read


def test_wait_for_data_waits_until_bytes_arrive_or_limit():
    from types import SimpleNamespace
    from usbtool import edl_link
    clock = [0.0]
    device = SimpleNamespace(in_waiting=0)

    def sleep(dt):
        clock[0] += dt
        if clock[0] >= 0.5:
            device.in_waiting = 48  # Hello 在端口打开后约 0.5 秒到达

    edl_link.wait_for_data(device, 3.0, clock=lambda: clock[0], sleep=sleep)
    assert device.in_waiting == 48 and 0.5 <= clock[0] < 0.6
    device.in_waiting, clock[0] = 0, 0.0
    edl_link.wait_for_data(device, 1.0, clock=lambda: clock[0], sleep=lambda dt: clock.__setitem__(0, clock[0] + dt))
    assert 1.0 <= clock[0] < 1.1  # 一直没有数据：最多等到上限


def test_serial_read_waits_for_hello_before_xml_clamp():
    import sys
    from types import SimpleNamespace
    from usbtool import edl_link
    old = sys.argv
    sys.argv = ['edl.py', 'printgpt']
    try:
        from edlclient.Library.Connection.seriallib import serial_class
    finally:
        sys.argv = old
    original, original_read = serial_class.usbread, serial_class.read
    try:
        calls = []
        edl_link.cap_serial_reads()
        waits = []
        real_wait = edl_link.wait_for_data
        edl_link.wait_for_data = lambda device, limit: (waits.append(limit), setattr(device, 'in_waiting', 48))
        fake = SimpleNamespace(xmlread=True, timeout=1500, device=SimpleNamespace(in_waiting=0),
                               usbread=lambda resplen, timeout: calls.append(resplen) or b'x' * resplen)
        assert serial_class.read(fake, 0x30, 1) == b'x' * 48
        assert waits == [edl_link.DATA_WAIT] and calls == [48]
    finally:
        edl_link.wait_for_data = real_wait
        serial_class.usbread, serial_class.read = original, original_read


def test_main_writes_log_file(tmp_path, capsys, local_dir):
    path = tmp_path / 'job-x.json'
    path.write_text(json.dumps({'action': 'erase', 'mode': 'usb'}), encoding='utf-8')
    assert worker.main(str(path)) == 1
    log = (local_dir / 'logs' / 'job-x.log').read_text(encoding='utf-8')
    assert '日志文件' in log and '"type": "error"' in log


def test_usb_mode_uses_explicit_libusb1_backend():
    import sys
    from usbtool import detect as detect_module, edl_link
    old = sys.argv
    sys.argv = ['edl.py', 'printgpt']
    try:
        from edlclient.Library.Connection.usblib import usb_class
    finally:
        sys.argv = old
    original = usb_class.__init__
    try:
        sentinel = object()
        edl_link.use_usb_backend(sentinel)
        edl_link.use_usb_backend(sentinel)  # 重复调用不嵌套
        dev = usb_class(portconfig=[[0x05c6, 0x9008, -1]])
        assert dev.backend is sentinel
    finally:
        usb_class.__init__ = original
    backend = detect_module.libusb_backend()
    assert backend is not None and type(backend).__module__ == 'usb.backend.libusb1'

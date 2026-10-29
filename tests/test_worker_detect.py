# SPDX-License-Identifier: GPL-3.0-only
import json
from types import SimpleNamespace

import pytest

from usbtool import detect, policy, worker


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

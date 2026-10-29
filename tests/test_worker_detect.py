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

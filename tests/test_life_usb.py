# SPDX-License-Identifier: GPL-3.0-only
from types import SimpleNamespace

import pytest

from usbtool import emmc_life, life_usb, paths

SAMPLE = 'type=MMC\nname=8GTF4R\nmanfid=0x000015\nrev=0x8\nlife_time=0x02 0x03\npre_eol_info=0x01\n'
SERIAL = 'ab' * 16


def ok(stdout='', code=0, stderr=''):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


@pytest.fixture
def key(tmp_path):
    path = tmp_path / 'id'
    path.write_text('k')
    return str(path)


def test_serials_only_from_rustsimhub_gadget():
    out = '\n'.join(['USB\\VID_1D6B&PID_0104\\' + SERIAL.upper(), 'USB\\VID_05C6&PID_9008\\5&123',
                     'USB\\VID_1D6B&PID_0104\\short', ''])
    assert life_usb.usb_stick_serials(run=lambda *a, **k: ok(out)) == [SERIAL]


def test_one_click_pins_per_stick_and_runs_fixed_command(key, tmp_path):
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return ok('' if command[0] == 'ssh-keygen' else SAMPLE, 1 if command[0] == 'ssh-keygen' else 0)
    hosts = str(tmp_path / 'hosts')
    (tmp_path / 'hosts').write_text('')
    report = life_usb.read_over_usb(key, run=run, serials=[SERIAL], known_hosts=hosts)
    ssh = seen[-1]
    assert report['verdict'] == 'ok' and report['host_key_recorded_now'] is True and '第一次连接' in report['note']
    assert 'StrictHostKeyChecking=accept-new' in ssh and f'HostKeyAlias=rustsimhub-usb-{SERIAL}' in ssh
    assert f'UserKnownHostsFile={hosts}' in ssh and 'BatchMode=yes' in ssh and 'ClearAllForwardings=yes' in ssh
    assert ssh[-2:] == ['root@10.55.0.2', emmc_life.REMOTE_COMMAND]


def test_known_stick_is_not_reported_as_new(key, tmp_path):
    (tmp_path / 'hosts').write_text('x')

    def run(command, **kwargs):
        return ok('# Host found\nline' if command[0] == 'ssh-keygen' else SAMPLE)
    report = life_usb.read_over_usb(key, run=run, serials=[SERIAL], known_hosts=str(tmp_path / 'hosts'))
    assert report['host_key_recorded_now'] is False and 'note' not in report


def test_changed_host_key_is_refused_with_explanation(key, tmp_path):
    def run(command, **kwargs):
        if command[0] == 'ssh-keygen':
            return ok('', 1)
        return ok('', 255, 'WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\nHost key verification failed.')
    with pytest.raises(emmc_life.LifeError, match='主机公钥和上次记下的不同'):
        life_usb.read_over_usb(key, run=run, serials=[SERIAL], known_hosts=str(tmp_path / 'h'))


@pytest.mark.parametrize('serials,match', [([], '没有找到 USB'), ([SERIAL, 'cd' * 16], '只留一根')])
def test_zero_or_several_sticks_refused_before_ssh(key, serials, match):
    def run(*a, **k):
        raise AssertionError('不应运行 ssh')
    with pytest.raises(emmc_life.LifeError, match=match):
        life_usb.read_over_usb(key, run=run, serials=serials)


def test_missing_key_refused(monkeypatch):
    monkeypatch.setattr(life_usb, 'default_identity', lambda: None)
    with pytest.raises(emmc_life.LifeError, match='维护私钥'):
        life_usb.read_over_usb(run=lambda *a, **k: ok(), serials=[SERIAL])


def test_default_identity_found_in_rustsimhub_private(tmp_path, monkeypatch):
    tool = tmp_path / 'RustSimHub-UsbTool' / 'dist'
    tool.mkdir(parents=True)
    monkeypatch.setattr(paths, 'app_dir', lambda: tool)
    assert life_usb.default_identity() is None
    folder = tmp_path / 'RustSimHub' / '.private' / 'maintenance-bootstrap-20261018-first'
    folder.mkdir(parents=True)
    (folder / 'id_ed25519').write_text('k')
    (tmp_path / 'RustSimHub' / '.private' / 'other').mkdir()
    assert life_usb.default_identity() == str(folder / 'id_ed25519')


def ext_csd(life_a, life_b, eol):
    data = bytearray(512)
    data[267], data[268], data[269] = eol, life_a, life_b
    return data.hex()


def test_adb_uses_sysfs_when_present():
    assert life_usb.parse_adb(SAMPLE + 'ext_csd=\n')['source'] == 'adb-sysfs'


def test_adb_falls_back_to_ext_csd_on_old_kernels():
    old = 'type=MMC\nname=8GTF4R\nmanfid=0x000015\nrev=\nlife_time=\npre_eol_info=\n'
    report = life_usb.parse_adb(old + 'ext_csd=' + ext_csd(9, 2, 2) + '\n')
    assert report['source'] == 'adb-ext_csd' and report['verdict'] == 'warning' and report['chip']['name'] == '8GTF4R'


def test_adb_without_any_life_data_says_so():
    old = 'type=MMC\nname=x\nmanfid=\nrev=\nlife_time=\npre_eol_info=\n'
    report = life_usb.parse_adb(old + 'ext_csd=\n')
    assert report['verdict'] == 'unknown' and 'root' in report['note']
    with pytest.raises(emmc_life.LifeError):
        life_usb.parse_adb('garbage')


def test_adb_needs_exactly_one_authorized_device():
    def run_with(listing):
        def run(command, **kwargs):
            if command[1] == 'devices':
                return ok(listing)
            return ok(SAMPLE)
        return run
    with pytest.raises(emmc_life.LifeError, match='没有找到已授权'):
        life_usb.read_over_adb(run=run_with('List of devices attached\nX1\tunauthorized\n'), adb='adb')
    with pytest.raises(emmc_life.LifeError, match='只留一根'):
        life_usb.read_over_adb(run=run_with('List of devices attached\nA\tdevice\nB\tdevice\n'), adb='adb')
    seen = []

    def record(command, **kwargs):
        seen.append(command)
        return ok('List of devices attached\nA\tdevice\n' if command[1] == 'devices' else SAMPLE)
    assert life_usb.read_over_adb(run=record, adb='adb')['verdict'] == 'ok'
    assert seen[-1] == ['adb', '-s', 'A', 'shell', life_usb.ADB_COMMAND]


def test_adb_command_is_read_only():
    for word in ('rm ', 'dd ', 'echo >', 'mount', 'setprop', 'reboot', '>'):
        assert word not in life_usb.ADB_COMMAND.replace('2>/dev/null', '')

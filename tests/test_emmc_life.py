# SPDX-License-Identifier: GPL-3.0-only
import json
import subprocess
from types import SimpleNamespace

import pytest

from usbtool import cli, emmc_life, worker

SAMPLE = 'type=MMC\nname=8GTF4R\nmanfid=0x000015\nrev=0x8\nlife_time=0x02 0x03\npre_eol_info=0x01\n'


@pytest.mark.parametrize('code,text,verdict', [
    (None, '芯片未提供', 'unknown'), (0, '芯片未提供', 'unknown'), (1, '已用 0%–10%', 'ok'), (7, '已用 60%–70%', 'ok'),
    (8, '已用 70%–80%', 'warning'), (10, '已用 90%–100%', 'critical'), (11, '已超过标称寿命', 'critical'),
    (12, '保留值 0x0c', 'unknown'),
])
def test_decode_life(code, text, verdict):
    assert emmc_life.decode_life(code) == (text, verdict)


def test_parse_normal_output():
    report = emmc_life.parse_sysfs(SAMPLE)
    assert report['available'] and report['verdict'] == 'ok' and report['life_type_b_text'] == '已用 20%–30%'
    assert report['chip'] == {'name': '8GTF4R', 'manfid': '0x000015', 'rev': '0x8'}
    assert '正常' in emmc_life.format_report(report)


@pytest.mark.parametrize('eol,verdict', [('0x02', 'warning'), ('0x03', 'critical')])
def test_worst_item_decides(eol, verdict):
    assert emmc_life.parse_sysfs(SAMPLE.replace('pre_eol_info=0x01', f'pre_eol_info={eol}'))['verdict'] == verdict


def test_older_emmc_without_life_fields_is_unknown_not_ok():
    report = emmc_life.parse_sysfs('type=MMC\nname=X\nlife_time=\npre_eol_info=\n')
    assert report['verdict'] == 'unknown' and not report['available'] and '5.0' in report['note']
    zero = emmc_life.parse_sysfs('type=MMC\nlife_time=0x00 0x00\npre_eol_info=0x00\n')
    assert zero['verdict'] == 'unknown' and not zero['available']


@pytest.mark.parametrize('output', ['', 'garbage\n', 'other=1\n'])
def test_unexpected_output_refused(output):
    with pytest.raises(emmc_life.LifeError, match='预期'):
        emmc_life.parse_sysfs(output)


def test_not_emmc_refused():
    with pytest.raises(emmc_life.LifeError, match='不是 eMMC'):
        emmc_life.parse_sysfs('type=SD\nlife_time=0x01 0x01\n')


def test_serial_is_never_requested():
    assert 'cid' not in emmc_life.REMOTE_COMMAND and 'serial' not in emmc_life.REMOTE_COMMAND


@pytest.fixture
def key_files(tmp_path):
    key, hosts = tmp_path / 'id', tmp_path / 'hosts'
    key.write_text('k')
    hosts.write_text('h')
    return str(key), str(hosts)


def test_ssh_command_is_strict_and_fixed(key_files):
    seen = {}

    def run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout=SAMPLE, stderr='')

    report = emmc_life.read_over_ssh('root@10.55.0.2', *key_files, run=run)
    command = seen['command']
    assert report['verdict'] == 'ok'
    assert 'StrictHostKeyChecking=yes' in command and 'BatchMode=yes' in command and 'IdentitiesOnly=yes' in command
    assert f'UserKnownHostsFile={key_files[1]}' in command and 'ClearAllForwardings=yes' in command
    assert command[-1] == emmc_life.REMOTE_COMMAND and command[-2] == 'root@10.55.0.2'


@pytest.mark.parametrize('host', ['', '-oProxyCommand=calc', 'root@-x', 'a b', 'root@host;rm'])
def test_bad_host_refused_before_running_ssh(host, key_files):
    def run(*a, **k):
        raise AssertionError('不应运行 ssh')
    with pytest.raises(emmc_life.LifeError, match='地址'):
        emmc_life.read_over_ssh(host, *key_files, run=run)


def test_missing_known_hosts_refused_no_tofu(key_files):
    def run(*a, **k):
        raise AssertionError('不应运行 ssh')
    with pytest.raises(emmc_life.LifeError, match='known_hosts'):
        emmc_life.read_over_ssh('root@10.55.0.2', key_files[0], None, run=run)
    with pytest.raises(emmc_life.LifeError, match='私钥'):
        emmc_life.read_over_ssh('root@10.55.0.2', 'missing', key_files[1], run=run)


def test_ssh_failures_reported(key_files):
    def denied(*a, **k):
        return SimpleNamespace(returncode=255, stdout='', stderr='debug\nHost key verification failed.\n')

    def slow(*a, **k):
        raise subprocess.TimeoutExpired('ssh', 1)

    def absent(*a, **k):
        raise FileNotFoundError()

    with pytest.raises(emmc_life.LifeError, match='Host key verification failed'):
        emmc_life.read_over_ssh('10.55.0.2', *key_files, run=denied)
    with pytest.raises(emmc_life.LifeError, match='超过'):
        emmc_life.read_over_ssh('10.55.0.2', *key_files, run=slow)
    with pytest.raises(emmc_life.LifeError, match='OpenSSH'):
        emmc_life.read_over_ssh('10.55.0.2', *key_files, run=absent)


@pytest.mark.parametrize('result', [None, (None, None)])
def test_firehose_unsupported_says_so(result):
    report = emmc_life.from_firehose(result)
    assert report['supported'] is False and not report['available'] and report['verdict'] == 'unknown'
    assert '不支持' in emmc_life.format_report(report)


def test_firehose_json_fields_decoded_without_serial():
    value = 'INFO: ' + json.dumps({'storage_info': {'life_time_est_typ_a': '0x03', 'life_time_est_typ_b': '0x09',
                                                    'pre_eol_info': 1, 'serial_num': 12345}})
    report = emmc_life.from_firehose((value.replace('INFO: ', ''), SimpleNamespace(data={})))
    assert report['life_type_a'] == 3 and report['life_type_b'] == 9 and report['verdict'] == 'warning'
    assert '12345' not in json.dumps(report, ensure_ascii=False)


def test_firehose_supported_but_no_life_fields():
    report = emmc_life.from_firehose((None, SimpleNamespace(data={'total_blocks': '7634944'})))
    assert report['supported'] and not report['available'] and '没有返回寿命' in report['note']


class StorageLink:
    def device_info(self):
        return {'msm_id': '0x7050e1'}

    def storage_info(self):
        return None, None

    def close(self):
        pass


def test_worker_storage_job(tmp_path, capsys):
    loader = tmp_path / 'l.mbn'
    loader.write_bytes(b'x')
    job = {'action': 'storage', 'mode': 'usb', 'loader': str(loader), 'allow_unknown_loader': True}
    worker.run(job, link_factory=lambda *a, **k: StorageLink())
    events = [worker.parse_line(l) for l in capsys.readouterr().out.splitlines() if l.startswith(worker.PREFIX)]
    assert next(e for e in events if e['type'] == 'result')['report']['supported'] is False


def test_guard_still_refuses_reads_other_than_storage_query():
    from usbtool import guard
    policy = guard.ReadOnlyPolicy()
    policy.check('<?xml version="1.0" ?><data><getstorageinfo physical_partition_number="0"/></data>')
    with pytest.raises(guard.RefusedOperation):
        policy.check('<?xml version="1.0" ?><data><peek address64="0" SizeInBytes="4"/></data>')


def test_cli_ssh_mode_reports_errors(capsys):
    assert cli.main(['emmc-life', '--identity', 'missing', '--known-hosts', 'missing']) == 1
    assert '私钥' in capsys.readouterr().err

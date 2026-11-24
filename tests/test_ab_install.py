# SPDX-License-Identifier: GPL-3.0-only
import hashlib
from pathlib import Path

import pytest

from conftest import FakeDisk
from test_write import FakeTarget
from usbtool import ab_gpt, backup, flashpack, gpt, oneclick, storage_layout, writeplan

DISK = 8192
DEVICE = {'chip_ok': True, 'serial': 'aa1234', 'msm_id': '0x7050e1', 'loader_sha256': 'a' * 64}
KIB = 1024


def source_parts(disk, boot_start, boot_size, chunk):
    names = ('modem', 'sbl1', 'rpm', 'tz', 'modemst1', 'modemst2', 'fsg', 'aboot', 'persist')
    start = 64
    parts = []
    for name in names:
        parts.append((name, start, start + chunk - 1))
        start += chunk
    end = boot_start + boot_size
    return parts + [('boot', boot_start, end - 1), ('system', end, end + chunk - 1),
                    ('cache', end + chunk, end + 2 * chunk - 1),
                    ('recovery', end + 2 * chunk, end + 3 * chunk - 1),
                    ('userdata', end + 3 * chunk, disk - gpt.BACKUP_SECTORS - 1)]


@pytest.mark.parametrize('capacity,disk', [(4, 7634944), (8, 15269920)])
def test_real_size_gpt_preserves_original_entries_and_both_gpt_copies(capacity, disk):
    parts = source_parts(disk, 526336, 131072, 4096)
    base, alternate = gpt.build_gpt(disk, parts)
    primary, second, target, profile = ab_gpt.build(base, alternate, 'UFI003')
    assert profile == capacity
    assert primary[:512] == base[:512]
    assert gpt.parse_gpt(primary, second, disk) == target
    before = gpt.parse_gpt(base, alternate, disk)
    old_rows = writeplan.gpt_entries_by_name(base)
    new_rows = writeplan.gpt_entries_by_name(primary)
    for row in before['partitions']:
        if row['name'] not in storage_layout.STOCK:
            assert old_rows[row['name']] == new_rows[row['name']]
            assert row in target['partitions']
    assert len(new_rows) - len(old_rows) == 4
    assert new_rows['RSH_A'][16:32] != new_rows['RSH_B'][16:32]


@pytest.fixture
def installable(tmp_path, monkeypatch):
    block = 16 * KIB
    monkeypatch.setattr(ab_gpt, 'BOOT_BYTES', 64 * block)
    monkeypatch.setattr(storage_layout, 'MIB', block)
    monkeypatch.setattr(storage_layout, 'ALIGN', block // 512)
    monkeypatch.setattr(storage_layout, 'PROFILES', {4: (4, 1, 1, 1, 1)})
    monkeypatch.setattr(storage_layout, 'capacity_profile', lambda value: 4)
    monkeypatch.setattr(flashpack, 'install_boot_ledger', lambda: bytes(8 * block))
    disk = FakeDisk(DISK, source_parts(DISK, 1024, 2048, 64))
    old, tail = disk.read(0, gpt.PRIMARY_SECTORS), disk.read(DISK - gpt.BACKUP_SECTORS, gpt.BACKUP_SECTORS)
    disk.data[64 * 512:64 * 512 + 19] = b'UFI003_CT 20211210\0'
    source = gpt.parse_gpt(old, tail, DISK)
    _, _, target, profile = ab_gpt.build(old, tail, 'UFI003')
    aboot = next(row for row in source['partitions'] if row['name'] == 'aboot')
    pin = hashlib.sha256(disk.read(aboot['first_sector'], aboot['sectors'])).hexdigest()
    images = {}
    for row in target['partitions']:
        name = row['name']
        if name not in flashpack.AB_IMAGES:
            continue
        path = tmp_path / f'{name}.img'
        path.write_bytes(bytes(row['bytes']) if name == 'RSH_BOOTCTL' else bytes([row['index']])*row['bytes'])
        images[name] = path
    key = tmp_path / 'signing.key'
    public = flashpack.generate_key(key)
    monkeypatch.setattr(flashpack, 'TRUSTED_KEYS', {public: 'synthetic test'})
    pack = tmp_path / 'firmware.rshpack'
    flashpack.create_ab_pack(pack, 'A/B test', '1', profile, pin, images, key)
    folder = flashpack.import_pack(pack)
    return disk, folder, target


def run(folder, disk, phase, *, retry=False, fault=None, device=DEVICE):
    target = FakeTarget(disk)
    target.fault = fault
    target.writes_enabled = True
    target.max_payload = dict(oneclick.PHASES)[phase]
    return oneclick.run_session(folder, phase, disk.read, target.out, target.inp, device,
                                log=lambda message: None, retry=retry)


def test_signed_install_full_backup_then_verify_and_publish_boot_ledger(installable):
    disk, folder, target = installable
    source = bytes(disk.data)
    first = run(folder, disk, 'prepare')
    assert first['written'] == ['gpt'] and not first['done']
    saved, errors = backup.verify_backup(Path(first['backup']))
    assert not errors and saved['reread_verified']
    assert (Path(first['backup'])/'full-disk.img').read_bytes() == source
    with pytest.raises(oneclick.FlashStopped, match='先完成'):
        run(folder, disk, 'images', device=dict(DEVICE, serial='dead'))
    final = run(folder, disk, 'images')
    assert final['written'] == list(flashpack.AB_IMAGES)
    assert final['manual_reboot'] and final['done']
    actual, _, _ = backup.read_layout(disk.read)
    assert actual == target
    for part in actual['partitions']:
        if part['name'] not in flashpack.AB_IMAGES:
            assert disk.range(part['first_sector'], part['last_sector']) == source[part['first_sector']*512:(part['last_sector']+1)*512]
    with pytest.raises(oneclick.FlashStopped, match='重新初始化'):
        run(folder, disk, 'prepare')


def test_failed_image_keeps_boot_ledger_and_refuses_unapproved_retry(installable):
    disk, folder, target = installable
    run(folder, disk, 'prepare')
    ledger = next(part for part in target['partitions'] if part['name'] == 'RSH_BOOTCTL')
    before = disk.read(ledger['first_sector'], ledger['sectors'])
    with pytest.raises(Exception, match='NAK'):
        run(folder, disk, 'images', fault='nak_program')
    assert disk.read(ledger['first_sector'], ledger['sectors']) == before
    with pytest.raises(oneclick.FlashStopped, match='再次确认'):
        run(folder, disk, 'images')
    assert run(folder, disk, 'images', retry=True)['done']

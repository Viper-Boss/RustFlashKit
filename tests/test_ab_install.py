# SPDX-License-Identifier: GPL-3.0-only
import hashlib
from pathlib import Path
import zipfile

import pytest

from conftest import FakeDisk
from test_write import FakeTarget
from usbtool import ab_gpt, ab_install, backup, flashpack, gpt, oneclick, storage_layout, writeplan

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


@pytest.mark.parametrize('usable', [True, False])
def test_stock_boot_expansion_uses_only_adjacent_android_system(usable):
    disk = 7634944
    boot_start = 396384
    boot_sectors = (16 << 20) // gpt.SECTOR
    parts = source_parts(disk, boot_start, boot_sectors, 4096)
    if usable:
        system_start = boot_start + boot_sectors
        parts = parts[:-4] + [
            ('system', system_start, 2067551),
            ('cache', 2133088, 2395231),
            ('recovery', 2395232, 2427999),
            ('userdata', 2428000, disk - gpt.BACKUP_SECTORS - 1),
        ]
    base, alternate = gpt.build_gpt(disk, parts)
    if not usable:
        with pytest.raises(storage_layout.LayoutError, match='boot 扩容需要'):
            ab_gpt.build(base, alternate, 'UFI003')
        return

    primary, tail, target, profile = ab_gpt.build(base, alternate, 'UFI003')
    assert profile == 4
    assert gpt.parse_gpt(primary, tail, disk) == target
    old_rows = writeplan.gpt_entries_by_name(base)
    new_rows = writeplan.gpt_entries_by_name(primary)
    assert not set(new_rows).intersection(storage_layout.STOCK)
    for name, old_row in old_rows.items():
        if name == 'boot':
            assert new_rows[name][:40] == old_row[:40]
            assert new_rows[name][48:] == old_row[48:]
        elif name not in storage_layout.STOCK:
            assert new_rows[name] == old_row
    boot = next(row for row in target['partitions'] if row['name'] == 'boot')
    assert (boot['first_sector'], boot['bytes']) == (boot_start, 64 << 20)


@pytest.fixture
def installable(tmp_path, monkeypatch):
    block = 16 * KIB
    monkeypatch.setattr(ab_gpt, 'BOOT_BYTES', 64 * block)
    monkeypatch.setattr(storage_layout, 'MIB', block)
    monkeypatch.setattr(storage_layout, 'ALIGN', block // 512)
    monkeypatch.setattr(storage_layout, 'PROFILES', {4: (4, 1, 1, 1, 1)})
    monkeypatch.setattr(storage_layout, 'capacity_profile', lambda value: 4)
    monkeypatch.setattr(flashpack, 'install_boot_ledger', lambda: bytes(8 * block))
    parts = [(name, start, start + block // gpt.SECTOR - 1 if name == 'aboot' else end)
             for name, start, end in source_parts(DISK, 1024, 2048, 64)]
    disk = FakeDisk(DISK, parts)
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
        size = 64 * block if name == 'RSH_AUDIO' else row['bytes']
        path.write_bytes(bytes(size) if name == 'RSH_BOOTCTL' else bytes([row['index']])*size)
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


def test_compressed_pack_must_match_signed_uncompressed_bytes(installable, tmp_path, monkeypatch):
    original = tmp_path / 'firmware.rshpack'
    changed = tmp_path / 'changed.rshpack'
    cache = tmp_path / 'new-cache'
    cache.mkdir()
    monkeypatch.setattr(flashpack, 'packs_dir', lambda: cache)
    with zipfile.ZipFile(original) as incoming, zipfile.ZipFile(changed, 'x') as outgoing:
        assert incoming.getinfo('RSH_A.img').compress_type == zipfile.ZIP_DEFLATED
        for entry in incoming.infolist():
            data = incoming.read(entry)
            if entry.filename == 'RSH_A.img':
                data = bytes([data[0] ^ 1]) + data[1:]
            outgoing.writestr(entry.filename, data, compress_type=entry.compress_type)
    with pytest.raises(flashpack.PackError, match='RSH_A.img 与清单不一致'):
        flashpack.import_pack(changed)
    assert not list(cache.iterdir())
    assert flashpack.import_pack(original).is_dir()


def test_signed_install_calibration_backup_then_verify_and_publish_boot_ledger(installable):
    disk, folder, target = installable
    source = bytes(disk.data)
    first = run(folder, disk, 'prepare')
    assert first['written'] == ['gpt'] and not first['done']
    saved, errors = backup.verify_backup(Path(first['backup']))
    assert not errors and saved['reread_verified']
    expected = ab_install.calibration_partitions(saved['layout'])
    assert {item['name'] for item in saved['items']} == set(expected)
    for name, part in expected.items():
        assert (Path(first['backup'])/f'{name}.img').read_bytes() == disk.read(part['first_sector'], part['sectors'])
    assert not (Path(first['backup'])/'full-disk.img').exists()
    assert not (Path(first['backup'])/'userdata.img').exists()
    with pytest.raises(oneclick.FlashStopped, match='先完成'):
        run(folder, disk, 'images', device=dict(DEVICE, serial='dead'))
    final = run(folder, disk, 'images')
    assert final['written'] == list(flashpack.AB_IMAGES)
    assert final['manual_reboot'] and final['done']
    for report_dir in final['reports']:
        assert not list(Path(report_dir).glob('preimage-*'))
    actual, _, _ = backup.read_layout(disk.read)
    assert actual == target
    for part in actual['partitions']:
        if part['name'] == 'RSH_AUDIO':
            start = part['first_sector'] * 512
            seed_bytes = (folder/'RSH_AUDIO.img').stat().st_size
            end = (part['last_sector'] + 1) * 512
            assert seed_bytes < end - start
            assert disk.data[start:start+seed_bytes] == (folder/'RSH_AUDIO.img').read_bytes()
            assert disk.data[start+seed_bytes:end] == source[start+seed_bytes:end]
        if part['name'] not in flashpack.AB_IMAGES:
            assert disk.range(part['first_sector'], part['last_sector']) == source[part['first_sector']*512:(part['last_sector']+1)*512]
    with pytest.raises(oneclick.FlashStopped, match='重新初始化'):
        run(folder, disk, 'prepare')



def test_signed_stock_migration_pins_source_and_activates_ledger_last(installable, tmp_path):
    disk, folder, _ = installable
    layout, _, _ = backup.read_layout(disk.read)
    source = flashpack.check_imported(folder)
    images = {name: folder / entry['file'] for name, entry in source['images'].items()}
    aboot = next(row for row in layout['partitions'] if row['name'] == 'aboot')
    aboot_image = tmp_path / 'aboot.img'
    aboot_image.write_bytes(b'b' * aboot['bytes'])
    images['aboot'] = aboot_image
    migration = {'source_gpt_sha256': layout['gpt_sha256'],
                 'source_aboot_sha256': source['aboot_sha256']}
    pack = tmp_path / 'migration.rshpack'
    flashpack.create_ab_pack(pack, 'A/B migration test', '2', 4,
                             hashlib.sha256(aboot_image.read_bytes()).hexdigest(), images,
                             tmp_path / 'signing.key', stock_migration=migration)
    imported = flashpack.import_pack(pack)

    start = aboot['first_sector'] * gpt.SECTOR
    original_aboot = disk.read(aboot['first_sector'], aboot['sectors'])
    disk.data[start] ^= 1
    with pytest.raises(oneclick.FlashStopped, match='现有引导程序'):
        run(imported, disk, 'prepare')
    disk.data[start] ^= 1

    assert run(imported, disk, 'prepare')['written'] == ['gpt']
    assert disk.read(aboot['first_sector'], aboot['sectors']) == original_aboot
    final = run(imported, disk, 'images')
    assert final['written'][-3:] == ['boot', 'aboot', 'RSH_BOOTCTL']
    assert disk.read(aboot['first_sector'], aboot['sectors']) == aboot_image.read_bytes()
    assert final['done']


def test_unstable_calibration_leaves_source_gpt_untouched(installable, monkeypatch):
    disk, folder, _ = installable
    before = bytes(disk.data)
    layout, _, _ = backup.read_layout(disk.read)
    part = ab_install.calibration_partitions(layout)['modemst2']
    read = disk.read
    calls = 0

    def unstable_read(start, count):
        nonlocal calls
        data = read(start, count)
        if start == part['first_sector']:
            calls += 1
            if calls == 2:
                return bytes([data[0] ^ 1]) + data[1:]
        return data

    monkeypatch.setattr(disk, 'read', unstable_read)
    with pytest.raises(backup.BackupError, match='modemst2.*不一致'):
        run(folder, disk, 'prepare')
    assert calls == 2
    assert bytes(disk.data) == before

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

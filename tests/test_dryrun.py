# SPDX-License-Identifier: GPL-3.0-only
import struct

import pytest

from usbtool import backup, dryrun, policy


@pytest.fixture
def backup_dir(disk, tmp_path):
    folder = backup.new_backup_dir(tmp_path / 'backups')
    backup.run_backup(disk.read, folder, ['boot', 'rootfs', 'NSHSTATE', backup.FULL_DISK], log=lambda m: None)
    return folder


def boot_image(tmp_path, size=4096):
    path = tmp_path / 'boot.img'
    path.write_bytes(b'ANDROID!' + bytes(size - 8))
    return path


def sparse_image(tmp_path, blocks, blk=4096, truncate=False):
    header = struct.pack('<I4H4I', 0xED26FF3A, 1, 0, 28, 12, blk, blocks, 2, 0)
    raw = struct.pack('<2H2I', 0xCAC1, 0, 1, 12 + blk) + bytes(blk)
    skip = struct.pack('<2H2I', 0xCAC3, 0, blocks - 1, 12)
    data = header + raw + skip
    path = tmp_path / 'rootfs.sparse.img'
    path.write_bytes(data[:-20] if truncate else data)
    return path


def test_valid_plan_passes(backup_dir, tmp_path):
    image = boot_image(tmp_path)
    report = dryrun.run_dryrun(backup_dir, [{'partition': 'boot', 'image': str(image), 'sha256': ''}])
    assert report['passed'], report['errors']
    assert report['writes_performed'] is False and len(report['plan_sha256']) == 64
    assert report['items'][0]['first_sector'] == 1300


def test_denied_partition_refused(backup_dir, tmp_path):
    image = boot_image(tmp_path)
    report = dryrun.run_dryrun(backup_dir, [{'partition': 'modem', 'image': str(image)}])
    assert not report['passed'] and any('禁写' in e for e in report['errors'])


@pytest.mark.parametrize('name', sorted(policy.DENIED_NAMES))
def test_every_denied_name_refused_even_case_changed(name):
    layout = {'partitions': [{'name': name.upper(), 'index': 1, 'first_sector': 34, 'last_sector': 40}]}
    assert policy.target_errors(name.upper(), layout)


def test_unlisted_partition_refused(backup_dir, tmp_path):
    report = dryrun.run_dryrun(backup_dir, [{'partition': 'sbl1', 'image': str(boot_image(tmp_path))}])
    assert not report['passed']


def test_oversize_image_refused(backup_dir, tmp_path):
    image = boot_image(tmp_path, size=256 * 512 + 512)
    report = dryrun.run_dryrun(backup_dir, [{'partition': 'boot', 'image': str(image)}])
    assert any('大于分区' in e for e in report['errors'])


def test_expected_digest_mismatch_refused(backup_dir, tmp_path):
    report = dryrun.run_dryrun(backup_dir, [{'partition': 'boot', 'image': str(boot_image(tmp_path)), 'sha256': '00' * 32}])
    assert any('摘要' in e for e in report['errors'])


def test_sparse_expanded_size_checked(backup_dir, tmp_path):
    ok = dryrun.run_dryrun(backup_dir, [{'partition': 'rootfs', 'image': str(sparse_image(tmp_path, 100))}])
    assert ok['passed'], ok['errors']
    assert ok['items'][0]['expanded_bytes'] == 100 * 4096 and ok['items'][0]['sparse']
    big = dryrun.run_dryrun(backup_dir, [{'partition': 'rootfs', 'image': str(sparse_image(tmp_path, 1000))}])
    assert any('大于分区' in e for e in big['errors'])


def test_truncated_sparse_refused(backup_dir, tmp_path):
    report = dryrun.run_dryrun(backup_dir, [{'partition': 'rootfs', 'image': str(sparse_image(tmp_path, 10, truncate=True))}])
    assert any('sparse' in e for e in report['errors'])


def test_missing_target_backup_refused(disk, tmp_path):
    folder = backup.new_backup_dir(tmp_path / 'b2')
    backup.run_backup(disk.read, folder, ['rootfs'], reread=False, log=lambda m: None)
    report = dryrun.run_dryrun(folder, [{'partition': 'boot', 'image': str(boot_image(tmp_path))}])
    assert any('写前备份' in e for e in report['errors'])
    assert any('全盘' in w for w in report['warnings'])


def test_broken_backup_refused(backup_dir, tmp_path):
    (backup_dir / 'boot.img').write_bytes(b'x')
    report = dryrun.run_dryrun(backup_dir, [{'partition': 'boot', 'image': str(boot_image(tmp_path))}])
    assert not report['passed'] and any('备份目录' in e for e in report['errors'])


def test_missing_manifest_refused(tmp_path):
    report = dryrun.run_dryrun(tmp_path, [{'partition': 'boot', 'image': 'x'}])
    assert not report['passed']


def test_duplicate_target_refused(backup_dir, tmp_path):
    image = str(boot_image(tmp_path))
    report = dryrun.run_dryrun(backup_dir, [{'partition': 'boot', 'image': image}, {'partition': 'boot', 'image': image}])
    assert any('重复' in e for e in report['errors'])


def test_format_and_save(backup_dir, tmp_path):
    report = dryrun.run_dryrun(backup_dir, [{'partition': 'boot', 'image': str(boot_image(tmp_path))}])
    text = dryrun.format_report(report)
    assert '未写入设备' in text
    assert dryrun.save_report(report, tmp_path / 'reports').is_file()

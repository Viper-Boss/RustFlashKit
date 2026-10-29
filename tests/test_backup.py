# SPDX-License-Identifier: GPL-3.0-only
import json

import pytest

from usbtool import backup


def make_folder(tmp_path):
    return backup.new_backup_dir(tmp_path / 'backups')


def test_backup_partitions_and_verify(disk, tmp_path, small_chunks):
    folder = make_folder(tmp_path)
    seen = []
    manifest = backup.run_backup(disk.read, folder, ['boot', 'modem', backup.FULL_DISK],
                                 on_disk_sectors=seen.append, log=lambda m: None)
    assert manifest['passed'] is True and seen == [8192]
    assert (folder / 'boot.img').read_bytes() == disk.range(1300, 1555)
    assert (folder / 'modem.img').read_bytes() == disk.range(100, 1099)
    assert (folder / 'full-disk.img').read_bytes() == bytes(disk.data)
    assert all(item['status'] == 'verified' and item['reread_sha256'] == item['sha256'] for item in manifest['items'])
    loaded, errors = backup.verify_backup(folder)
    assert errors == [] and loaded['layout']['disk_sectors'] == 8192


def test_reread_mismatch_fails_and_keeps_partial(disk, tmp_path):
    folder = make_folder(tmp_path)
    disk.flip_after = 3  # 两次 GPT 读取 + 第一遍分区读取正常，回读时变化
    with pytest.raises(backup.BackupError, match='不一致'):
        backup.run_backup(disk.read, folder, ['boot'], log=lambda m: None)
    assert not (folder / 'boot.img').exists() and (folder / 'boot.img.partial').exists()
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['passed'] is False and manifest['items'][0]['status'] == 'failed_reread'
    _m, errors = backup.verify_backup(folder)
    assert errors


def test_short_read_rejected(disk, tmp_path):
    disk.short = True
    with pytest.raises(backup.BackupError, match='长度'):
        backup.run_backup(disk.read, make_folder(tmp_path), ['boot'], log=lambda m: None)


def test_unknown_partition_rejected(disk, tmp_path):
    with pytest.raises(backup.BackupError, match='没有'):
        backup.run_backup(disk.read, make_folder(tmp_path), ['nosuch'], log=lambda m: None)


def test_non_empty_folder_rejected(disk, tmp_path):
    folder = make_folder(tmp_path)
    (folder / 'x').write_text('old')
    with pytest.raises(backup.BackupError, match='空目录'):
        backup.run_backup(disk.read, folder, ['boot'], log=lambda m: None)


def test_verify_detects_tampered_image(disk, tmp_path):
    folder = make_folder(tmp_path)
    backup.run_backup(disk.read, folder, ['boot'], reread=False, log=lambda m: None)
    data = bytearray((folder / 'boot.img').read_bytes())
    data[100] ^= 1
    (folder / 'boot.img').write_bytes(bytes(data))
    _m, errors = backup.verify_backup(folder)
    assert any('摘要' in e for e in errors)


def test_verify_detects_tampered_gpt(disk, tmp_path):
    folder = make_folder(tmp_path)
    backup.run_backup(disk.read, folder, ['boot'], reread=False, log=lambda m: None)
    data = bytearray((folder / 'gpt-backup.bin').read_bytes())
    data[5] ^= 1
    (folder / 'gpt-backup.bin').write_bytes(bytes(data))
    _m, errors = backup.verify_backup(folder)
    assert any('GPT' in e for e in errors)


def test_new_backup_dirs_are_unique(tmp_path):
    a, b = make_folder(tmp_path), make_folder(tmp_path)
    assert a != b and a.is_dir() and b.is_dir()

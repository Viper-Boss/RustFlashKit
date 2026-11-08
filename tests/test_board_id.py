# SPDX-License-Identifier: GPL-3.0-only
import pytest

from usbtool import backup, board_id, gpt

FIRST, SECTORS = 100, 2000
LAYOUT = {'partitions': [{'name': 'modem', 'first_sector': FIRST, 'sectors': SECTORS}]}


class Disk:
    def __init__(self, tag=None, at=700, flip_on_read=None):
        self.data = bytearray((FIRST + SECTORS) * 512)
        self.at = FIRST * 512 + at
        self.reads = 0
        self.flip_on_read = flip_on_read
        if tag:
            self.data[self.at:self.at + len(tag)] = tag

    def read(self, start, count):
        self.reads += 1
        chunk = bytearray(self.data[start * 512:(start + count) * 512])
        if self.reads == self.flip_on_read:
            offset = self.at - start * 512
            if 0 <= offset < len(chunk):
                chunk[offset + 4] ^= 1
        return bytes(chunk)


@pytest.mark.parametrize('tag,family,name,date', [
    (b'UFI003_CT 20211210\0', 'UFI003', 'UFI003_CT', '20211210'),
    (b'UFI001CT 20211106\0', 'UFI001C', 'UFI001CT', '20211106'),
])
def test_real_tags(tag, family, name, date):
    result = board_id.identify(Disk(tag).read, LAYOUT)
    assert (result['family'], result['tag'], result['date']) == (family, name, date)
    assert family in board_id.describe(result)


def test_unknown_tag_is_not_guessed():
    result = board_id.identify(Disk(b'UFI999X 20220101\0').read, LAYOUT)
    assert result['family'] is None and result['tag'] == 'UFI999X' and '未知' in board_id.describe(result)


def test_no_tag():
    result = board_id.identify(Disk().read, LAYOUT)
    assert result['family'] is None and '没有找到' in result['note']


def test_conflicting_tags_refused():
    disk = Disk(b'UFI003_CT 20211210\0')
    disk.data[disk.at + 4096:disk.at + 4096 + 18] = b'UFI001CT 20211106\0'
    result = board_id.identify(disk.read, LAYOUT)
    assert result['family'] is None and '多个' in result['note']


def test_unstable_read_of_tag_refused():
    # 扫描时读到的标签，重读时翻转：不采信
    disk = Disk(b'UFI003_CT 20211210\0', flip_on_read=2)
    assert SECTORS <= backup.CHUNK_SECTORS  # 扫描只读一次，第二次读取就是重读
    result = board_id.identify(disk.read, LAYOUT)
    assert result['family'] is None and '不一致' in result['note']


def test_tag_across_chunk_boundary(monkeypatch):
    monkeypatch.setattr(backup, 'CHUNK_SECTORS', 8)
    at = 8 * 512 - 5
    result = board_id.identify(Disk(b'UFI001CT 20211106\0', at=at).read, LAYOUT)
    assert result['family'] == 'UFI001C'


def test_no_modem_partition():
    assert board_id.identify(Disk().read, {'partitions': []})['family'] is None


def test_tag_in_real_context():
    # 真实上下文：标签前后都是二进制，后面紧跟 NUL
    blob = b'Nov 04 2016\0\0\0\0\x0002:00:00\0\0\0\0\0\0\0\0UFI003_CT 20211210\0\0d\0\0\0\xfe\x0f'
    disk = Disk(blob)
    assert board_id.identify(disk.read, LAYOUT)['family'] == 'UFI003'
    assert gpt.SECTOR == 512

# SPDX-License-Identifier: GPL-3.0-only
"""识别棒子型号。只读。

分区表、MSM_ID、PK_HASH 在 UFI003 和 UFI001C 上完全相同，分不出来。可靠的依据是 modem 分区里的出厂固件标签，
实测为 "UFI003_CT 20211210" 和 "UFI001CT 20211106"。刷 RustSimHub 不写 modem，所以刷完仍可识别。
这个标签说明的是棒子出厂时写入的基带固件；如果有人换过基带固件，它反映的是固件而不是电路板。
"""
import re

from . import backup, gpt

TAG = re.compile(rb'(UFI[0-9A-Z_]{3,16}) (20\d{6})\0')
FAMILIES = (('UFI003', 'UFI003'), ('UFI001C', 'UFI001C'))  # 标签前缀 -> 型号
TAG_SPAN = 64  # 跨块时的重叠字节数


def family_of(tag):
    for prefix, family in FAMILIES:
        if tag.startswith(prefix):
            return family
    return None


def _result(family, tag=None, date=None, note=None):
    return {'family': family, 'tag': tag, 'date': date, 'note': note}


def scan(read, first, sectors, progress=None):
    """流式扫描一段扇区，返回 [(绝对字节偏移, 标签字节)]。不落盘。"""
    found = []
    done = 0
    tail = b''
    while done < sectors:
        n = min(backup.CHUNK_SECTORS, sectors - done)
        data = backup.checked_read(read, first + done, n)
        window = tail + data
        base = (first + done) * gpt.SECTOR - len(tail)
        for match in TAG.finditer(window):
            found.append((base + match.start(), match.group(0)))
        tail = data[-TAG_SPAN:]
        done += n
        if progress:
            progress('识别型号', done, sectors)
    return sorted(set(found))


def identify(read, layout, progress=None):
    """返回 {'family': 'UFI003'|'UFI001C'|None, 'tag', 'date', 'note'}。认不准就是 None，绝不猜。"""
    modem = [p for p in layout['partitions'] if p['name'] == 'modem']
    if len(modem) != 1:
        return _result(None, note='分区表里没有 modem 分区，无法识别型号')
    modem = modem[0]
    found = scan(read, modem['first_sector'], modem['sectors'], progress)
    tags = {tag for _offset, tag in found}
    if not tags:
        return _result(None, note='modem 分区里没有找到型号标签（可能换过基带固件，或这处读错了）')
    if len(tags) > 1:
        return _result(None, note='modem 分区里有多个不同的型号标签：' + '、'.join(sorted(t[:-1].decode() for t in tags)))
    offset, tag = found[0]
    # 重读两遍所在扇区：有的棒子读数会翻转，翻一位就可能认错型号
    start = offset // gpt.SECTOR
    count = (offset + len(tag) - 1) // gpt.SECTOR - start + 1
    skip = offset - start * gpt.SECTOR
    for _ in range(2):
        again = backup.checked_read(read, start, count)
        if again[skip:skip + len(tag)] != tag:
            return _result(None, note='型号标签两次读取不一致，这根棒子可能读不稳')
    match = TAG.match(tag)
    name, date = match.group(1).decode(), match.group(2).decode()
    family = family_of(name)
    return _result(family, name, date, None if family else f'固件标签 {name} 不是已知型号')


def describe(board):
    if board.get('family'):
        return f'{board["family"]}（固件标签 {board["tag"]} {board["date"]}）'
    return '未知' + (f'：{board["note"]}' if board.get('note') else '')

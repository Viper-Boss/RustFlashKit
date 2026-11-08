# SPDX-License-Identifier: GPL-3.0-only
"""Android sparse 镜像头解析：只计算展开后大小并检查块结构，不展开数据。"""
import os
import struct

MAGIC = 0xED26FF3A
CHUNK_RAW, CHUNK_FILL, CHUNK_DONT_CARE, CHUNK_CRC32 = 0xCAC1, 0xCAC2, 0xCAC3, 0xCAC4


class SparseError(ValueError):
    pass


def is_sparse(path):
    with open(path, 'rb') as stream:
        head = stream.read(4)
    return len(head) == 4 and struct.unpack('<I', head)[0] == MAGIC


def expanded_size(path):
    """返回展开后的字节数；结构不一致时抛 SparseError。"""
    with open(path, 'rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        header = stream.read(28)
        if len(header) != 28:
            raise SparseError('sparse 头不完整')
        magic, major, _minor, file_hdr, chunk_hdr, blk_sz, total_blks, total_chunks, _crc = struct.unpack('<I4H4I', header)
        if magic != MAGIC or major != 1 or file_hdr < 28 or chunk_hdr < 12 or blk_sz == 0 or blk_sz % 4:
            raise SparseError('sparse 头字段无效')
        stream.seek(file_hdr)
        blocks = 0
        for _ in range(total_chunks):
            raw = stream.read(chunk_hdr)
            if len(raw) != chunk_hdr:
                raise SparseError('sparse 块头不完整')
            kind, _reserved, chunk_blks, total_sz = struct.unpack_from('<2H2I', raw)
            body = total_sz - chunk_hdr
            expected = {CHUNK_RAW: chunk_blks * blk_sz, CHUNK_FILL: 4, CHUNK_DONT_CARE: 0, CHUNK_CRC32: 4}.get(kind)
            if expected is None or body != expected:
                raise SparseError(f'sparse 块类型或长度无效（0x{kind:04x}）')
            stream.seek(body, 1)
            if stream.tell() > size:
                raise SparseError('sparse 数据被截断')
            if kind != CHUNK_CRC32:
                blocks += chunk_blks
        if stream.tell() != size:
            raise SparseError('sparse 文件末尾有多余数据')
        if blocks != total_blks:
            raise SparseError('sparse 块数与头部声明不一致')
    return total_blks * blk_sz

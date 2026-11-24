# SPDX-License-Identifier: GPL-3.0-only
"""Derive an A/B GPT from a validated device GPT without moving preserved entries."""
import struct
import uuid
import zlib

from . import gpt, gpt_plan, storage_layout

BOOT_BYTES = 64 << 20


def build(primary, backup, board):
    disk = gpt.disk_sectors_from_primary(primary)
    source = gpt.parse_gpt(primary, backup, disk)
    header = gpt.parse_header(primary[gpt.SECTOR:2 * gpt.SECTOR], 1)
    geometry = dict(source, first_usable=header["first_usable"], last_usable=header["last_usable"],
                    entry_count=header["count"])
    if not any(p["name"] == "boot" and p["bytes"] == BOOT_BYTES for p in source["partitions"]):
        raise storage_layout.LayoutError("A/B 安装需要原位 64 MiB boot 分区")
    if any(struct.unpack_from("<I", raw, offset)[0] != 92
           for raw, offset in ((primary, gpt.SECTOR + 12), (backup, len(backup) - gpt.SECTOR + 12))):
        raise storage_layout.LayoutError("A/B 安装需要已知 GPT 头格式")

    plan = storage_layout.plan_layout(geometry, board)
    entries = bytearray(32 * gpt.SECTOR)

    def row_slice(index):
        return slice((index - 1) * 128, index * 128)

    used_ids = set()
    for row in plan["preserved_partitions"]:
        span = row_slice(row["index"])
        raw = primary[2 * gpt.SECTOR + span.start:2 * gpt.SECTOR + span.stop]
        unique = raw[16:32]
        if unique in used_ids or unique == bytes(16):
            raise storage_layout.LayoutError("保留分区 GUID 重复或缺失")
        used_ids.add(unique)
        entries[span] = raw
    namespace = uuid.UUID(bytes_le=header["disk_guid"])
    for row in plan["new_partitions"]:
        name = row["name"]
        value = uuid.uuid5(namespace, f"RustSimHub A/B v1 {name}").bytes_le
        if value in used_ids:
            raise storage_layout.LayoutError("目标分区 GUID 冲突")
        used_ids.add(value)
        entries[row_slice(row["index"])] = (
            gpt_plan.LINUX_DATA.bytes_le + value
            + struct.pack("<QQQ", row["first_sector"], row["last_sector"], 0)
            + name.encode("utf-16-le").ljust(72, b"\0")
        )
    length = plan["entry_count"] * 128
    crc = zlib.crc32(entries[:length])

    def make_header(stock, current, alternate, table_lba):
        fixed = bytearray(stock[:92])
        struct.pack_into("<I", fixed, 80, plan["entry_count"])
        return gpt_plan._header(fixed, current, alternate, table_lba, crc)

    primary_header = make_header(primary[gpt.SECTOR:2 * gpt.SECTOR], 1, disk - 1, 2)
    backup_header = make_header(backup[-gpt.SECTOR:], disk - 1, 1, disk - gpt.BACKUP_SECTORS)
    new_primary = primary[:gpt.SECTOR] + primary_header + bytes(entries)
    new_backup = bytes(entries) + backup_header
    target = gpt.parse_gpt(new_primary, new_backup, disk)
    return new_primary, new_backup, target, plan["profile_gb"]

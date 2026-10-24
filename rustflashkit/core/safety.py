"""分区安全策略。

- BOOTLOADER_CHAIN：刷错可能变砖（需进 9008 救砖），二次确认。
- EFS_DANGER：modemst/EFS 相关，刷写会丢失 IMEI/网络校准数据，默认硬禁用。
"""
from __future__ import annotations

import re

# 合法 fastboot 分区名：字母数字、下划线、点、短横线，长度 1..36
PARTITION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,36}$")

# 引导链分区：失败=变砖风险，必须在确认对话框中逐个点名
BOOTLOADER_CHAIN = {
    "sbl1", "sbl1bak", "sbl2", "sbl3", "rpm", "rpmbak", "tz", "tzbak",
    "hyp", "hypbak", "aboot", "abootbak", "ddr", "spl", "xbl", "abl",
}

# EFS/校准分区：刷写会丢 IMEI，无备份不可恢复——默认直接拒绝
EFS_DANGER = {
    "modemst1", "modemst2", "fsc", "fsg", "ssd", "persistent",
    "devinfo", "efs", "m9kefs1", "m9kefs2", "m9kefs3", "config",
}


class PartitionBlocked(Exception):
    """该分区被安全策略禁止刷写。"""


def validate_partition_name(name: str) -> str | None:
    """返回错误信息；None 表示合法。"""
    if not name or not name.strip():
        return "分区名不能为空"
    if not PARTITION_RE.match(name.strip()):
        return "分区名只能包含字母/数字/._- 且长度不超过 36"
    return None


def assess(partition: str) -> str | None:
    """评估分区风险等级。

    返回 None（普通）、'bootloader'（引导链，需强化确认）或 'efs'（默认禁止）。
    """
    p = (partition or "").strip().lower()
    if p in EFS_DANGER:
        return "efs"
    if p in BOOTLOADER_CHAIN:
        return "bootloader"
    return None


def check_flashable(partition: str, allow_bootloader: bool = False) -> None:
    """刷写前的强制校验，不通过抛 PartitionBlocked。"""
    err = validate_partition_name(partition)
    if err:
        raise PartitionBlocked(f"非法分区名 {partition!r}: {err}")
    level = assess(partition)
    if level == "efs":
        raise PartitionBlocked(
            f"分区 {partition} 属于 EFS/校准数据（含 IMEI），刷写可能永久丢失基带信息，工具默认禁止。"
            "如确有必要请手工使用 fastboot。"
        )
    if level == "bootloader" and not allow_bootloader:
        raise PartitionBlocked(
            f"分区 {partition} 属于引导链，刷错将变砖（需 9008 救砖）。"
            "需在模块设置中显式允许引导链刷写并二次确认。"
        )

# SPDX-License-Identifier: GPL-3.0-only
"""硬编码的分区写入策略。没有配置文件或参数可以放宽它。"""

# 只有这些系统分区可以成为刷写目标（演练或将来的真实写入）。
ALLOWED_TARGETS = frozenset({'boot', 'rootfs', 'NSHSTATE'})

# 基带/NV/校准/引导链等分区：永远拒绝，名字比较不区分大小写。
DENIED_NAMES = frozenset(name.lower() for name in (
    'modem', 'fsg', 'modemst1', 'modemst2', 'fsc', 'sec', 'persist', 'DDR', 'ssd',
    'sbl1', 'sbl1bak', 'rpm', 'rpmbak', 'tz', 'tzbak', 'hyp', 'hypbak',
    'aboot', 'abootbak', 'devinfo', 'config', 'keystore', 'oem', 'misc', 'splash',
))

# MSM8916（骁龙 410）的 Sahara MSM_ID。
MSM8916_ID = 0x7050E1

# 已知可信的 Firehose 加载器（RustSimHub 在本板 9008 只读捕获中使用过）。
KNOWN_LOADERS = {
    '959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14': 'UFI003 prog_emmc_firehose_8916.mbn（RustSimHub 实机只读使用过）',
}

# RustSimHub 记录的 UFI003 布局：分区序号 -> (名字, 字节数)。只用于提示，不作为写入许可。
UFI003_LAYOUT = {12: ('boot', 67108864), 14: ('rootfs', 3436183552), 15: ('NSHSTATE', 135249408)}


def is_denied(name):
    return name.lower() in DENIED_NAMES


def target_errors(name, layout):
    """返回把 name 当作写入目标时的拒绝原因列表（空列表表示策略允许）。"""
    errors = []
    if is_denied(name):
        errors.append(f'{name} 在硬编码禁写名单中（基带/NV/校准/引导链）')
    if name not in ALLOWED_TARGETS:
        errors.append(f'{name} 不在允许刷写的系统分区名单 {sorted(ALLOWED_TARGETS)} 中')
    matches = [p for p in layout['partitions'] if p['name'] == name]
    if len(matches) != 1:
        errors.append(f'分区表中 {name} 出现 {len(matches)} 次，需要恰好 1 次')
        return errors
    target = matches[0]
    for other in layout['partitions']:
        if other is target:
            continue
        overlap = not (other['last_sector'] < target['first_sector'] or other['first_sector'] > target['last_sector'])
        if overlap:
            errors.append(f'{name} 的扇区范围与 {other["name"]} 重叠')
    return errors


def ufi003_warnings(layout):
    """与 RustSimHub 记录的 UFI003 布局比较，返回不一致的提示。"""
    by_index = {p['index']: p for p in layout['partitions']}
    warnings = []
    for index, (name, size) in UFI003_LAYOUT.items():
        actual = by_index.get(index)
        if actual is None or actual['name'] != name or actual['bytes'] != size:
            warnings.append(f'分区 {index} 不是预期的 {name}（{size} 字节），这可能不是 RustSimHub 记录过的那块 UFI003')
    return warnings

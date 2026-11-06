# SPDX-License-Identifier: GPL-3.0-only
"""读取并解读 eMMC 寿命（JEDEC EXT_CSD 的 DEVICE_LIFE_TIME_EST_TYP_A/B 和 PRE_EOL_INFO）。

两条只读通道：
- 开机状态：经维护 SSH 执行一条固定命令，读 Linux sysfs。不接受任意命令，只用公钥登录，
  必须提供 known_hosts 严格核对主机，不做首次自动信任。
- 9008 状态：Firehose getstorageinfo。标准 Firehose 没有读 EXT_CSD 的命令，能否拿到取决于加载器；
  UFI003 用的 8916 加载器实测不支持。工具不会为此读写芯片寄存器。
"""
import json
import os
import re
import subprocess

SYSFS_DIR = '/sys/block/mmcblk0/device'
SYSFS_FIELDS = ('type', 'name', 'manfid', 'rev', 'life_time', 'pre_eol_info')  # 不读 cid/serial 等设备标识
REMOTE_COMMAND = ('for f in ' + ' '.join(SYSFS_FIELDS) + '; do printf "%s=" "$f"; '
                  f'cat "{SYSFS_DIR}/$f" 2>/dev/null || echo; done')
SSH_TIMEOUT = 25
HOST = re.compile(r'^(?:[A-Za-z_][A-Za-z0-9_-]{0,31}@)?[A-Za-z0-9][A-Za-z0-9.:-]{0,253}$')
EOL_NAMES = {0: ('未定义', 'unknown'), 1: ('正常', 'ok'), 2: ('警告：保留块已用掉 80%', 'warning'),
             3: ('紧急：保留块已用掉 90%', 'critical')}
VERDICTS = ('unknown', 'ok', 'warning', 'critical')
VERDICT_NAMES = {'unknown': '无法判断', 'ok': '正常', 'warning': '注意', 'critical': '危险'}


class LifeError(RuntimeError):
    pass


def parse_code(text):
    """把 0x01 或 1 这样的文本转成整数；无效返回 None。"""
    try:
        value = int(str(text).strip(), 0)
    except ValueError:
        return None
    return value if 0 <= value <= 255 else None


def decode_life(code):
    """JEDEC：0x00 未定义，0x01–0x0A 每档 10%，0x0B 超过标称寿命。返回 (说明, 判定)。"""
    if code is None or code == 0:
        return '芯片未提供', 'unknown'
    if 1 <= code <= 10:
        verdict = 'ok' if code <= 7 else ('warning' if code <= 9 else 'critical')
        return f'已用 {(code - 1) * 10}%–{code * 10}%', verdict
    if code == 11:
        return '已超过标称寿命', 'critical'
    return f'保留值 0x{code:02x}', 'unknown'


def decode_eol(code):
    if code is None:
        return '芯片未提供', 'unknown'
    return EOL_NAMES.get(code, (f'保留值 0x{code:02x}', 'unknown'))


def summarize(life_a, life_b, eol, source, extra=None):
    """三个原始代码（整数或 None）-> 报告字典。总体判定取其中最严重的一项。"""
    a_text, a_verdict = decode_life(life_a)
    b_text, b_verdict = decode_life(life_b)
    eol_text, eol_verdict = decode_eol(eol)
    verdict = max((a_verdict, b_verdict, eol_verdict), key=VERDICTS.index)
    return {'source': source, 'available': any(v != 'unknown' for v in (a_verdict, b_verdict, eol_verdict)),
            'verdict': verdict, 'life_type_a': life_a, 'life_type_a_text': a_text, 'life_type_b': life_b,
            'life_type_b_text': b_text, 'pre_eol': eol, 'pre_eol_text': eol_text, **(extra or {})}


def parse_sysfs(output):
    """解析固定命令的输出（key=value 行）。内核的 life_time 格式为 "0x01 0x02"（A 类 B 类）。"""
    fields = {}
    for line in output.splitlines():
        key, sep, value = line.partition('=')
        if sep and key in SYSFS_FIELDS and key not in fields:
            fields[key] = value.strip()
    if not fields:
        raise LifeError('设备没有返回预期的内容')
    if fields.get('type') and fields['type'].upper() != 'MMC':
        raise LifeError(f'mmcblk0 不是 eMMC（类型 {fields["type"]}）')
    parts = fields.get('life_time', '').split()
    life_a = parse_code(parts[0]) if len(parts) == 2 else None
    life_b = parse_code(parts[1]) if len(parts) == 2 else None
    eol = parse_code(fields['pre_eol_info']) if fields.get('pre_eol_info') else None
    extra = {'chip': {k: fields[k] for k in ('name', 'manfid', 'rev') if fields.get(k)}}
    report = summarize(life_a, life_b, eol, 'ssh-sysfs', extra)
    if not report['available']:
        report['note'] = '内核没有提供寿命字段，或这颗 eMMC 早于 5.0 标准、不支持寿命上报'
    return report


def read_over_ssh(host, identity, known_hosts, run=subprocess.run):
    """host 形如 root@10.55.0.2。只执行固定命令 REMOTE_COMMAND。"""
    if not HOST.match(host or ''):
        raise LifeError('设备地址格式无效')
    for label, path in (('私钥', identity), ('known_hosts', known_hosts)):
        if not path or not os.path.isfile(path):
            raise LifeError(f'{label}文件不存在：{path}')
    command = ['ssh', '-i', identity, '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={known_hosts}',
               '-o', 'GlobalKnownHostsFile=none', '-o', 'ClearAllForwardings=yes', '-o', 'ConnectTimeout=8',
               '-F', 'none', '-T', host, REMOTE_COMMAND]
    try:
        done = run(command, capture_output=True, encoding='utf-8', errors='replace', timeout=SSH_TIMEOUT,
                   stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except FileNotFoundError:
        raise LifeError('找不到 ssh 命令（Windows 可在“可选功能”里安装 OpenSSH 客户端）') from None
    except subprocess.TimeoutExpired:
        raise LifeError(f'SSH 超过 {SSH_TIMEOUT} 秒没有结果') from None
    if done.returncode != 0:
        detail = (done.stderr or '').strip().splitlines()
        raise LifeError('SSH 失败：' + (detail[-1] if detail else f'退出码 {done.returncode}'))
    return parse_sysfs(done.stdout)


def _find_life(fields, letter):
    for key, value in fields.items():
        lowered = str(key).lower().replace('-', '_')
        if 'life' in lowered and re.search(rf'(?:typ|type|_)_?{letter}(?:$|_)', lowered):
            return value
    return None


def _find_eol(fields):
    for key, value in fields.items():
        if 'eol' in str(key).lower():
            return value
    return None


def from_firehose(result):
    """解读 edl cmd_getstorageinfo 的返回值。只取寿命相关字段，不保留序列号等其他内容。"""
    if not isinstance(result, tuple) or len(result) != 2 or result[1] is None:
        return {'source': 'firehose', 'supported': False, 'available': False, 'verdict': 'unknown',
                'note': '这个加载器不支持 getstorageinfo，9008 下读不到寿命。请在棒子开机后用 SSH 方式读取'}
    value, response = result
    fields = {}
    data = getattr(response, 'data', None)
    if isinstance(data, dict):
        fields.update(data)
    if isinstance(value, str):
        try:
            info = json.loads(value.replace('INFO:', '')).get('storage_info')
        except (ValueError, AttributeError):
            info = None
        if isinstance(info, dict):
            fields.update(info)
    raw_a, raw_b, raw_eol = _find_life(fields, 'a'), _find_life(fields, 'b'), _find_eol(fields)
    report = summarize(None if raw_a is None else parse_code(raw_a), None if raw_b is None else parse_code(raw_b),
                       None if raw_eol is None else parse_code(raw_eol), 'firehose', {'supported': True})
    if not report['available']:
        report['note'] = '加载器支持 getstorageinfo，但没有返回寿命字段。请在棒子开机后用 SSH 方式读取'
    return report


def format_report(report):
    lines = [f'总体判定：{VERDICT_NAMES[report["verdict"]]}（来源：{report["source"]}）']
    if report.get('chip'):
        lines.append('芯片：' + '  '.join(f'{k}={v}' for k, v in report['chip'].items()))
    if report.get('available'):
        lines += [f'A 类存储寿命：{report["life_type_a_text"]}', f'B 类存储寿命：{report["life_type_b_text"]}',
                  f'保留块状态：{report["pre_eol_text"]}',
                  '寿命值由芯片自己估算，只分 10% 一档；各厂商的 A/B 类含义不完全相同。']
    if report.get('note'):
        lines.append(report['note'])
    return '\n'.join(lines)

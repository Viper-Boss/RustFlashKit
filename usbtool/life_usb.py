# SPDX-License-Identifier: GPL-3.0-only
"""一键读取 eMMC 寿命，全部只读。

- 一键（USB）：刷好 RustSimHub 的棒子开机后，经 USB 网卡（10.55.0.2）和维护 SSH 读 sysfs。自动找维护私钥；
  按 USB 序列号分别记住每根棒子的主机公钥：第一次连接某根棒子时记下（只在 USB 直连线上发生），
  以后公钥变了就拒绝。
- 原厂棒子（adb）：刷机前在原厂系统里读 sysfs，没有时读 debugfs 里的 EXT_CSD。只执行固定的 cat 命令。
"""
import os
import re
import shutil
import subprocess

from . import emmc_life, paths
from .emmc_life import LifeError

USB_HOST = 'root@10.55.0.2'
GADGET = re.compile(r'^USB\\VID_1D6B&PID_0104\\([0-9A-Fa-f]{32})$')  # RustSimHub USB 网卡；序列号每根棒子首启随机生成
KEY_DIR = re.compile(r'^maintenance-bootstrap-[0-9a-z-]+$')
KNOWN_HOSTS_NAME = 'usb-known_hosts'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _run(run, command, timeout):
    return run(command, capture_output=True, encoding='utf-8', errors='replace', timeout=timeout,
               stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)


def usb_stick_serials(run=subprocess.run):
    """Windows 上当前插着的 RustSimHub 棒子的 USB 序列号（只读查询即插即用设备列表）。"""
    script = ("Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
              "Where-Object { $_.InstanceId -like 'USB\\VID_1D6B&PID_0104\\*' } | ForEach-Object { $_.InstanceId }")
    try:
        done = _run(run, ['powershell', '-NoProfile', '-NonInteractive', '-Command', script], 20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        raise LifeError('无法查询 USB 设备列表') from None
    found = {m.group(1).lower() for m in (GADGET.match(l.strip()) for l in (done.stdout or '').splitlines()) if m}
    return sorted(found)


def default_identity():
    """维护私钥：程序目录往上三层内 RustSimHub/.private/maintenance-bootstrap-*/id_ed25519，取最新。"""
    base = paths.app_dir()
    keys = []
    for parent in [base, *list(base.parents)[:3]]:
        private = parent / 'RustSimHub' / '.private'
        try:
            keys += [d / 'id_ed25519' for d in private.iterdir() if KEY_DIR.match(d.name) and (d / 'id_ed25519').is_file()]
        except OSError:
            continue
    return str(max(keys, key=lambda p: p.stat().st_mtime)) if keys else None


def _host_known(known_hosts, alias, run):
    if not os.path.isfile(known_hosts):
        return False
    try:
        done = _run(run, ['ssh-keygen', '-F', alias, '-f', known_hosts], 10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and bool((done.stdout or '').strip())


def ssh_command(identity, known_hosts, alias):
    return ['ssh', '-i', identity, '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={known_hosts}',
            '-o', f'HostKeyAlias={alias}', '-o', 'GlobalKnownHostsFile=none', '-o', 'ClearAllForwardings=yes',
            '-o', 'ConnectTimeout=8', '-F', 'none', '-T', USB_HOST, emmc_life.REMOTE_COMMAND]


def read_over_usb(identity=None, run=subprocess.run, serials=None, known_hosts=None):
    identity = identity or default_identity()
    if not identity or not os.path.isfile(identity):
        raise LifeError('没有找到维护私钥（RustSimHub/.private/maintenance-bootstrap-*/id_ed25519），请用下面的手动方式')
    found = usb_stick_serials(run) if serials is None else serials
    if not found:
        raise LifeError('没有找到 USB 连接的 RustSimHub 棒子。请确认棒子已刷好、已开机，并用 USB 线连到电脑')
    if len(found) > 1:
        raise LifeError(f'同时插着 {len(found)} 根 RustSimHub 棒子，请只留一根')
    known_hosts = known_hosts or str(paths.local_dir() / KNOWN_HOSTS_NAME)
    alias = 'rustsimhub-usb-' + found[0]
    first = not _host_known(known_hosts, alias, run)
    try:
        done = _run(run, ssh_command(identity, known_hosts, alias), emmc_life.SSH_TIMEOUT)
    except FileNotFoundError:
        raise LifeError('找不到 ssh 命令（Windows 可在“可选功能”里安装 OpenSSH 客户端）') from None
    except subprocess.TimeoutExpired:
        raise LifeError(f'SSH 超过 {emmc_life.SSH_TIMEOUT} 秒没有结果') from None
    if done.returncode != 0:
        detail = (done.stderr or '').strip().splitlines()
        text = detail[-1] if detail else f'退出码 {done.returncode}'
        if 'IDENTIFICATION HAS CHANGED' in (done.stderr or '') or 'Host key verification failed' in text:
            text += ('（这根棒子的主机公钥和上次记下的不同，已拒绝。重刷过的棒子会这样；'
                     f'确认是重刷后，可删掉 .local/{KNOWN_HOSTS_NAME} 里 {alias} 那一行）')
        raise LifeError('SSH 失败：' + text)
    report = emmc_life.parse_sysfs(done.stdout)
    report['source'] = 'usb-ssh-sysfs'
    report['host_key_recorded_now'] = first
    if first:
        note = '第一次连接这根棒子，已在 USB 直连线上记下它的主机公钥；以后公钥变了会拒绝连接。'
        report['note'] = report['note'] + '\n' + note if report.get('note') else note
    return report


# ---------- 原厂系统经 adb ----------
EXT_CSD_PRE_EOL, EXT_CSD_LIFE_A, EXT_CSD_LIFE_B = 267, 268, 269
ADB_COMMAND = (emmc_life.REMOTE_COMMAND + '; for e in /sys/kernel/debug/mmc0/mmc0:*/ext_csd; do '
               'printf "ext_csd="; cat "$e" 2>/dev/null; echo; break; done')


def find_adb():
    for candidate in (shutil.which('adb'), os.path.join(os.path.expanduser('~'), 'platform-tools', 'adb.exe'),
                      os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Android', 'Sdk', 'platform-tools', 'adb.exe')):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def parse_adb(output):
    """sysfs 有寿命字段就用它；原厂老内核没有时，解读 debugfs 里的 EXT_CSD（512 字节的十六进制文本）。"""
    plain = '\n'.join(l for l in output.splitlines() if not l.startswith('ext_csd='))
    try:
        report = emmc_life.parse_sysfs(plain)
    except LifeError:
        report = None
    if report and report['available']:
        report['source'] = 'adb-sysfs'
        return report
    raw = next((l[len('ext_csd='):].strip() for l in output.splitlines() if l.startswith('ext_csd=')), '')
    if re.fullmatch(r'[0-9a-fA-F]{1024}', raw):
        data = bytes.fromhex(raw)
        extra = {'chip': report['chip']} if report and report.get('chip') else None
        result = emmc_life.summarize(data[EXT_CSD_LIFE_A], data[EXT_CSD_LIFE_B], data[EXT_CSD_PRE_EOL],
                                     'adb-ext_csd', extra)
        if not result['available']:
            result['note'] = '这颗 eMMC 没有提供寿命字段（可能早于 5.0 标准）'
        return result
    if report:
        report['source'] = 'adb-sysfs'
        report['note'] = '原厂系统没有提供寿命字段，也读不到 EXT_CSD（可能没有 root 权限或没有挂载 debugfs）'
        return report
    raise LifeError('原厂系统没有返回预期的内容')


def read_over_adb(run=subprocess.run, adb=None):
    """原厂棒子正常开机、开着 adb 时读一次寿命。只执行固定的只读命令。"""
    adb = adb or find_adb()
    if not adb:
        raise LifeError('找不到 adb（可把 platform-tools 放在用户目录下）')

    def call(*args):
        try:
            return _run(run, [adb, *args], 20)
        except subprocess.TimeoutExpired:
            raise LifeError('adb 没有响应') from None
    listed = call('devices').stdout or ''
    devices = [l.split('\t')[0] for l in listed.splitlines()[1:] if l.strip().endswith('\tdevice') or l.endswith('\tdevice')]
    if not devices:
        raise LifeError('adb 没有找到已授权的设备。请让原厂棒子正常开机（不要按键），并确认它开着 adb')
    if len(devices) > 1:
        raise LifeError(f'adb 同时看到 {len(devices)} 台设备，请只留一根棒子')
    done = call('-s', devices[0], 'shell', ADB_COMMAND)
    if done.returncode != 0:
        raise LifeError('adb shell 失败：' + (((done.stderr or '').strip().splitlines()) or ['未知错误'])[-1])
    return parse_adb(done.stdout or '')

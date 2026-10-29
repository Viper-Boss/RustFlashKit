# SPDX-License-Identifier: GPL-3.0-only
"""通过 bkerler/edl（GPLv3，固定提交 1cda1a6）连接 9008 设备。只在工作子进程里使用。"""
import hashlib
import sys

from . import guard
from .policy import KNOWN_LOADERS, MSM8916_ID

EDL_REVISION = '1cda1a616748d608667b2d13b73a28d3197ceb45'


class LinkError(RuntimeError):
    pass


# edl 把 cdc.timeout 设成 1500（USB 后端按毫秒），pyserial 却按秒理解，串口模式一次读可能卡 25 分钟。
# 这里给串口读取加上限；停滞更久的情况由工作进程看门狗兜底。
CONNECT_READ_CAP = 5.0
DATA_READ_CAP = 30.0
SERIAL_READ_CAP = [CONNECT_READ_CAP]
MISSED_HELLO = ('EDL 连接失败。最常见原因：设备进入 9008 后只发一次 Sahara Hello，工具打开端口时已经错过。'
                '请拔掉 USB、重新进入 9008，然后立即读取分区表；也可能是加载器与设备不匹配。详见日志。')


def capped(timeout):
    cap = SERIAL_READ_CAP[0]
    return cap if timeout is None or timeout > cap else timeout


def cap_serial_reads():
    from edlclient.Library.Connection.seriallib import serial_class
    if getattr(serial_class.usbread, '_usbtool_capped', False):
        return
    original = serial_class.usbread

    def usbread(self, resplen=None, timeout=0):
        return original(self, resplen, capped(timeout))

    usbread._usbtool_capped = True
    serial_class.usbread = usbread


def loader_digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def check_loader(path, allow_unknown):
    digest = loader_digest(path)
    if digest not in KNOWN_LOADERS and not allow_unknown:
        raise LinkError(f'加载器 SHA256 {digest} 不在已知名单中；确认来源可信后需在界面上明确勾选才可使用')
    return digest, KNOWN_LOADERS.get(digest, '未知加载器（用户确认使用）')


class EdlLink:
    """一次 EDL 会话：Sahara 上传加载器 -> Firehose。所有写方法都被 guard 禁用。"""

    def __init__(self, loader, mode, port, log=print):
        self.policy = guard.ReadOnlyPolicy()
        self.log = log
        old_argv = sys.argv
        try:
            # edl.py 在导入时用 docopt 解析 sys.argv，给它一个固定的只读命令。
            sys.argv = ['edl.py', 'printgpt', '--memory=emmc']
            import edlclient.edl as edl_module
            from edlclient.Library.firehose import firehose
        finally:
            sys.argv = old_argv
        guard.install(firehose, self.policy)
        cap_serial_reads()
        args = dict(edl_module.args)
        args.update({'--loader': str(loader), '--memory': 'emmc', '--skipwrite': True,
                     '--maxpayload': '32768', '--sectorsize': '512', '--vid': '0x05c6', '--pid': '0x9008',
                     'printgpt': False})
        if mode == 'serial':
            args.update({'--serial': True, '--portname': '\\\\.\\' + port if not port.startswith('\\') else port})
        elif mode != 'usb':
            raise LinkError(f'未知连接方式 {mode}')
        self.client = edl_module.main(args)
        SERIAL_READ_CAP[0] = CONNECT_READ_CAP
        log(f'打开 {port if mode == "serial" else "USB"}，等待 Sahara Hello 并上传加载器…')
        if self.client.run() != 0 or self.client.fh is None:
            raise LinkError(MISSED_HELLO)
        SERIAL_READ_CAP[0] = DATA_READ_CAP
        log('Firehose 已就绪')
        fh = self.client.fh.firehose
        if fh.cfg.MemoryName.lower() != 'emmc':
            raise LinkError('设备存储类型不是 eMMC')
        self.fh = fh

    def device_info(self):
        sahara = self.client.sahara
        msm_id = getattr(sahara, 'msm_id', None)
        info = {'hwid': getattr(sahara, 'hwidstr', None), 'msm_id': None if msm_id is None else f'0x{msm_id:06x}',
                'serial': None if getattr(sahara, 'serial', None) is None else str(sahara.serial),
                'chip_ok': msm_id == MSM8916_ID if msm_id is not None else None}
        if msm_id is not None and msm_id != MSM8916_ID:
            raise LinkError(f'芯片 MSM_ID {info["msm_id"]} 不是 MSM8916，停止')
        return info

    def set_disk_sectors(self, sectors):
        self.policy.disk_sectors = sectors

    def read(self, start, count):
        rsp = self.fh.cmd_read_buffer(0, start, count, display=False)
        data = getattr(rsp, 'data', None)
        if getattr(rsp, 'resp', False) is not True or data is None or len(data) != count * 512:
            raise LinkError(f'读取扇区 {start}+{count} 失败')
        return bytes(data)

    def reset(self):
        self.policy.allow_reset = True
        try:
            ok = self.fh.cmd_reset('reset')
        finally:
            self.policy.allow_reset = False
        if ok is not True:
            raise LinkError('没有收到重启确认，请检查设备状态，不要盲目重试')

    def close(self):
        if self.client is not None and self.client.cdc is not None:
            self.client.cdc.close()

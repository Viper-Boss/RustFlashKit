# SPDX-License-Identifier: GPL-3.0-only
"""在 bkerler/edl 外面加的只读护栏。

做法移植自 RustSimHub tools/recovery/edl_readonly.py：
1. 每条发往设备的 Firehose XML 先过白名单，只允许 configure(SkipWrite=1)/read/getstorageinfo/nop，
   以及调用方显式打开时的一次 power reset；
2. 把 edl 里所有会改写存储或内存的方法替换成直接抛错。
"""
import xml.etree.ElementTree as ET

MUTATING_METHODS = (
    'cmd_program', 'cmd_program_buffer', 'cmd_erase', 'cmd_patch', 'cmd_poke', 'cmd_memcpy',
    'cmd_setactiveslot', 'cmd_setbootablestoragedrive', 'cmd_writeimei', 'cmd_send', 'cmd_xml', 'cmd_rawxml',
)

MAX_SECTORS = 1 << 32  # 读到 GPT 之前的宽松上限


class RefusedOperation(RuntimeError):
    pass


class ReadOnlyPolicy:
    def __init__(self, write_payload=None):
        self.disk_sectors = MAX_SECTORS
        self.allow_reset = False
        # 只有受限写入会话在连接时设置：连接时那一次 configure 必须是 SkipWrite=0 且块大小正好等于它。
        # 加载器只认第一次 configure（2026-11-01 真机：先 SkipWrite=1 再 SkipWrite=0，都回 ACK，但没有写入）。
        self.write_payload = write_payload

    def check(self, data):
        if isinstance(data, bytes):
            data = data.decode('utf-8', errors='strict')
        if len(data) > 8192:
            raise RefusedOperation('Firehose XML 过长')
        body = data.strip()
        if body.startswith('<?xml'):
            end = body.find('?>')
            if end < 0:
                raise RefusedOperation('XML 声明不完整')
            body = body[end + 2:]
        if '<!' in body:
            raise RefusedOperation('拒绝 DOCTYPE/实体声明')
        root = ET.fromstring(body)
        if root.tag != 'data' or root.attrib or len(root) != 1:
            raise RefusedOperation('每次只允许一条 Firehose 操作')
        command = root[0]
        if list(command):
            raise RefusedOperation('拒绝嵌套 Firehose 命令')
        tag = command.tag
        if tag == 'configure':
            if command.get('MemoryName', '').lower() != 'emmc':
                raise RefusedOperation('只支持 eMMC')
            if self.write_payload is None:
                if command.get('SkipWrite') != '1':
                    raise RefusedOperation('configure 必须保持 SkipWrite=1')
            elif command.get('SkipWrite') != '0' or \
                    command.get('MaxPayloadSizeToTargetInBytes') != str(self.write_payload):
                raise RefusedOperation(f'写入会话的 configure 必须是 SkipWrite=0 且块大小为 {self.write_payload}')
        elif tag == 'read':
            allowed = {'SECTOR_SIZE_IN_BYTES', 'num_partition_sectors', 'physical_partition_number', 'start_sector'}
            if set(command.attrib) != allowed:
                raise RefusedOperation('read 含有意外属性')
            if command.get('SECTOR_SIZE_IN_BYTES') != '512' or command.get('physical_partition_number') != '0':
                raise RefusedOperation('只允许读 eMMC 用户区（512 字节扇区）')
            start = int(command.get('start_sector'))
            count = int(command.get('num_partition_sectors'))
            if not (0 <= start < self.disk_sectors and 0 < count <= self.disk_sectors - start):
                raise RefusedOperation('读取超出磁盘范围')
        elif tag == 'getstorageinfo':
            if command.attrib not in ({}, {'physical_partition_number': '0'}):
                raise RefusedOperation('不支持的存储查询')
        elif tag == 'nop':
            if command.attrib:
                raise RefusedOperation('nop 不应有属性')
        elif tag == 'power':
            if not (self.allow_reset and command.attrib == {'value': 'reset'}):
                raise RefusedOperation('只允许显式请求的一次正常重启')
            self.allow_reset = False
        else:
            raise RefusedOperation(f'拒绝非只读 Firehose 操作：{tag}')


def install(firehose_class, policy):
    """给 edl 的 firehose 类打补丁。返回 policy 以便调用方调整磁盘上限。"""
    original = firehose_class.xmlsend

    def guarded_xmlsend(self, data, *args, **kwargs):
        policy.check(data)
        return original(self, data, *args, **kwargs)

    firehose_class.xmlsend = guarded_xmlsend

    def refused(*_args, **_kwargs):
        raise RefusedOperation('本工具禁用了会写入设备的 Firehose 方法')

    for name in MUTATING_METHODS:
        setattr(firehose_class, name, refused)
    return policy

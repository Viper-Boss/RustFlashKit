# SPDX-License-Identifier: GPL-3.0-only
"""严格的 Firehose 写入传输。规则移植自 RustSimHub tools/recovery/restore_protocol.py（同一版权人）：

- 只认完整 <data> 帧里的单个明确 ACK；日志里出现 “ACK” 字样不算，多个响应视为含糊并失败；
- program 必须先拿到 rawmode="true" 的 ACK 才发数据，数据发完必须拿到 rawmode="false" 的 ACK；
- 每块数据按实际写出字节数核对，短写/结果不明立即失败；每块后补一个零长度包（与上游 edl 相同）；
- 任何失败都终止本会话，不重发、不重试、不推断成功。
不经过 edl 的 xmlsend/cmd_program（它们在本工具里被禁用），直接使用 USB 端点。
"""
import array
import hashlib
import re
import time
import xml.etree.ElementTree as ET

MAX_PAYLOAD = 16384   # 8916 加载器在真机上报告的上限；不协商、不重试
MAX_REPLY = 65536
REPLY_TIMEOUT = 15


class WriteFailed(RuntimeError):
    """写入会话失败。phase 表示失败时的阶段；数据可能已部分写入，必须人工检查。"""

    def __init__(self, message, phase):
        super().__init__(message)
        self.phase = phase


def decode_reply(data):
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_REPLY or b'<!' in data:
        raise ValueError('回复为空、过长或含声明')
    cleaned = re.sub(rb'<\?xml[ \t\r\n]+[^?<>]{1,256}\?>', b'', bytes(data))
    if b'<?' in cleaned:
        raise ValueError('回复含意外的处理指令')
    root = ET.fromstring(b'<frames>' + cleaned + b'</frames>')
    if (root.text or '').strip():
        raise ValueError('回复帧外有文字')
    responses, logs = [], []
    for frame in root:
        if frame.tag != 'data' or frame.attrib or (frame.text or '').strip() or (frame.tail or '').strip():
            raise ValueError('回复帧格式异常')
        for item in frame:
            if list(item) or (item.text or '').strip() or (item.tail or '').strip():
                raise ValueError('回复元素嵌套或带文字')
            if item.tag == 'log' and set(item.attrib) == {'value'}:
                logs.append(item.get('value'))
                continue
            if item.tag != 'response' or item.get('value') not in ('ACK', 'NAK'):
                raise ValueError('回复含未知元素')
            if item.get('rawmode') not in (None, 'true', 'false'):
                raise ValueError('rawmode 取值无效')
            responses.append(dict(item.attrib))
    if len(responses) > 1:
        raise ValueError('一次收到多个响应，含糊')
    return (responses[0] if responses else None), logs


def read_reply(endpoint, timeout=REPLY_TIMEOUT, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    data = bytearray()
    logs = []
    while clock() < deadline:
        try:
            part = endpoint.read(4096, timeout=1000)
        except Exception as exc:  # pyusb 超时：继续等到总期限
            if 'timeout' in str(exc).lower() or getattr(exc, 'errno', None) in (110, 10060) \
                    or getattr(exc, 'backend_error_code', None) == -7:
                continue
            raise
        if not isinstance(part, (bytes, bytearray, array.array)) or len(part) > 4096:
            raise ValueError('USB 回复类型或长度异常')
        data.extend(bytes(part))
        if len(data) > MAX_REPLY:
            raise ValueError('USB 回复过长')
        if data.rstrip().endswith(b'</data>'):
            reply, logs = decode_reply(bytes(data))
            if reply is not None:
                return reply, logs
        if not part:
            sleep(0.01)
    raise TimeoutError('在期限内没有收到明确的响应；不会重发命令')


def write_once(endpoint, data):
    payload = bytes(data)
    if not payload:
        count = endpoint.write(b'', timeout=5000)
        if type(count) is not int or count != 0:
            raise IOError('零长度包没有得到确认')
        return 0
    count = endpoint.write(payload, timeout=5000)
    if type(count) is not int or count != len(payload):
        raise IOError(f'USB 短写或结果不明（请求 {len(payload)}，确认 {count}）；不会重试')
    return count


def configure_xml():
    return ('<?xml version="1.0" ?><data><configure MemoryName="eMMC" Verbose="0" AlwaysValidate="0" '
            f'MaxDigestTableSizeInBytes="2048" MaxPayloadSizeToTargetInBytes="{MAX_PAYLOAD}" ZLPAwareHost="1" '
            'SkipStorageInit="0" SkipWrite="0" /></data>').encode()


def program_xml(first_sector, sectors):
    return ('<?xml version="1.0" ?><data><program SECTOR_SIZE_IN_BYTES="512" '
            f'num_partition_sectors="{sectors}" physical_partition_number="0" start_sector="{first_sector}" /></data>').encode()


class WriteSession:
    """按固定顺序：configure（SkipWrite=0）一次 → 每段 program → 原始数据 → 最终 ACK。状态错乱即失败。"""

    def __init__(self, ep_out, ep_in, segments, log=print, progress=None, reply_timeout=REPLY_TIMEOUT):
        self.out, self.inp = ep_out, ep_in
        self.segments = list(segments)
        self.log, self.progress = log, progress
        self.reply_timeout = reply_timeout
        self.phase = 'ready'
        self.transcript = []
        self.index = 0

    def fail(self, message):
        phase, self.phase = self.phase, 'failed'
        raise WriteFailed(message, phase)

    def _reply(self, label):
        try:
            reply, logs = read_reply(self.inp, self.reply_timeout)
        except Exception as err:
            self.transcript.append({'phase': self.phase, 'step': label, 'error': str(err)})
            self.fail(f'{label}：没有收到明确响应（{err}）')
        self.transcript.append({'phase': self.phase, 'step': label, 'reply': reply, 'logs': logs[-5:]})
        return reply

    def _send(self, data, label):
        try:
            write_once(self.out, data)
        except Exception as err:
            self.transcript.append({'phase': self.phase, 'step': label, 'error': str(err)})
            self.fail(f'{label}：{err}')

    def configure(self):
        if self.phase != 'ready':
            self.fail('configure 只能在会话开始时发送一次')
        self.phase = 'configure-sent'
        self._send(configure_xml(), 'configure')
        reply = self._reply('configure')
        if reply.get('value') != 'ACK' or reply.get('rawmode') not in (None, 'false'):
            self.fail(f'写入配置被拒绝（{reply}）；不协商、不重试')
        self.phase = 'configured'

    def program_next(self, stream):
        """写下一段。stream 为载荷文件对象；返回实际发送数据的 SHA256。"""
        if self.phase != 'configured' or self.index >= len(self.segments):
            self.fail('program 顺序错误')
        seg = self.segments[self.index]
        label = f'program 扇区 {seg["first_sector"]}+{seg["sectors"]}'
        self.phase = 'program-sent'
        self._send(program_xml(seg['first_sector'], seg['sectors']), label)
        reply = self._reply(label)
        if reply.get('value') != 'ACK' or reply.get('rawmode') != 'true':
            self.fail(f'{label}：没有收到进入原始模式的明确 ACK（{reply}），未发送任何数据')
        self.phase = 'payload'
        total = seg['sectors'] * 512
        sent = 0
        h = hashlib.sha256()
        while sent < total:
            block = stream.read(min(MAX_PAYLOAD, total - sent))
            if len(block) == 0 or len(block) % 512:
                self.fail(f'{label}：载荷文件提前结束或长度异常')
            self._send(block, f'{label} 数据 @{sent}')
            self._send(b'', f'{label} 零长度包 @{sent}')
            h.update(block)
            sent += len(block)
            if self.progress:
                self.progress(f'写入 {seg["first_sector"]}', sent // 512, seg['sectors'])
        if stream.read(1):
            self.fail(f'{label}：载荷比计划长')
        digest = h.hexdigest()
        if digest != seg['sha256']:
            self.phase = 'payload-mismatch'
            self.fail(f'{label}：实际发送数据的摘要与计划不一致（数据已发出，必须检查）')
        self.phase = 'payload-sent'
        reply = self._reply(f'{label} 完成')
        if reply.get('value') != 'ACK' or reply.get('rawmode') != 'false':
            self.fail(f'{label}：数据发完后没有收到明确的完成 ACK（{reply}）；写入结果不明')
        self.index += 1
        self.phase = 'configured'
        return digest

    def done(self):
        return self.phase == 'configured' and self.index == len(self.segments)

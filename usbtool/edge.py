# SPDX-License-Identifier: GPL-3.0-only
"""RustSimHub Edge HTTPS 客户端：配对、登录、Wi-Fi 试连接。

协议与校验规则对应 RustSimHub crates/wire/src/auth.rs 和 crates/edge/src/web/proof.js：
设备返回签名过的 transcript 原始字节，客户端先用已核对的设备公钥验签、逐字段核对，再对**原始字节**签名。
只实现 pair/login 两种用途，不提供任意签名。经 USB 网络连接时用证书指纹钉住，不依赖系统 CA。
"""
import hashlib
import http.client
import json
import re
import socket
import ssl
import uuid

SCOPES = ('status.read', 'sms.read', 'sms.send', 'calls.read', 'calls.control', 'media.use',
          'network.manage', 'clients.manage', 'update.manage', 'system.manage')
TOOL_SCOPES = ('status.read', 'network.manage')
DEFAULT_ADDRESS = '10.55.0.2:8443'  # 设备 USB NCM 网卡 usb0
MAX_RESPONSE = 65536
HEX64 = re.compile(r'^[a-f0-9]{64}$')
TRIAL_STATES = ('starting', 'connecting', 'ready', 'saving', 'saved', 'rolled_back', 'unknown')
TERMINAL_STATES = ('saved', 'rolled_back', 'unknown')


class EdgeError(RuntimeError):
    pass


class CoreRefused(EdgeError):
    """Core 明确拒绝（权限、状态、参数）。"""


class Uncertain(EdgeError):
    """请求可能已到达设备但没有拿到确定答复，只能查询，不能当成失败重做。"""


class PinMismatch(EdgeError):
    pass


def scope_mask(scopes):
    mask = 0
    for scope in scopes:
        if scope not in SCOPES or mask & (1 << SCOPES.index(scope)):
            raise EdgeError('配对权限无效')
        mask |= 1 << SCOPES.index(scope)
    return mask


def _bytes(value, size=None):
    if not isinstance(value, list) or (size and len(value) != size) or \
            any(not isinstance(b, int) or isinstance(b, bool) or not 0 <= b <= 255 for b in value):
        raise EdgeError('挑战格式无效')
    return bytes(value)


def prove(challenge, device_key, client_public, sign, purpose, scopes=(), expected_kind='client'):
    """校验设备挑战并返回签名（字节列表）。device_key: cryptography Ed25519PublicKey。"""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    if purpose not in ('pair', 'login'):
        raise EdgeError('签名用途不支持')
    if not isinstance(challenge, dict) or set(challenge) != {'id', 'transcript', 'device_signature', 'expires_in_seconds'} \
            or not isinstance(challenge['id'], str) or not HEX64.match(challenge['id']) \
            or challenge['expires_in_seconds'] != 60 or not isinstance(challenge['transcript'], list) \
            or not 1 <= len(challenge['transcript']) <= 4096:
        raise EdgeError('挑战格式无效')
    transcript = _bytes(challenge['transcript'])
    signature = _bytes(challenge['device_signature'], 64)
    try:
        device_key.verify(signature, transcript)
    except InvalidSignature:
        raise EdgeError('设备签名与已核对的设备公钥不匹配') from None
    try:
        t = json.loads(transcript.decode('utf-8', errors='strict'))
    except ValueError:
        raise EdgeError('挑战格式无效') from None
    pin = device_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fields = {'domain', 'purpose', 'instance', 'challenge_id', 'device_public_key', 'client_public_key', 'kind', 'scopes', 'generation'}

    def integer(v):
        return isinstance(v, int) and not isinstance(v, bool)

    if not isinstance(t, dict) or set(t) != fields or t['domain'] != 'RustSimHub.Auth.v1' or t['purpose'] != purpose \
            or t['kind'] != expected_kind or t['challenge_id'] != challenge['id'] \
            or not isinstance(t['instance'], str) or not HEX64.match(t['instance']) \
            or _bytes(t['device_public_key'], 32) != pin or _bytes(t['client_public_key'], 32) != client_public \
            or not integer(t['scopes']) or not 1 <= t['scopes'] <= 1023 or not integer(t['generation']) or t['generation'] < 0:
        raise EdgeError('挑战绑定不匹配')
    if purpose == 'login' and t['generation'] < 1:
        raise EdgeError('登录代次无效')
    if purpose == 'pair' and (expected_kind != 'client' or t['scopes'] != scope_mask(scopes)):
        raise EdgeError('配对权限不匹配')
    return list(sign(transcript))


def wifi_credentials_valid(ssid, passphrase):
    """与 RustSimHub wire::wifi_credentials_valid 一致的本地预检。"""
    raw = ssid.encode('utf-8')
    if not 1 <= len(raw) <= 32 or any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f for c in ssid):
        return False
    if len(passphrase) == 64:
        return all(c in '0123456789abcdefABCDEF' for c in passphrase)
    return 8 <= len(passphrase) <= 63 and all(0x20 <= ord(c) <= 0x7e for c in passphrase)


def check_catalogue(result):
    """与网页相同的 wifi_networks 结果校验。"""
    ok = isinstance(result, dict) and result.get('schema') == 1 and result.get('source') == 'networkmanager_cache' \
        and result.get('scan_requested') is False and result.get('hardware_mutations') is False \
        and isinstance(result.get('wifi_enabled'), bool) and isinstance(result.get('networks'), list) \
        and len(result['networks']) <= 32
    if not ok:
        raise EdgeError('网络列表格式无效')
    for n in result['networks']:
        if not isinstance(n, dict) or not isinstance(n.get('ssid'), str) or not n['ssid'] \
                or len(n['ssid'].encode('utf-8')) > 32 or not isinstance(n.get('strength'), int) \
                or n.get('security') not in ('wpa2_personal', 'open', 'unsupported') \
                or n.get('selectable') is not (n.get('security') == 'wpa2_personal'):
            raise EdgeError('网络条目格式无效')
    return result


def check_trial(result):
    if not isinstance(result, dict) or not isinstance(result.get('trial_id'), str) or result.get('state') not in TRIAL_STATES \
            or result.get('wifi_only') is not True or result.get('credentials_returned') is not False \
            or result.get('cellular_activation') is not False:
        raise EdgeError('试连接响应格式无效')
    return result


def split_address(address):
    host, sep, port = address.rpartition(':')
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise EdgeError('地址格式应为 IP:端口，例如 10.55.0.2:8443')
    return host.strip('[]'), int(port)


class Transport:
    """一次请求一个 TLS 连接。cert_sha256 为 None 时只允许 fetch_certificate。"""

    def __init__(self, address, origin, cert_sha256, timeout=20):
        self.host, self.port = split_address(address)
        self.origin = origin
        self.cert_sha256 = cert_sha256
        self.timeout = timeout

    def _connection(self):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # 自签名证书：下面用指纹钉住代替 CA 校验
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=context)

    def fetch_certificate(self):
        conn = self._connection()
        try:
            conn.connect()
            return conn.sock.getpeercert(binary_form=True)
        finally:
            conn.close()

    def post(self, command, uncertain_on_failure=False):
        if not self.cert_sha256 or not self.origin:
            raise EdgeError('还没有核对设备证书')
        request_id = str(uuid.uuid4())
        body = json.dumps({'protocol': 2, 'id': request_id, 'command': command}, ensure_ascii=False).encode('utf-8')
        authority = self.origin.split('://', 1)[1]
        conn = self._connection()
        try:
            try:
                conn.connect()
            except (OSError, ssl.SSLError) as err:
                raise EdgeError(f'连接设备失败：{err}') from None
            actual = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            if actual != self.cert_sha256:
                raise PinMismatch('设备证书与上次核对的不一致，已停止（可能连错设备或被冒充）')
            try:
                conn.putrequest('POST', '/api/v2', skip_host=True, skip_accept_encoding=True)
                for name, value in (('Host', authority), ('Origin', self.origin), ('Content-Type', 'application/json'),
                                    ('X-RustSimHub-Request', '2'), ('Content-Length', str(len(body))), ('Cache-Control', 'no-store')):
                    conn.putheader(name, value)
                conn.endheaders(body)
                response = conn.getresponse()
                data = response.read(MAX_RESPONSE + 1)
            except (OSError, socket.timeout, http.client.HTTPException) as err:
                if uncertain_on_failure:
                    raise Uncertain(f'请求结果不明：{err}') from None
                raise EdgeError(f'请求失败：{err}') from None
        finally:
            conn.close()
        if response.status != 200 or len(data) > MAX_RESPONSE:
            if uncertain_on_failure and response.status >= 500:
                raise Uncertain(f'设备返回 HTTP {response.status}，结果不明')
            raise CoreRefused(f'设备拒绝请求（HTTP {response.status}）')
        try:
            value = json.loads(data.decode('utf-8'))
        except ValueError:
            raise EdgeError('响应不是有效 JSON') from None
        if not isinstance(value, dict) or value.get('protocol') != 2 or value.get('id') != request_id:
            raise EdgeError('响应与请求不匹配')
        result = value.get('result')
        if isinstance(result, dict) and 'error' in result:
            raise CoreRefused(f'设备拒绝：{result["error"]}（请检查授权、登录和设备状态）')
        return result


def origin_from_certificate(der, port=8443):
    """从证书 SAN 推断设备网址：优先 .local 主机名。"""
    from cryptography import x509
    cert = x509.load_der_x509_certificate(der)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    names = san.get_values_for_type(x509.DNSName)
    local = [n for n in names if n.endswith('.local')]
    return f'https://{(local or names)[0]}:{port}' if (local or names) else None


def identity(transport):
    """读取设备公钥（经证书已钉住的连接）。"""
    result = transport.post({'op': 'identity'})
    if not isinstance(result, dict) or result.get('signature_algorithm') != 'ed25519':
        raise EdgeError('设备身份响应无效')
    return _bytes(result.get('public_key'), 32).hex()


class Session:
    """已核对设备上的客户端会话。key 为 cryptography Ed25519PrivateKey。"""

    def __init__(self, transport, device_public_hex, key):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        self.transport = transport
        self.device_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(device_public_hex))
        self.key = key
        self.public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.token = None

    def pair(self, scopes=TOOL_SCOPES):
        challenge = self.transport.post({'op': 'pair_challenge', 'public_key': list(self.public), 'kind': 'client', 'scopes': list(scopes)})
        signature = prove(challenge, self.device_key, self.public, self.key.sign, 'pair', scopes)
        result = self.transport.post({'op': 'pair_proof', 'challenge_id': challenge['id'], 'signature': signature})
        pairing = (result or {}).get('pairing_id')
        if not isinstance(pairing, str) or not HEX64.match(pairing):
            raise EdgeError('配对响应无效')
        return pairing

    def login(self):
        self.token = None
        challenge = self.transport.post({'op': 'challenge', 'public_key': list(self.public)})
        signature = prove(challenge, self.device_key, self.public, self.key.sign, 'login')
        result = self.transport.post({'op': 'authenticate', 'challenge_id': challenge['id'], 'signature': signature})
        token = (result or {}).get('token')
        if not isinstance(token, str) or not HEX64.match(token):
            raise EdgeError('登录响应无效')
        self.token = token
        return token

    def _auth(self, command, uncertain=False):
        if not self.token:
            raise EdgeError('请先登录')
        return self.transport.post({**command, 'token': self.token}, uncertain_on_failure=uncertain)

    def wifi_networks(self):
        return check_catalogue(self._auth({'op': 'wifi_networks'}))

    def wifi_begin(self, ssid, passphrase, idempotency_key):
        if not wifi_credentials_valid(ssid, passphrase):
            raise EdgeError('网络名需 1–32 字节且无控制字符；密码需 8–63 位可打印 ASCII 或 64 位十六进制')
        return check_trial(self._auth({'op': 'wifi_begin', 'idempotency_key': idempotency_key, 'ssid': ssid,
                                       'passphrase': passphrase}, uncertain=True))

    def wifi_action(self, op, trial_id):
        if op not in ('wifi_status', 'wifi_confirm', 'wifi_cancel'):
            raise EdgeError('不支持的操作')
        return check_trial(self._auth({'op': op, 'trial_id': trial_id}, uncertain=op != 'wifi_status'))

# SPDX-License-Identifier: GPL-3.0-only
import datetime
import hashlib
import http.server
import ipaddress
import json
import os
import secrets
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from usbtool import dpapi, edge, edge_store


def raw(public):
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class FakeCore:
    """按 RustSimHub 规则模拟 Core：挑战签名、配对待批准、登录、Wi-Fi 试连接。"""

    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        self.instance = secrets.token_hex(32)
        self.challenges = {}
        self.pending = {}
        self.approved = {}
        self.tokens = {}
        self.trials = {}
        self.begins = {}
        self.tamper = None

    def challenge(self, purpose, client, kind, scopes, generation):
        cid = secrets.token_hex(32)
        t = {'domain': 'RustSimHub.Auth.v1', 'purpose': purpose, 'instance': self.instance, 'challenge_id': cid,
             'device_public_key': list(raw(self.key.public_key())), 'client_public_key': list(client),
             'kind': kind, 'scopes': scopes, 'generation': generation}
        if self.tamper:
            self.tamper(t)
        transcript = json.dumps(t, separators=(',', ':')).encode()
        self.challenges[cid] = (purpose, bytes(client), transcript, scopes)
        return {'id': cid, 'transcript': list(transcript), 'device_signature': list(self.key.sign(transcript)),
                'expires_in_seconds': 60}

    def verify(self, cid, signature, purpose):
        entry = self.challenges.pop(cid, None)
        if not entry or entry[0] != purpose:
            return None
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            Ed25519PublicKey.from_public_bytes(entry[1]).verify(bytes(signature), entry[2])
        except Exception:
            return None
        return entry

    def handle(self, c):
        op = c['op']
        if op == 'identity':
            return {'public_key': list(raw(self.key.public_key())), 'signature_algorithm': 'ed25519', 'protocol': 2}
        if op == 'pair_challenge':
            return self.challenge('pair', c['public_key'], c['kind'], edge.scope_mask(c['scopes']), 0)
        if op == 'pair_proof':
            entry = self.verify(c['challenge_id'], c['signature'], 'pair')
            if not entry:
                return {'error': 'forbidden'}
            pid = secrets.token_hex(32)
            self.pending[pid] = entry[1]
            return {'pairing_id': pid}
        if op == 'challenge':
            if bytes(c['public_key']) not in self.approved.values():
                return {'error': 'forbidden'}
            return self.challenge('login', c['public_key'], 'client', edge.scope_mask(edge.TOOL_SCOPES), 1)
        if op == 'authenticate':
            entry = self.verify(c['challenge_id'], c['signature'], 'login')
            if not entry:
                return {'error': 'forbidden'}
            token = secrets.token_hex(32)
            self.tokens[token] = entry[1]
            return {'token': token, 'expires_in_seconds': 900}
        if c.get('token') not in self.tokens:
            return {'error': 'forbidden'}
        if op == 'wifi_networks':
            return {'schema': 1, 'source': 'networkmanager_cache', 'scan_requested': False, 'hardware_mutations': False,
                    'wifi_enabled': True, 'scan_cache_known': True, 'truncated': False, 'omitted': 0,
                    'networks': [{'ssid': 'Home', 'strength': 80, 'frequency_mhz': 2412, 'security': 'wpa2_personal',
                                  'connected': False, 'selectable': True}]}
        if op == 'wifi_begin':
            key = c['idempotency_key']
            if key in self.begins:
                if self.begins[key] != (c['ssid'], c['passphrase']):
                    return {'error': 'forbidden'}
                return self.view(self.trials_by_key[key])
            self.begins[key] = (c['ssid'], c['passphrase'])
            tid = secrets.token_hex(16)
            self.trials[tid] = 'connecting'
            self.trials_by_key = getattr(self, 'trials_by_key', {})
            self.trials_by_key[key] = tid
            return self.view(tid)
        if op in ('wifi_status', 'wifi_confirm', 'wifi_cancel'):
            tid = c['trial_id']
            if tid not in self.trials:
                return {'error': 'not_found'}
            if op == 'wifi_status' and self.trials[tid] == 'connecting':
                self.trials[tid] = 'ready'
            elif op == 'wifi_confirm':
                if self.trials[tid] != 'ready':
                    return {'error': 'invalid_state'}
                self.trials[tid] = 'saved'
            elif op == 'wifi_cancel':
                self.trials[tid] = 'rolled_back'
            return self.view(tid)
        return {'error': 'unsupported'}

    def view(self, tid):
        state = self.trials[tid]
        return {'trial_id': tid, 'state': state, 'expires_in_seconds': 40, 'wifi_only': True,
                'requires_confirmation': state not in edge.TERMINAL_STATES, 'credentials_returned': False,
                'cellular_activation': False}

    def approve_all(self):
        self.approved.update(self.pending)
        self.pending.clear()


class FakeTransport:
    def __init__(self, core):
        self.core = core
        self.sent = []
        self.fail_next = None

    def post(self, command, uncertain_on_failure=False):
        self.sent.append(command)
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            raise error
        result = self.core.handle(json.loads(json.dumps(command)))
        if isinstance(result, dict) and 'error' in result:
            raise edge.CoreRefused(result['error'])
        return result


@pytest.fixture
def core():
    return FakeCore()


def session(core, key=None):
    return edge.Session(FakeTransport(core), raw(core.key.public_key()).hex(), key or Ed25519PrivateKey.generate())


def test_full_pair_login_wifi_flow(core):
    s = session(core)
    assert edge.identity(s.transport) == raw(core.key.public_key()).hex()
    pairing = s.pair()
    assert len(pairing) == 64 and core.pending
    with pytest.raises(edge.CoreRefused):
        s.login()  # 主人尚未批准
    core.approve_all()
    s.login()
    assert [n['ssid'] for n in s.wifi_networks()['networks']] == ['Home']
    trial = s.wifi_begin('Home', 'correct horse', 'key-1')
    assert trial['state'] == 'connecting'
    assert s.wifi_action('wifi_status', trial['trial_id'])['state'] == 'ready'
    assert s.wifi_action('wifi_confirm', trial['trial_id'])['state'] == 'saved'


def test_same_idempotency_key_returns_original_trial(core):
    s = session(core)
    s.pair()
    core.approve_all()
    s.login()
    first = s.wifi_begin('Home', 'password1', 'k')
    again = s.wifi_begin('Home', 'password1', 'k')
    assert first['trial_id'] == again['trial_id'] and len(core.trials) == 1


def test_cancel_rolls_back(core):
    s = session(core)
    s.pair()
    core.approve_all()
    s.login()
    trial = s.wifi_begin('Home', 'password1', 'k')
    assert s.wifi_action('wifi_cancel', trial['trial_id'])['state'] == 'rolled_back'


def test_wifi_requires_login(core):
    with pytest.raises(edge.EdgeError, match='登录'):
        session(core).wifi_networks()


def test_unknown_wifi_op_refused(core):
    s = session(core)
    s.token = 'a' * 64
    with pytest.raises(edge.EdgeError):
        s.wifi_action('wifi_delete_all', 'x')


@pytest.mark.parametrize('tamper,match', [
    (lambda t: t.update(purpose='login'), '绑定'),
    (lambda t: t.update(kind='owner'), '绑定'),
    (lambda t: t.update(domain='Other'), '绑定'),
    (lambda t: t.update(scopes=1023), '权限'),
    (lambda t: t.update(extra=1), '绑定'),
    (lambda t: t.update(client_public_key=[0] * 32), '绑定'),
    (lambda t: t.update(instance='xyz'), '绑定'),
])
def test_pair_rejects_bad_transcript(core, tamper, match):
    core.tamper = tamper
    s = session(core)
    with pytest.raises(edge.EdgeError, match=match):
        s.pair()
    assert not any(c['op'] == 'pair_proof' for c in s.transport.sent)  # 不对可疑挑战签名


def test_login_rejects_generation_zero(core):
    s = session(core)
    s.pair()
    core.approve_all()
    core.tamper = lambda t: t.update(generation=0)
    with pytest.raises(edge.EdgeError, match='代次'):
        s.login()


def test_wrong_device_key_rejected(core):
    s = edge.Session(FakeTransport(core), raw(Ed25519PrivateKey.generate().public_key()).hex(), Ed25519PrivateKey.generate())
    with pytest.raises(edge.EdgeError, match='设备签名'):
        s.pair()


def test_challenge_shape_checked(core):
    s = session(core)
    good = core.challenge('login', s.public, 'client', 3, 1)
    for bad in ({**good, 'expires_in_seconds': 30}, {**good, 'id': 'short'}, {**good, 'extra': 1},
                {**good, 'device_signature': good['device_signature'][:10]}):
        with pytest.raises(edge.EdgeError):
            edge.prove(bad, core.key.public_key(), s.public, s.key.sign, 'login')
    with pytest.raises(edge.EdgeError, match='用途'):
        edge.prove(good, core.key.public_key(), s.public, s.key.sign, 'bootstrap')


def test_uncertain_begin_is_reported_not_retried(core):
    s = session(core)
    s.pair()
    core.approve_all()
    s.login()
    s.transport.fail_next = edge.Uncertain('timeout')
    with pytest.raises(edge.Uncertain):
        s.wifi_begin('Home', 'password1', 'k')
    assert sum(c['op'] == 'wifi_begin' for c in s.transport.sent) == 1


@pytest.mark.parametrize('ssid,passphrase,ok', [
    ('Home', 'password1', True), ('Home', 'short', False), ('', 'password1', False),
    ('x' * 33, 'password1', False), ('a\nb', 'password1', False), ('家', 'p' * 63, True),
    ('Home', 'p' * 64, False), ('Home', 'a' * 64, True), ('Home', 'pässword1', False),
])
def test_wifi_credentials(ssid, passphrase, ok):
    assert edge.wifi_credentials_valid(ssid, passphrase) is ok


def test_catalogue_and_trial_validation():
    with pytest.raises(edge.EdgeError):
        edge.check_catalogue({'schema': 1, 'source': 'scan', 'scan_requested': True})
    with pytest.raises(edge.EdgeError):
        edge.check_trial({'trial_id': 'x', 'state': 'ready', 'wifi_only': True, 'credentials_returned': True, 'cellular_activation': False})
    with pytest.raises(edge.EdgeError):
        edge.check_trial({'trial_id': 'x', 'state': 'ready', 'wifi_only': True, 'credentials_returned': False, 'cellular_activation': True})


# ---------- 真实 TLS：证书钉住与请求头 ----------

def make_cert(tmp_path, name='rustsimhub-0087.local'):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'c.pem', tmp_path / 'k.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_path, key_path, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


@pytest.fixture
def tls_server(tmp_path):
    cert_path, key_path, digest = make_cert(tmp_path)
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((dict(self.headers), body))
            result = {'error': 'forbidden'} if body['command']['op'] == 'deny' else {'ok': True}
            reply_id = 'wrong' if body['command']['op'] == 'badid' else body['id']
            data = json.dumps({'protocol': 2, 'id': reply_id, 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'127.0.0.1:{server.server_address[1]}', digest, seen
    server.shutdown()


def test_tls_pin_headers_and_origin(tls_server):
    address, digest, seen = tls_server
    probe = edge.Transport(address, None, None)
    der = probe.fetch_certificate()
    assert hashlib.sha256(der).hexdigest() == digest
    origin = edge.origin_from_certificate(der, probe.port)
    assert origin == f'https://rustsimhub-0087.local:{probe.port}'
    t = edge.Transport(address, origin, digest)
    assert t.post({'op': 'identity'}) == {'ok': True}
    headers, body = seen[-1]
    assert headers['Host'] == origin.split('://')[1] and headers['Origin'] == origin
    assert headers['X-RustSimHub-Request'] == '2' and headers['Content-Type'] == 'application/json'
    assert body['protocol'] == 2 and body['command'] == {'op': 'identity'}


def test_tls_pin_mismatch_sends_nothing(tls_server):
    address, _digest, seen = tls_server
    with pytest.raises(edge.PinMismatch):
        edge.Transport(address, 'https://x.local:8443', '00' * 32).post({'op': 'identity'})
    assert seen == []


def test_tls_core_refusal_and_id_mismatch(tls_server):
    address, digest, _seen = tls_server
    t = edge.Transport(address, 'https://x.local:8443', digest)
    with pytest.raises(edge.CoreRefused):
        t.post({'op': 'deny'})
    with pytest.raises(edge.EdgeError, match='不匹配'):
        t.post({'op': 'badid'})


def test_post_requires_pin():
    with pytest.raises(edge.EdgeError, match='核对'):
        edge.Transport('127.0.0.1:1', None, None).post({'op': 'identity'})


def test_bad_address_rejected():
    with pytest.raises(edge.EdgeError):
        edge.Transport('10.55.0.2', None, None)


# ---------- 本地保存 ----------

def test_store_roundtrip_and_corrupt_key(tmp_path):
    identity = lambda b: b
    key = edge_store.load_or_create_key(identity, identity)
    again = edge_store.load_or_create_key(identity, identity)
    assert raw(key.public_key()) == raw(again.public_key())
    (edge_store.folder() / 'client.key').write_bytes(b'short')
    with pytest.raises(ValueError, match='损坏'):
        edge_store.load_or_create_key(identity, identity)
    assert edge_store.load_device() is None
    edge_store.save_device('10.55.0.2:8443', 'https://a.local:8443', 'ab' * 32, 'cd' * 32)
    assert edge_store.load_device()['origin'] == 'https://a.local:8443'
    edge_store.forget_device()
    assert edge_store.load_device() is None


@pytest.mark.skipif(os.name != 'nt', reason='DPAPI 仅 Windows')
def test_dpapi_roundtrip():
    secret = os.urandom(32)
    sealed = dpapi.protect(secret)
    assert sealed != secret and dpapi.unprotect(sealed) == secret
    with pytest.raises(OSError):
        dpapi.unprotect(sealed[:-4] + b'xxxx')

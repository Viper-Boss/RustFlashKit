# SPDX-License-Identifier: GPL-3.0-only
"""刷机包（.rshpack）：一个不压缩的 zip，含 manifest.json、manifest.sig 和镜像文件。

一键刷机会写启动链，所以刷机包必须有 Ed25519 签名，公钥写死在 TRUSTED_KEYS；没有“跳过校验”的选项。
镜像只能是 IMAGES 列出的这几种。包里还可以带 Firehose 加载器（高通专有文件，这样的包只能自用，不能公开分发）。
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import zipfile

from . import gpt_plan, paths

KIND = 'rustsimhub-flashpack'
IMAGES = ('aboot', 'boot', 'NSHSTATE', 'rootfs')  # 写入顺序
PARTITION_SECTORS = {'aboot': 2048, **{n: gpt_plan.NEW[n][1] for n in ('boot', 'NSHSTATE', 'rootfs')}}
BLOCK = 16384  # 镜像长度必须是它的整数倍：写入只发满块（见 firehose_write.chunk_size）
FILE_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
HEX64 = re.compile(r'^[0-9a-f]{64}$')
# 刷机包签名公钥（私钥放在 RustSimHub/.private/flashpack-signing.key，不进 git）
TRUSTED_KEYS = {
    '9a4ea8cb4aa8715e2e90dc820d9c50da9fbca17fcb5300dbcd6cd201a1b4a858': 'RustSimHub 刷机包密钥 2026-11',
}


class PackError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise PackError(message)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(4 << 20), b''):
            h.update(block)
    return h.hexdigest()


def generate_key(path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    with open(path, 'x', encoding='ascii') as stream:  # 绝不覆盖已有私钥
        stream.write(raw.hex())
    return public_key_hex(key)


def public_key_hex(key):
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def load_key(path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    with open(path, encoding='ascii') as stream:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(stream.read().strip()))


def validate_manifest(manifest):
    _require(isinstance(manifest, dict) and manifest.get('format') == 1 and manifest.get('kind') == KIND, '不是受支持的刷机包')
    _require(set(manifest) <= {'format', 'kind', 'name', 'version', 'board', 'images', 'loader'}, '刷机包清单含有未知字段')
    _require(manifest.get('board') == 'ufi003', '刷机包不是给 UFI003 用的')
    _require(isinstance(manifest.get('name'), str) and isinstance(manifest.get('version'), str), '刷机包缺少名称或版本')
    images = manifest.get('images')
    _require(isinstance(images, dict) and set(images) == set(IMAGES), f'刷机包必须正好包含 {", ".join(IMAGES)}')
    for name, info in images.items():
        _require(isinstance(info, dict) and set(info) == {'file', 'sha256', 'bytes'}, f'{name} 的清单项格式不对')
        _require(FILE_NAME.match(str(info['file'])) and HEX64.match(str(info['sha256'])), f'{name} 的文件名或摘要无效')
        size = info['bytes']
        _require(isinstance(size, int) and not isinstance(size, bool) and 0 < size, f'{name} 的长度无效')
        _require(size % BLOCK == 0, f'{name} 的长度必须是 {BLOCK} 字节的整数倍')
        _require(size <= PARTITION_SECTORS[name] * 512, f'{name} 比目标分区大')
    loader = manifest.get('loader')
    if loader is not None:
        _require(isinstance(loader, dict) and set(loader) == {'file', 'sha256', 'bytes'}
                 and FILE_NAME.match(str(loader['file'])) and HEX64.match(str(loader['sha256'])), '加载器清单项无效')
    files = [i['file'] for i in images.values()] + ([loader['file']] if loader else [])
    _require(len(set(files)) == len(files) and not {'manifest.json', 'manifest.sig'} & set(files), '刷机包文件名重复或冲突')
    return manifest


def create_pack(output, name, version, images, key_path, loader=None):
    """images: {'aboot': 路径, ...}。输出文件已存在时拒绝。"""
    key = load_key(key_path)
    manifest = {'format': 1, 'kind': KIND, 'name': name, 'version': version, 'board': 'ufi003', 'images': {}}
    entries = {}
    for image, path in images.items():
        manifest['images'][image] = {'file': f'{image}.img', 'sha256': _sha256(path), 'bytes': os.path.getsize(path)}
        entries[f'{image}.img'] = path
    if loader:
        manifest['loader'] = {'file': 'loader.mbn', 'sha256': _sha256(loader), 'bytes': os.path.getsize(loader)}
        entries['loader.mbn'] = loader
    validate_manifest(manifest)
    raw = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_STORED, allowZip64=True) as pack:
        pack.writestr('manifest.json', raw)
        pack.writestr('manifest.sig', key.sign(raw))
        for arcname, path in entries.items():
            pack.write(path, arcname)
    return manifest


def _verify_signature(raw, signature, trusted=None):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    for public in (TRUSTED_KEYS if trusted is None else trusted):
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(public)).verify(signature, raw)
            return public
        except (InvalidSignature, ValueError):
            continue
    raise PackError('刷机包签名无效，或不是受信任的密钥签的，已拒绝')


def packs_dir():
    path = paths.local_dir() / 'packs'
    path.mkdir(parents=True, exist_ok=True)
    return path


def import_pack(path, progress=None):
    """校验签名和每个文件的摘要，解到 .local/packs/<清单摘要前 16 位>/，返回该目录。任何不符都不留下结果。"""
    try:
        pack = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as err:
        raise PackError(f'打不开刷机包：{err}') from None
    with pack:
        names = pack.namelist()
        _require(len(names) == len(set(names)) and {'manifest.json', 'manifest.sig'} <= set(names), '刷机包缺少清单或签名')
        raw, signature = pack.read('manifest.json'), pack.read('manifest.sig')
        _require(len(raw) <= 1 << 20, '刷机包清单过大')
        _verify_signature(raw, signature)
        try:
            manifest = json.loads(raw.decode('utf-8'))
        except ValueError:
            raise PackError('刷机包清单不是有效 JSON') from None
        validate_manifest(manifest)
        files = list(manifest['images'].values()) + ([manifest['loader']] if manifest.get('loader') else [])
        _require(set(names) == {'manifest.json', 'manifest.sig'} | {f['file'] for f in files}, '刷机包里有清单之外的文件')
        for info in pack.infolist():
            _require(info.compress_type == zipfile.ZIP_STORED and not info.flag_bits & 1, '刷机包不能压缩或加密')
        target = packs_dir() / hashlib.sha256(raw).hexdigest()[:16]
        if target.is_dir() and check_imported(target) is not None:
            return target
        partial = target.with_name(target.name + '.partial')
        if partial.exists():
            shutil.rmtree(partial)
        if target.exists():
            shutil.rmtree(target)
        partial.mkdir()
        total, done = sum(f['bytes'] for f in files), 0
        for info in files:
            h = hashlib.sha256()
            with pack.open(info['file']) as source, open(partial / info['file'], 'xb') as sink:
                for block in iter(lambda: source.read(4 << 20), b''):
                    h.update(block)
                    sink.write(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
            if h.hexdigest() != info['sha256'] or os.path.getsize(partial / info['file']) != info['bytes']:
                shutil.rmtree(partial)
                raise PackError(f'{info["file"]} 与清单不一致，刷机包可能已损坏')
        (partial / 'manifest.json').write_bytes(raw)
        (partial / 'manifest.sig').write_bytes(signature)
        os.replace(partial, target)
    return target


def check_imported(folder, deep=False):
    """读取已导入目录的清单并重新验签。deep=True 时重算文件摘要。不符返回 None。"""
    folder = pathlib.Path(folder)
    try:
        raw = (folder / 'manifest.json').read_bytes()
        _verify_signature(raw, (folder / 'manifest.sig').read_bytes())
        manifest = validate_manifest(json.loads(raw.decode('utf-8')))
        files = list(manifest['images'].values()) + ([manifest['loader']] if manifest.get('loader') else [])
        for info in files:
            path = folder / info['file']
            if path.stat().st_size != info['bytes'] or (deep and _sha256(path) != info['sha256']):
                return None
    except (OSError, ValueError):
        return None
    return manifest

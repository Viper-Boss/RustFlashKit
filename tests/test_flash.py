# SPDX-License-Identifier: GPL-3.0-only
import json
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from conftest import FakeDisk
from test_write import FakeTarget
from usbtool import firehose_write, flashpack, gpt, gpt_plan, oneclick, worker, writeplan

DISK = 30000
SMALL = ('modem', 'sbl1', 'sbl1bak', 'aboot', 'abootbak', 'rpm', 'rpmbak', 'tz', 'tzbak', 'hyp', 'hypbak', 'pad',
         'modemst1', 'modemst2', 'misc', 'fsc', 'ssd', 'splash', 'DDR', 'fsg', 'sec')


def stock_parts():
    parts = [(name, 34 + i * 64, 34 + i * 64 + 63) for i, name in enumerate(SMALL)]
    return parts + [('boot', 1378, 1505), ('system', 1506, 9999), ('persist', 10000, 10063), ('cache', 10064, 10127),
                    ('recovery', 10128, 10191), ('userdata', 10192, DISK - 34)]


class StockDisk(FakeDisk):
    def __init__(self, parts=None, seed=5):
        parts = parts or stock_parts()
        super().__init__(DISK, parts[:16], seed=seed)
        primary, back = gpt.build_gpt(DISK, parts, count=28)
        self.data[:len(primary)] = primary
        self.data[-len(back):] = back
        # 原厂 tz 与 tzbak 内容相同
        tz = dict((p[0], p) for p in parts).get('tz')
        tzbak = dict((p[0], p) for p in parts).get('tzbak')
        if tz and tzbak:
            self.data[tzbak[1] * 512:(tzbak[2] + 1) * 512] = self.data[tz[1] * 512:(tz[2] + 1) * 512]


@pytest.fixture(autouse=True)
def scaled_layout(monkeypatch):
    monkeypatch.setattr(gpt_plan, 'DISK_SECTORS', DISK)
    monkeypatch.setattr(gpt_plan, 'NSHSTATE_SECTORS', 512)
    monkeypatch.setattr(gpt_plan, 'NEW', {
        'boot': (1400, 1024, None), 'NSHSTATE': (2424, 512, gpt_plan.LINUX_DATA),
        'devinfo': (2936, 64, gpt_plan.DEVINFO_TYPE), 'rootfs': (10200, 4096, gpt_plan.LINUX_DATA)})
    monkeypatch.setattr(flashpack, 'PARTITION_SECTORS', {'aboot': 64, 'boot': 1024, 'NSHSTATE': 512, 'rootfs': 4096})


@pytest.fixture
def signing(tmp_path, monkeypatch):
    key_path = tmp_path / 'signing.key'
    public = flashpack.generate_key(key_path)
    monkeypatch.setattr(flashpack, 'TRUSTED_KEYS', {public: 'test'})
    return key_path


def image_files(tmp_path):
    sizes = {'aboot': 32768, 'boot': 512 * 1024, 'NSHSTATE': 480 * 512, 'rootfs': 2048 * 1024}
    files = {}
    for i, (name, size) in enumerate(sizes.items()):
        path = tmp_path / f'src-{name}.img'
        path.write_bytes(bytes([0x31 + i]) * size)
        files[name] = path
    return files


def make_pack(tmp_path, signing, name='p.rshpack', **kwargs):
    path = tmp_path / name
    flashpack.create_pack(path, 'RustSimHub', '1.0', image_files(tmp_path), signing, **kwargs)
    return path


@pytest.fixture
def pack_dir(tmp_path, signing):
    return flashpack.import_pack(make_pack(tmp_path, signing))


DEVICE = {'chip_ok': True, 'serial': '1234', 'msm_id': '0x7050e1'}


def session(pack, phase, disk, fault=None, retry=False, device=DEVICE):
    chunk = dict(oneclick.PHASES)[phase]
    target = FakeTarget(disk)
    target.fault = fault
    target.out.write(firehose_write.configure_xml(chunk))  # 连接时唯一一次 configure
    target.inp.read(4096)
    return oneclick.run_session(pack, phase, disk.read, target.out, target.inp, device, log=lambda m: None, retry=retry)


def layout_of(disk):
    primary, back = disk.range(0, 33), disk.range(DISK - 33, DISK - 1)
    return gpt.parse_gpt(primary, back, DISK)


def test_full_flash_from_stock(pack_dir, tmp_path):
    disk = StockDisk()
    keep = {name: disk.range(first, last) for name, first, last in stock_parts() if name in ('modem', 'fsg', 'persist', 'sbl1')}
    first = session(pack_dir, 'prepare', disk)
    assert first['written'] == ['gpt'] and first['done'] is False and first['layout_state'] == 'stock'
    assert gpt_plan.classify(layout_of(disk)) == 'target'
    second = session(pack_dir, 'images', disk)
    assert second['written'] == list(flashpack.IMAGES) and second['done'] is True
    by_name = {p['name']: p for p in layout_of(disk)['partitions']}
    for name, src in image_files(tmp_path).items():
        data = src.read_bytes()
        start = by_name[name]['first_sector']
        assert disk.range(start, start + len(data) // 512 - 1) == data
    for (name, first_s, last), data in ((p, keep[p[0]]) for p in stock_parts() if p[0] in keep):
        assert disk.range(first_s, last) == data
    # 再跑一遍：全部跳过，不写入
    before = bytes(disk.data)
    again_prepare = session(pack_dir, 'prepare', disk)
    again = session(pack_dir, 'images', disk)
    assert again_prepare['skipped'] == ['gpt'] and again['skipped'] == list(flashpack.IMAGES) and not again['written']
    assert bytes(disk.data) == before


def test_unstable_chip_refused_before_any_write(pack_dir):
    disk = StockDisk()
    disk.flaky = {40: {2}}  # modem 内一个扇区第二遍读错
    before_gpt = disk.range(0, 33)
    with pytest.raises(oneclick.FlashStopped, match='预检未通过'):
        session(pack_dir, 'prepare', disk)
    assert disk.range(0, 33) == before_gpt


def test_damaged_tz_refused_before_any_write(pack_dir):
    disk = StockDisk()
    tz_first = dict((p[0], p) for p in stock_parts())['tz'][1]
    disk.data[tz_first * 512] ^= 0xFF
    before_gpt = disk.range(0, 33)
    with pytest.raises(oneclick.FlashStopped, match='tz'):
        session(pack_dir, 'prepare', disk)
    assert disk.range(0, 33) == before_gpt


def test_unknown_layout_refused(pack_dir):
    disk = StockDisk(stock_parts()[:-1] + [('other', 10192, DISK - 34)])
    with pytest.raises(oneclick.FlashStopped, match='既不是'):
        session(pack_dir, 'prepare', disk)


def test_images_phase_on_stock_layout_refused(pack_dir):
    with pytest.raises(oneclick.FlashStopped, match='从头开始'):
        session(pack_dir, 'images', StockDisk())


def test_chip_not_confirmed_refused(pack_dir):
    with pytest.raises(oneclick.FlashStopped, match='MSM8916'):
        session(pack_dir, 'prepare', StockDisk(), device={'chip_ok': None})


def test_failed_step_needs_explicit_retry_then_resumes(pack_dir):
    disk = StockDisk()
    session(pack_dir, 'prepare', disk)
    with pytest.raises(firehose_write.WriteFailed):
        session(pack_dir, 'images', disk, fault='nak_program')
    with pytest.raises(oneclick.FlashStopped, match='再次确认'):
        session(pack_dir, 'images', disk)
    result = session(pack_dir, 'images', disk, retry=True)
    assert result['done'] and result['written'] == list(flashpack.IMAGES)


def test_corrupted_write_stops(pack_dir):
    disk = StockDisk()
    session(pack_dir, 'prepare', disk)
    with pytest.raises(oneclick.FlashStopped, match='核对未通过'):
        session(pack_dir, 'images', disk, fault='corrupt')


def test_nv_ranges_always_protected_in_plans(pack_dir):
    disk = StockDisk()
    plan = oneclick.gpt_plan_file(oneclick.work_dir(pack_dir), layout_of(disk), disk.range(0, 33), disk.range(DISK - 33, DISK - 1))
    names = {r['name'] for r in plan['protected']}
    assert {'modem', 'fsg', 'modemst1', 'modemst2', 'fsc', 'sec', 'persist', 'tz', 'sbl1', 'rpm', 'hyp'} <= names
    assert not {'boot', 'aboot', 'rootfs', 'NSHSTATE'} & names


# ---------- 刷机包 ----------

def rewrite_zip(src, dst, change):
    with zipfile.ZipFile(src) as old, zipfile.ZipFile(dst, 'w', zipfile.ZIP_STORED) as new:
        items = {n: old.read(n) for n in old.namelist()}
        change(items)
        for name, data in items.items():
            new.writestr(name, data)
    return dst


def test_pack_with_loader_and_reimport(tmp_path, signing):
    loader = tmp_path / 'prog.mbn'
    loader.write_bytes(b'loader')
    path = make_pack(tmp_path, signing, loader=loader)
    folder = flashpack.import_pack(path)
    assert flashpack.check_imported(folder, deep=True)['loader']['bytes'] == 6
    assert flashpack.import_pack(path) == folder


@pytest.mark.parametrize('change,match', [
    (lambda i: i.__setitem__('manifest.json', i['manifest.json'].replace(b'"1.0"', b'"2.0"')), '签名'),
    (lambda i: i.__setitem__('manifest.sig', bytes(64)), '签名'),
    (lambda i: i.__setitem__('extra.bin', b'x'), '清单之外'),
    (lambda i: i.pop('rootfs.img'), '清单之外'),
    (lambda i: i.__setitem__('boot.img', b'\0' * len(i['boot.img'])), '不一致'),
])
def test_tampered_packs_refused(tmp_path, signing, change, match):
    bad = rewrite_zip(make_pack(tmp_path, signing), tmp_path / 'bad.rshpack', change)
    with pytest.raises(flashpack.PackError, match=match):
        flashpack.import_pack(bad)
    assert not [p for p in flashpack.packs_dir().iterdir() if flashpack.check_imported(p)]


def test_untrusted_key_refused(tmp_path, signing, monkeypatch):
    path = make_pack(tmp_path, signing)
    monkeypatch.setattr(flashpack, 'TRUSTED_KEYS', {Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex(): 'other'})
    with pytest.raises(flashpack.PackError, match='签名'):
        flashpack.import_pack(path)


def test_compressed_pack_refused(tmp_path, signing):
    path = make_pack(tmp_path, signing)
    bad = tmp_path / 'deflated.rshpack'
    with zipfile.ZipFile(path) as old, zipfile.ZipFile(bad, 'w', zipfile.ZIP_DEFLATED) as new:
        for name in old.namelist():
            new.writestr(name, old.read(name))
    with pytest.raises(flashpack.PackError, match='压缩'):
        flashpack.import_pack(bad)


@pytest.mark.parametrize('image,size,match', [('boot', 16384 + 512, '整数倍'), ('rootfs', 4097 * 512 + 15 * 512, '整数倍'),
                                              ('aboot', 64 * 512 + 16384, '比目标分区大')])
def test_bad_image_size_refused(tmp_path, signing, image, size, match):
    files = image_files(tmp_path)
    files[image].write_bytes(bytes(size))
    with pytest.raises(flashpack.PackError, match=match):
        flashpack.create_pack(tmp_path / 'x.rshpack', 'n', 'v', files, signing)


def test_manifest_cannot_list_other_targets(tmp_path, signing):
    files = image_files(tmp_path)
    modem = tmp_path / 'modem.img'
    modem.write_bytes(bytes(16384))
    with pytest.raises(flashpack.PackError, match='必须正好包含'):
        flashpack.create_pack(tmp_path / 'x.rshpack', 'n', 'v', {**files, 'modem': modem}, signing)


def test_shipped_public_key_is_valid():
    import importlib
    module = importlib.reload(flashpack)
    assert module.TRUSTED_KEYS and all(len(bytes.fromhex(k)) == 32 for k in module.TRUSTED_KEYS)


def test_key_generation_never_overwrites(tmp_path):
    path = tmp_path / 'k'
    flashpack.generate_key(path)
    with pytest.raises(FileExistsError):
        flashpack.generate_key(path)


# ---------- 工作进程 ----------

@pytest.mark.parametrize('job', [
    {'action': 'flash', 'mode': 'serial', 'pack_dir': 'x', 'phase': 'prepare', 'chunk': 512, 'confirmed': True},
    {'action': 'flash', 'mode': 'usb', 'pack_dir': 'x', 'phase': 'prepare', 'chunk': 512},
    {'action': 'flash', 'mode': 'usb', 'pack_dir': 'x', 'phase': 'prepare', 'chunk': 16384, 'confirmed': True},
    {'action': 'flash', 'mode': 'usb', 'pack_dir': 'x', 'phase': 'erase', 'chunk': 512, 'confirmed': True},
])
def test_worker_flash_job_rules(job):
    with pytest.raises(ValueError):
        worker.validate_job(job)


class StaleLink:
    def __init__(self, *a, **k):
        self.write_payload = k.get('write_payload')

    def device_info(self):
        return {'msm_id': None}

    def close(self):
        pass


def test_worker_flash_refuses_device_already_in_firehose(tmp_path):
    loader = tmp_path / 'l.mbn'
    loader.write_bytes(b'x')
    made = []

    def factory(*a, **k):
        made.append(k)
        return StaleLink(*a, **k)

    job = {'action': 'flash', 'mode': 'usb', 'pack_dir': 'x', 'phase': 'images', 'chunk': 16384, 'confirmed': True,
           'loader': str(loader), 'allow_unknown_loader': True}
    with pytest.raises(ValueError, match='拔插'):
        worker.run(job, link_factory=factory)
    assert made[0]['write_payload'] == 16384


def test_real_geometry_gpt_keeps_protected_rows(monkeypatch):
    monkeypatch.undo()
    parts = [(name, 34 + i * 2048, 34 + i * 2048 + 2047) for i, name in enumerate(SMALL)]
    parts += [('boot', 1116554, 1149321), ('system', 1149322, 2067551), ('persist', 2067552, 2133087),
              ('cache', 2133088, 2500000), ('recovery', 2500001, 2600000), ('userdata', 4789152, 7634910)]
    primary, back = gpt.build_gpt(gpt_plan.DISK_SECTORS, parts, count=28)
    new_primary, new_back, layout = gpt_plan.build(primary, back)
    assert gpt_plan.classify(layout) == 'target'
    index = {p['index']: p for p in layout['partitions']}
    assert index[15]['name'] == 'NSHSTATE' and index[15]['sectors'] == 264159
    old, new = writeplan.gpt_entries_by_name(primary), writeplan.gpt_entries_by_name(new_primary)
    assert all(old[n] == new[n] for n in gpt_plan.PROTECTED)
    assert json.dumps(gpt_plan.build(primary, back)[2]) == json.dumps(layout)  # 结果确定
    bad = [('x' + p[0] if p[0] == 'system' else p[0], p[1], p[2]) for p in parts]
    bad_primary, bad_back = gpt.build_gpt(gpt_plan.DISK_SECTORS, bad, count=28)
    with pytest.raises(gpt_plan.GptPlanError, match='原厂'):
        gpt_plan.build(bad_primary, bad_back)
    colliding = [(n, a, b) if n != 'fsg' else ('fsg', 1200000, 1202047) for n, a, b in parts]
    colliding[22] = ('system', 1202048, 2067551)
    colliding[21] = ('boot', 1116554, 1199999)
    col_primary, col_back = gpt.build_gpt(gpt_plan.DISK_SECTORS, sorted(colliding, key=lambda p: [q[0] for q in parts].index(p[0])), count=28)
    with pytest.raises(gpt_plan.GptPlanError, match='保留分区'):
        gpt_plan.build(col_primary, col_back)


def test_written_but_nak_is_rechecked_not_rewritten(pack_dir):
    # 数据已落盘但最后回 NAK：下次核对内容已对，跳过这一步，不需要重写
    disk = StockDisk()
    session(pack_dir, 'prepare', disk)
    with pytest.raises(firehose_write.WriteFailed):
        session(pack_dir, 'images', disk, fault='final_nak')
    result = session(pack_dir, 'images', disk)
    assert result['skipped'] == ['aboot'] and result['written'] == ['boot', 'NSHSTATE', 'rootfs']


def test_default_loader_only_when_known(tmp_path, monkeypatch):
    from usbtool import edl_link, paths
    loader = tmp_path / 'prog_emmc_firehose_8916.mbn'
    loader.write_bytes(b'x')
    monkeypatch.setattr(paths, 'bundled_loader', lambda: loader)
    assert edl_link.default_loader() is None  # 摘要不在已知名单：不当作内置
    monkeypatch.setattr(edl_link, 'KNOWN_LOADERS', {edl_link.loader_digest(loader): 'x'})
    assert edl_link.default_loader() == str(loader)
    monkeypatch.setattr(paths, 'bundled_loader', lambda: None)
    assert edl_link.default_loader() is None

"""模块/Profile 模型测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from rustflashkit.core.modules import FlashModule, Profile


class TestModules(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "images").mkdir()
        self.img = self.tmp / "images" / "boot.img"
        self.img.write_bytes(b"fake boot image" * 100)

    def mk(self, **kw) -> FlashModule:
        base = dict(id="boot", name="boot", partition="boot",
                    image="images/boot.img")
        base.update(kw)
        return FlashModule(**base)

    def test_image_path_relative(self):
        m = self.mk()
        self.assertEqual(m.image_path(self.tmp), self.img)
        self.assertTrue(m.image_exists(self.tmp))

    def test_image_missing_errors(self):
        m = self.mk(image="images/nope.img")
        errs = m.validate(self.tmp)
        self.assertTrue(any("镜像不存在" in e for e in errs))

    def test_allow_missing_hint(self):
        m = self.mk(image="images/nope.img", allow_missing=True)
        errs = m.validate(self.tmp)
        self.assertTrue(any("将跳过" in e for e in errs))

    def test_bad_partition_rejected(self):
        m = self.mk(partition="boot;rm -rf /")
        errs = m.validate(self.tmp)
        self.assertTrue(any("分区名" in e for e in errs))
        m2 = self.mk(partition="../../etc")
        self.assertTrue(any("分区名" in e for e in m2.validate(self.tmp)))

    def test_bad_id_rejected(self):
        m = self.mk(id="Bad ID!")
        self.assertTrue(any("id 非法" in e for e in m.validate(self.tmp)))

    def test_sha256(self):
        m = self.mk()
        h = m.compute_sha256(self.tmp)
        self.assertEqual(len(h), 64)
        import hashlib
        self.assertEqual(h, hashlib.sha256(self.img.read_bytes()).hexdigest())

    def test_profile_roundtrip(self):
        p = Profile(name="测试", board="ufi003",
                    modules=[self.mk(), self.mk(id="rootfs", name="rootfs",
                                                partition="rootfs",
                                                image="images/rootfs.img",
                                                allow_missing=True)])
        path = self.tmp / "p.json"
        p.save(path)
        p2 = Profile.load(path)
        self.assertEqual(p2.name, "测试")
        self.assertEqual([m.id for m in p2.modules], ["boot", "rootfs"])
        self.assertEqual(p2.modules[1].allow_missing, True)

    def test_profile_rejects_unknown_schema(self):
        path = self.tmp / "q.json"
        path.write_text(json.dumps({"schema": 99, "name": "x", "modules": []}),
                        encoding="utf-8")
        with self.assertRaises(ValueError):
            Profile.load(path)

    def test_profile_duplicate_partition_flagged(self):
        p = Profile(name="dup", modules=[
            self.mk(), self.mk(id="boot2", name="b2", partition="boot")])
        errs = p.validate(self.tmp)
        self.assertTrue(any("分区重复" in e for e in errs))

    def test_extra_fields_ignored_on_load(self):
        path = self.tmp / "r.json"
        path.write_text(json.dumps({
            "schema": 1, "name": "x", "future_field": 1,
            "modules": [{"id": "boot", "name": "b", "partition": "boot",
                         "image": "images/boot.img", "new_field": True}]}),
            encoding="utf-8")
        p = Profile.load(path)
        self.assertEqual(len(p.modules), 1)


if __name__ == "__main__":
    unittest.main()

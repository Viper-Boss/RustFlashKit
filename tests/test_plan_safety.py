"""安全策略与计划生成测试。"""
import tempfile
import unittest
from pathlib import Path

from rustflashkit.core.modules import FlashModule, Profile
from rustflashkit.core.plan import PlanOptions, build_plan
from rustflashkit.core.safety import PartitionBlocked, assess, check_flashable


class TestSafety(unittest.TestCase):
    def test_normal_partitions_ok(self):
        for p in ("boot", "rootfs", "NSHSTATE", "system", "userdata", "recovery"):
            check_flashable(p)  # 不抛异常
            self.assertIsNone(assess(p))

    def test_bootloader_chain_flagged(self):
        self.assertEqual(assess("sbl1"), "bootloader")
        self.assertEqual(assess("aboot"), "bootloader")
        with self.assertRaises(PartitionBlocked):
            check_flashable("tz")
        check_flashable("tz", allow_bootloader=True)  # 显式允许后放行

    def test_efs_hard_blocked(self):
        for p in ("modemst1", "modemst2", "fsg", "ssd", "persistent", "devinfo"):
            self.assertEqual(assess(p), "efs")
            with self.assertRaises(PartitionBlocked):
                check_flashable(p, allow_bootloader=True)  # 即使允许引导链也拒绝

    def test_bad_names_blocked(self):
        for p in ("", "  ", "a b", "../../x", "boot;reboot", "垫片"):
            with self.assertRaises(PartitionBlocked):
                check_flashable(p)


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "images").mkdir()
        self.boot = self._mkimg("boot.img")
        self.root = self._mkimg("rootfs.img", big=True)

    def _mkimg(self, name, big=False) -> Path:
        p = self.tmp / "images" / name
        p.write_bytes(b"x" * (5 * 1024 * 1024 if big else 4096))
        return p

    def mk_profile(self, *modules) -> Profile:
        return Profile(name="t", modules=list(modules))

    def mod(self, **kw) -> FlashModule:
        base = dict(id="m0", name="m0", partition="boot",
                    image="images/boot.img")
        base.update(kw)
        return FlashModule(**base)

    def test_plan_order_and_reboot(self):
        p = self.mk_profile(
            self.mod(id="boot", partition="boot"),
            self.mod(id="rootfs", partition="rootfs", image="images/rootfs.img"))
        plan = build_plan(p, self.tmp, PlanOptions(reboot_after=True, verify_sha256=False))
        self.assertTrue(plan.ok, plan.errors)
        kinds = [s.kind for s in plan.steps]
        self.assertEqual(kinds, ["flash", "flash", "reboot"])
        self.assertEqual(plan.flashed_partitions(), ["boot", "rootfs"])
        self.assertEqual(plan.steps[0].command_repr(),
                         f'fastboot flash boot "{self.boot}"')

    def test_plan_disabled_excluded(self):
        p = self.mk_profile(
            self.mod(id="boot"),
            self.mod(id="cache", partition="cache", image="images/boot.img",
                     enabled=False))
        plan = build_plan(p, self.tmp, PlanOptions(reboot_after=False))
        self.assertEqual(plan.flashed_partitions(), ["boot"])

    def test_missing_image_error_and_skip(self):
        p = self.mk_profile(
            self.mod(id="x", partition="system", image="images/absent.img"))
        plan = build_plan(p, self.tmp, PlanOptions(reboot_after=False))
        self.assertFalse(plan.ok)
        self.assertTrue(any("镜像不存在" in e for e in plan.errors))

        p2 = self.mk_profile(
            self.mod(id="x", partition="system", image="images/absent.img",
                     allow_missing=True),
            self.mod(id="boot", partition="boot"))
        plan2 = build_plan(p2, self.tmp, PlanOptions(reboot_after=False))
        self.assertTrue(plan2.ok, plan2.errors)
        self.assertEqual(plan2.flashed_partitions(), ["boot"])
        self.assertTrue(any("跳过" in s for s in plan2.skipped))

    def test_bootloader_partition_needs_flag(self):
        p = self.mk_profile(
            self.mod(id="aboot", partition="aboot", allow_bootloader=False))
        plan = build_plan(p, self.tmp, PlanOptions(reboot_after=False))
        self.assertFalse(plan.ok)
        self.assertTrue(any("引导链" in e for e in plan.errors))

        p2 = self.mk_profile(
            self.mod(id="aboot", partition="aboot", allow_bootloader=True))
        plan2 = build_plan(p2, self.tmp, PlanOptions(reboot_after=False))
        self.assertTrue(plan2.ok, plan2.errors)
        self.assertEqual(plan2.danger_partitions(), ["aboot"])

    def test_efs_never_plan(self):
        p = self.mk_profile(self.mod(id="m", partition="modemst1",
                                     allow_bootloader=True))
        plan = build_plan(p, self.tmp, PlanOptions(reboot_after=False))
        self.assertFalse(plan.ok)
        self.assertTrue(any("EFS" in e or "禁止" in e for e in plan.errors))

    def test_sha256_step(self):
        import hashlib
        sha = hashlib.sha256(self.boot.read_bytes()).hexdigest()
        p = self.mk_profile(self.mod(sha256=sha))
        plan = build_plan(p, self.tmp, PlanOptions(verify_sha256=True))
        self.assertEqual([s.kind for s in plan.steps if s.kind == "verify_sha256"],
                         ["verify_sha256"])
        p2 = self.mk_profile(self.mod(sha256=sha))
        plan2 = build_plan(p2, self.tmp, PlanOptions(verify_sha256=False,
                                                     reboot_after=False))
        self.assertEqual([s.kind for s in plan2.steps], ["flash"])

    def test_empty_plan_is_error(self):
        plan = build_plan(Profile(name="空"), self.tmp, PlanOptions())
        self.assertFalse(plan.ok)
        self.assertTrue(any("没有可执行" in e for e in plan.errors))

    def test_post_delay_and_timeout(self):
        p = self.mk_profile(self.mod(timeout=600, post_delay=2))
        plan = build_plan(p, self.tmp, PlanOptions(reboot_after=False))
        flash = next(s for s in plan.steps if s.kind == "flash")
        self.assertEqual(flash.timeout, 600)


if __name__ == "__main__":
    unittest.main()

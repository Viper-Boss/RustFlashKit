"""FlashExecutor 端到端测试（假 fastboot）。"""
import tempfile
import time
import unittest
from pathlib import Path

from rustflashkit.core.device import DeviceService
from rustflashkit.core.flasher import FlashExecutor
from rustflashkit.core.modules import FlashModule, Profile
from rustflashkit.core.plan import PlanOptions, build_plan
from rustflashkit.core.runner import ToolRunner

from tests.fakes import calls, make_fake_tools


def run_executor(plan, runner):
    svc = DeviceService(runner)
    events = {"logs": [], "steps": [], "done": []}

    ex = FlashExecutor(
        plan, runner, svc,
        on_log=events["logs"].append,
        on_step=lambda r: events["steps"].append(r),
        on_done=lambda ok, msg: events["done"].append((ok, msg)))
    ex.start()
    deadline = time.time() + 10
    while not events["done"] and time.time() < deadline:
        time.sleep(0.05)
    return ex, events


class TestFlasher(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "images").mkdir()
        self.good = self.tmp / "images" / "boot.img"
        self.good.write_bytes(b"good" * 1024)
        self.bad = self.tmp / "images" / "bad.img"
        self.bad.write_bytes(b"bad" * 1024)
        self.locator, self.log = make_fake_tools(self.tmp)
        self.runner = ToolRunner(self.locator)

    def profile(self, *mods) -> Profile:
        return Profile(name="t", modules=list(mods))

    def mod(self, mid, partition, image) -> FlashModule:
        return FlashModule(id=mid, name=mid, partition=partition, image=image)

    def test_success_flow(self):
        p = self.profile(self.mod("boot", "boot", "images/boot.img"),
                         self.mod("sys", "system", "images/boot.img"))
        plan = build_plan(p, self.tmp, PlanOptions(reboot_after=True))
        self.assertTrue(plan.ok, plan.errors)
        _, ev = run_executor(plan, self.runner)
        self.assertEqual(ev["done"], [(True, "全部完成")])
        self.assertTrue(all(s.ok for s in ev["steps"]))
        argvs = calls(self.log)
        self.assertIn(["fastboot", "flash", "boot", str(self.good)], argvs)
        self.assertIn(["fastboot", "flash", "system", str(self.good)], argvs)
        self.assertIn(["fastboot", "reboot"], argvs)
        self.assertLess(argvs.index(["fastboot", "flash", "boot", str(self.good)]),
                        argvs.index(["fastboot", "reboot"]))

    def test_sha256_mismatch_stops(self):
        import hashlib
        wrong = "0" * 64
        p = self.profile(FlashModule(id="boot", name="boot", partition="boot",
                                     image="images/boot.img", sha256=wrong))
        plan = build_plan(p, self.tmp, PlanOptions(reboot_after=True, verify_sha256=True))
        self.assertTrue(plan.ok, plan.errors)
        _, ev = run_executor(plan, self.runner)
        ok, msg = ev["done"][0]
        self.assertFalse(ok)
        self.assertIn("SHA256 不匹配", msg)
        # 不应执行任何 flash
        self.assertFalse(any(c[:2] == ["fastboot", "flash"] for c in calls(self.log)))

    def test_flash_failure_stops_following_steps(self):
        p = self.profile(self.mod("boot", "boot", "images/boot.img"),
                         self.mod("sys", "system", "images/bad.img"))
        plan = build_plan(p, self.tmp, PlanOptions(reboot_after=True))
        self.assertTrue(plan.ok, plan.errors)
        _, ev = run_executor(plan, self.runner)
        ok, msg = ev["done"][0]
        self.assertFalse(ok)
        self.assertIn("第 2 步", msg)
        argvs = calls(self.log)
        self.assertIn(["fastboot", "flash", "boot", str(self.good)], argvs)
        # 第 2 步尝试失败（命令本身执行过），其后步骤不再执行
        self.assertIn(["fastboot", "flash", "system", str(self.bad)], argvs)
        self.assertNotIn(["fastboot", "reboot"], argvs)  # 失败后不 reboot

    def test_cancel_before_start_step(self):
        p = self.profile(self.mod("boot", "boot", "images/boot.img"))
        plan = build_plan(p, self.tmp, PlanOptions(reboot_after=False))
        svc = DeviceService(self.runner)
        done = []
        ex = FlashExecutor(plan, self.runner, svc, on_log=lambda s: None,
                           on_step=lambda r: None,
                           on_done=lambda ok, msg: done.append((ok, msg)))
        ex.cancel()          # 在任何步骤开始前取消
        ex.start()
        deadline = time.time() + 5
        while not done and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(done, [(False, "已取消")])
        self.assertFalse(any(c[:2] == ["fastboot", "flash"] for c in calls(self.log)))


if __name__ == "__main__":
    unittest.main()

"""runner 定位/执行 + 设备探测测试（使用假 adb/fastboot）。"""
import tempfile
import unittest
from pathlib import Path

from rustflashkit.core.device import DeviceService
from rustflashkit.core.runner import ToolLocator, ToolRunner

from tests.fakes import calls, make_fake_tools


class TestRunnerAndDevice(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.locator, self.log = make_fake_tools(self.tmp)
        self.runner = ToolRunner(self.locator)

    def test_locator_explicit_paths(self):
        self.assertEqual(self.locator.resolve("adb"), self.tmp / "adb")
        self.assertEqual(self.locator.resolve("fastboot"), self.tmp / "fastboot")

    def test_locator_falls_back_to_path(self):
        import shutil
        loc = ToolLocator()
        resolved = loc.resolve("python3") if shutil.which("python3") else loc.resolve("sh")
        self.assertTrue(resolved and resolved.is_file())

    def test_runner_missing_tool(self):
        loc = ToolLocator(adb_path=str(self.tmp / "nope"), fastboot_path="")
        runner = ToolRunner(loc)
        res = runner.run("fastboot", ["devices"])
        self.assertFalse(res.ok)
        self.assertIn("未找到", res.stderr)

    def test_runner_executes(self):
        res = self.runner.run("fastboot", ["devices"], timeout=10)
        self.assertTrue(res.ok)
        self.assertIn("TESTSERIAL", res.stdout)
        self.assertEqual(calls(self.log)[0], ["fastboot", "devices"])

    def test_device_probe_bootloader(self):
        svc = DeviceService(self.runner)
        info = svc.probe()
        self.assertEqual(info.mode, "bootloader")
        self.assertEqual(info.serial, "TESTSERIAL")

    def test_device_probe_system_when_no_fastboot_device(self):
        # 覆盖假 fastbot：让它报“无设备”，验证回退到 adb
        fb = self.tmp / "fastboot"
        fb.write_text('#!/bin/sh\ncase "$1" in devices) exit 0;; *) exit 0;; esac\n')
        fb.chmod(0o755)
        svc = DeviceService(self.runner)
        info = svc.probe()
        self.assertEqual(info.mode, "system")
        self.assertEqual(info.serial, "TESTSERIAL")

    def test_reboot_to_bootloader(self):
        # 用一个“无 fastboot 设备”的假 fastboot，验证回退到 adb reboot bootloader
        silent = self.tmp / "fastboot_silent"
        silent.write_text('#!/bin/sh\ncase "$1" in devices) exit 0;; *) exit 0;; esac\n')
        silent.chmod(0o755)
        self.locator.fastboot_path = str(silent)
        svc = DeviceService(self.runner)
        self.assertTrue(svc.reboot_to_bootloader())
        self.assertIn(["adb", "-s", "TESTSERIAL", "reboot", "bootloader"],
                      calls(self.log))

    def test_reboot_device(self):
        svc = DeviceService(self.runner)
        self.assertTrue(svc.reboot_device())
        self.assertIn(["fastboot", "reboot"], calls(self.log))


if __name__ == "__main__":
    unittest.main()

"""固件导出（exporter）测试：分区表解析、单分区导出、批量任务流。"""
import tempfile
import time
import unittest
from pathlib import Path

from rustflashkit.core.exporter import (ExportJob, PartitionInfo,
                                        export_partition, export_timeout,
                                        read_partition_table)
from rustflashkit.core.modules import sha256_of
from rustflashkit.core.runner import ToolRunner

from tests.fakes import calls, make_fake_tools


def run_job(runner, serial, parts, out_dir, with_sha256, timeout=15):
    events = {"logs": [], "items": [], "done": []}
    job = ExportJob(runner, serial, parts, out_dir, with_sha256,
                    on_log=events["logs"].append,
                    on_item=lambda r: events["items"].append(r),
                    on_done=lambda ok, msg: events["done"].append((ok, msg)))
    job.start()
    deadline = time.time() + timeout
    while not events["done"] and time.time() < deadline:
        time.sleep(0.05)
    return job, events


class TestExporter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.locator, self.log = make_fake_tools(self.tmp)
        self.runner = ToolRunner(self.locator)

    def test_read_partition_table(self):
        parts = read_partition_table(self.runner, "TESTSERIAL")
        names = [p.name for p in parts]
        self.assertEqual(names, ["boot", "rootfs", "modemst1"])
        self.assertEqual(parts[0].path, "/dev/block/mmcblk0p7")
        self.assertEqual(parts[0].size, 8388608)
        self.assertEqual(parts[0].size_text, "8.0MB")
        self.assertIn(["adb", "-s", "TESTSERIAL", "shell"],
                      [c[:4] for c in calls(self.log)])

    def test_export_partition_writes_file(self):
        part = PartitionInfo("boot", "/dev/block/mmcblk0p7", 8388608)
        dest = self.tmp / "out" / "boot.img"
        export_partition(self.runner, "TESTSERIAL", part, dest)
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.stat().st_size, 8192)
        self.assertIn(["adb", "-s", "TESTSERIAL", "pull",
                       "/dev/block/bootdevice/by-name/boot", str(dest)],
                      calls(self.log))

    def test_export_partition_permission_denied(self):
        part = PartitionInfo("modemst1", "/dev/block/mmcblk0p2", 2048)
        with self.assertRaises(RuntimeError) as ctx:
            export_partition(self.runner, "TESTSERIAL", part, self.tmp / "m.img")
        self.assertIn("root", str(ctx.exception))

    def test_export_job_success_flow(self):
        parts = [PartitionInfo("boot", "/dev/block/mmcblk0p7", 8388608),
                 PartitionInfo("rootfs", "/dev/block/mmcblk0p18", 4831838208)]
        _, ev = run_job(self.runner, "TESTSERIAL", parts, self.tmp / "bak",
                        with_sha256=True)
        self.assertEqual(ev["done"], [(True, "导出完成")])
        self.assertEqual([r.part.name for r in ev["items"]], ["boot", "rootfs"])
        for r in ev["items"]:
            self.assertTrue(r.ok)
            self.assertTrue(r.file.is_file())
            self.assertEqual(r.sha256, sha256_of(r.file))
            self.assertEqual(len(r.sha256), 64)

    def test_export_job_stops_on_failure(self):
        parts = [PartitionInfo("boot", "/dev/block/mmcblk0p7", 8388608),
                 PartitionInfo("modemst1", "/dev/block/mmcblk0p2", 2048),
                 PartitionInfo("rootfs", "/dev/block/mmcblk0p18", 4831838208)]
        _, ev = run_job(self.runner, "TESTSERIAL", parts, self.tmp / "bak2",
                        with_sha256=False)
        ok, msg = ev["done"][0]
        self.assertFalse(ok)
        self.assertIn("modemst1", msg)
        self.assertEqual([r.part.name for r in ev["items"]], ["boot", "modemst1"])
        self.assertFalse((self.tmp / "bak2" / "rootfs.img").exists())

    def test_export_job_cancel_before_start(self):
        parts = [PartitionInfo("boot", "/dev/block/mmcblk0p7", 8388608)]
        done, files = [], []
        job = ExportJob(self.runner, "TESTSERIAL", parts, self.tmp / "bak3",
                        with_sha256=False,
                        on_log=lambda s: None,
                        on_item=lambda r: files.append(r),
                        on_done=lambda ok, m: done.append((ok, m)))
        job.cancel()
        job.start()
        deadline = time.time() + 5
        while not done and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(done, [(False, "已取消")])
        self.assertFalse(files)
        self.assertFalse((self.tmp / "bak3" / "boot.img").exists())

    def test_export_timeout_scales_with_size(self):
        self.assertEqual(export_timeout(0), 120)
        self.assertEqual(export_timeout(10 * 1024 * 1024), 120)  # 10MB*8=80 < 120 → 120
        self.assertEqual(export_timeout(512 * 1024 * 1024), 4096)


if __name__ == "__main__":
    unittest.main()

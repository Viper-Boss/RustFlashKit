"""BLE 模块测试：模板加载/校验、载荷构建（纯逻辑，无需 bleak/硬件）。"""
import tempfile
import unittest
from pathlib import Path

from rustflashkit.core import ble
from rustflashkit.core.ble import BleTemplate, build_payload, ensure_template


class TestTemplate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_default_template_created_on_demand(self):
        path = self.tmp / "config" / "tpl.json"
        t = ensure_template(path)
        self.assertTrue(path.is_file())
        self.assertEqual(t.service_uuid, ble.DEFAULT_TEMPLATE["service_uuid"])
        # 全零占位 UUID 格式合法（validate 只查格式），占位状态由 is_placeholder 标记
        self.assertEqual(t.validate(), [])
        self.assertTrue(t.is_placeholder)

    def test_valid_template(self):
        t = BleTemplate(
            service_uuid="12345678-1234-5678-1234-567812345678",
            write_char="12345678-1234-5678-1234-567812345679",
            fields=["ssid", "psk"])
        self.assertEqual(t.validate(), [])
        self.assertFalse(t.is_placeholder)

    def test_bad_uuid_flagged(self):
        t = BleTemplate(service_uuid="not-a-uuid", write_char="", fields=["ssid"])
        errs = t.validate()
        self.assertEqual(len(errs), 2)

    def test_load_rejects_future_schema(self):
        path = self.tmp / "f.json"
        path.write_text('{"schema": 99}', encoding="utf-8")
        with self.assertRaises(ValueError):
            BleTemplate.load(path)

    def test_roundtrip(self):
        t = BleTemplate(name="x", service_uuid="12345678-1234-5678-1234-567812345678",
                        write_char="12345678-1234-5678-1234-567812345679",
                        payload={"v": 1, "type": "wifi.recovery"}, fields=["ssid"])
        p = self.tmp / "t.json"
        t.save(p)
        t2 = BleTemplate.load(p)
        self.assertEqual(t2.name, "x")
        self.assertEqual(t2.fields, ["ssid"])
        self.assertEqual(t2.payload["type"], "wifi.recovery")


class TestPayload(unittest.TestCase):
    def mk(self, **kw) -> BleTemplate:
        base = dict(service_uuid="12345678-1234-5678-1234-567812345678",
                    write_char="12345678-1234-5678-1234-567812345679",
                    payload={"v": 1, "type": "wifi.recovery"}, fields=["ssid", "psk"])
        base.update(kw)
        return BleTemplate(**base)

    def test_payload_shape(self):
        import json
        data = build_payload(self.mk(), {"ssid": "Home", "psk": "pw123"})
        doc = json.loads(data.decode("utf-8"))
        self.assertEqual(doc, {"v": 1, "type": "wifi.recovery",
                               "ssid": "Home", "psk": "pw123"})
        self.assertTrue(data.endswith(b"\n"))

    def test_missing_field_rejected(self):
        with self.assertRaises(ValueError):
            build_payload(self.mk(), {"ssid": "Home"})

    def test_unicode_ssid(self):
        import json
        data = build_payload(self.mk(), {"ssid": "中文网络", "psk": "x"})
        self.assertEqual(json.loads(data)["ssid"], "中文网络")


class TestBleUnavailable(unittest.TestCase):
    def test_scan_without_bleak_raises_friendly(self):
        try:
            import bleak  # noqa: F401
            self.skipTest("本机已安装 bleak")
        except ImportError:
            pass
        import asyncio
        with self.assertRaises(ble.BleUnavailable):
            asyncio.run(ble.scan_devices(0.1))


if __name__ == "__main__":
    unittest.main()

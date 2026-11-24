# SPDX-License-Identifier: GPL-3.0-only
import unittest
from types import SimpleNamespace

from usbtool import maintenance


class MaintenanceTests(unittest.TestCase):
    def test_selects_unique_usb_acm(self):
        device = SimpleNamespace(device="ttyACM0", vid=0x1D6B, pid=0x0104)
        other = SimpleNamespace(device="ttyUSB0", vid=0x05C6, pid=0x9008)
        self.assertEqual(maintenance.pick_port(None, [device, other]), "ttyACM0")
        with self.assertRaises(ValueError):
            maintenance.pick_port("ttyUSB0", [device, other])
        with self.assertRaises(ValueError):
            maintenance.pick_port(None, [device, device])

    def test_short_or_invalid_replies_are_rejected(self):
        for raw in (b'{"ok":true}', b'[]\n', b'bad\n', b'x' * maintenance.RESPONSE_LIMIT):
            link = SimpleNamespace(write=len, read_until=lambda delimiter, limit: raw)
            with self.assertRaises(ValueError):
                maintenance.exchange(link, "SSH_STATUS")

    def test_identity_uses_serial_frames(self):
        def write(data):
            self.assertEqual(data, b"RSHUSB1 IDENTIFY\n")
            return len(data)

        def read_until(delimiter, limit):
            self.assertEqual((delimiter, limit), (b"\n", maintenance.RESPONSE_LIMIT))
            return b'{"ok":true,"protocol":1,"product":"RustSimHub","role":"local-administrator"}\n'

        link = SimpleNamespace(write=write, read_until=read_until)
        self.assertEqual(maintenance.exchange(link, "IDENTIFY"), maintenance.IDENTITY)

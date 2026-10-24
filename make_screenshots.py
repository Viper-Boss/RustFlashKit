"""生成 README 截图：主窗口 / 计划预览 / 模块编辑器。

前置：Xvfb :99；PATH 中有假 adb/fastboot（tests/fakes.make_fake_tools 生成）。
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 假工具 → 设备面板显示 "Bootloader 模式 · TESTSERIAL"
sys.path.insert(0, str(ROOT / "tests"))
from fakes import make_fake_tools                      # noqa: E402

ftdir = ROOT / ".fake_tools"
ftdir.mkdir(exist_ok=True)
make_fake_tools(ftdir.resolve())
os.environ["PATH"] = f"{ftdir.resolve()}:" + os.environ["PATH"]

from rustflashkit.gui.app import MainApp                    # noqa: E402
from rustflashkit.core.plan import PlanOptions, build_plan  # noqa: E402
from rustflashkit.gui.dialogs import ModuleEditor, PlanPreviewDialog  # noqa: E402

# 演示镜像（稀疏文件，只为让状态列显示真实感的大小）
imgdir = ROOT / "profiles" / "images"
imgdir.mkdir(exist_ok=True)
for name, size in {"boot.img": 6 * 1024 * 1024,
                   "rootfs.img": 460 * 1024 * 1024,
                   "nshstate.img": 1024 * 1024}.items():
    with open(imgdir / name, "wb") as fh:
        fh.truncate(size)

app = MainApp()
app.geometry("1120x780+20+20")
app._apply_profile(ROOT / "profiles" / "ufi003_rustsimhub.json")
app.profile.modules[2].enabled = True   # NSHSTATE 也勾上，表格更丰满
app.refresh_modules()
for _ in range(20):
    app.update(); time.sleep(0.03)

which = sys.argv[1]
dlg = None
if which == "preview":
    plan = build_plan(app.profile, app.base_dir, PlanOptions())
    dlg = PlanPreviewDialog(app, plan)
    dlg.geometry("760x700+180+20")
    print("DLG", hex(dlg.winfo_id()), file=sys.stderr)
elif which == "editor":
    dlg = ModuleEditor(app, module=app.profile.modules[0],
                       base_dir=app.base_dir, used_ids=set())
    print("DLG", hex(dlg.winfo_id()), file=sys.stderr)
elif which == "export":
    from rustflashkit.gui.export_dialog import ExportDialog
    # 让设备探测走 adb（系统模式）：静音假 fastboot
    fb = ftdir / "fastboot"
    fb.write_text('#!/bin/sh\nexit 0\n')
    import stat as _stat
    fb.chmod(fb.stat().st_mode | _stat.S_IEXEC)
    dlg = ExportDialog(app)
    dlg.geometry("820x660+140+20")
    print("DLG", hex(dlg.winfo_id()), file=sys.stderr)
    for _ in range(15):
        app.update(); time.sleep(0.04)
    dlg._read_table()
    dlg._select_preset()
elif which == "ble":
    from rustflashkit.gui.ble_dialog import BleDialog
    dlg = BleDialog(app)
    dlg.geometry("860x760+120+10")
    print("DLG", hex(dlg.winfo_id()), file=sys.stderr)
    for _ in range(15):
        app.update(); time.sleep(0.04)
    dlg.v_addr.set("RustSimHub-BLE [AA:BB:CC:DD:EE:FF]")
    dlg.v_ssid.set("Home-WiFi")
    dlg.v_psk.set("********")
    dlg._log("→ 正在扫描 BLE 设备（6 秒）…")
    dlg._log("✓ 发现 1 台设备", "ok")
    dlg._log("★ RustSimHub-BLE  AA:BB:CC:DD:EE:FF  -58 dBm")

print("APP", hex(app.winfo_id()), file=sys.stderr)
end = time.time() + 50
while time.time() < end:
    try:
        app.update()
        time.sleep(0.05)
    except Exception:
        break

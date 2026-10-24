"""GUI 冒烟测试：在 Xvfb 下实例化主窗口 + 计划预览对话框并保持显示，供外部截图。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rustflashkit.gui.app import MainApp            # noqa: E402
from rustflashkit.core.plan import PlanOptions, build_plan  # noqa: E402
from rustflashkit.gui.dialogs import PlanPreviewDialog       # noqa: E402

app = MainApp()
app.geometry("1080x720+30+30")

# 直接加载 UFI003 模板（镜像缺失属预期，用于观察状态列）
app._apply_profile(ROOT / "profiles" / "ufi003_rustsimhub.json")
app.update_idletasks()
app.update()
time.sleep(1.2)
app.update_idletasks(); app.update()

mode = sys.argv[1] if len(sys.argv) > 1 else "main"

if mode == "preview":
    plan = build_plan(app.profile, app.base_dir, PlanOptions())
    dlg = PlanPreviewDialog(app, plan)
    dlg.geometry("720x560+120+80")

end = time.time() + 60
while time.time() < end:
    try:
        app.update()
        time.sleep(0.05)
    except Exception:
        break

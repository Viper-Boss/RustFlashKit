# RustFlashKit

MSM8916（骁龙 410 / UFI 棒子）**Windows 刷机 + 模块配置工具**。

参考 [Viper-Boss/RustSimHub](https://github.com/Viper-Boss/RustSimHub)（只读参考，本工具与其互不修改）：RustSimHub 在 UFI003_MB_V02 上实测使用的分区为 `boot` / `rootfs` / `NSHSTATE`。RustSimHub **官方尚未发布通用刷机包**，因此本工具是通用刷写前端——**镜像文件由使用者自行提供**（例如你的 RustSimHub 构建产物）。

## 功能

- **模块化刷写**：以「配置档案 (Profile, JSON)」为单位，一行 = 一个模块（分区 + 镜像 + 选项）；勾选决定是否刷写
- **模块编辑器**：添加/编辑/复制/删除/排序，分区名自动校验，SHA256 一键计算与刷前校验
- **计划预览 + 二次确认**：先看到完整 `fastboot` 命令序列，勾选风险确认后才能执行；引导链分区还需输入「确认引导链」
- **安全策略**：EFS/IMEI 分区（modemst1、fsg 等）一律拒绝；引导链分区（sbl1、aboot 等）需显式允许；失败立即停止后续步骤
- **设备面板**：自动轮询 adb/fastboot 设备，一键「重启到 Bootloader」
- **日志**：窗口实时日志 + `logs/flash-时间戳.log` 会话存档

## 环境要求

- Windows 10/11 + [Python 3.9+](https://www.python.org/downloads/windows/)（安装时勾选 *Add python.exe to PATH*）
- [Android platform-tools](https://developer.android.com/tools/releases/platform-tools) 中的 `adb.exe`、`fastboot.exe`

## 快速开始

1. 把 platform-tools 解压出的 `adb.exe`、`fastboot.exe`（含两个 `Adb*.dll`）放入 `tools/` 目录
2. 双击 `run.bat`
3. `文件 → 打开配置…` 选择 `profiles/ufi003_rustsimhub.json`（或新建空白配置）
4. 双击模块行，把「镜像文件」改为你实际构建出的镜像路径（相对配置文件目录）
5. 插入设备 → 「重启到 Bootloader」→ `① 生成预览` 核对 → `② 开始刷机…`

## 打包为单个 exe（可选）

双击 `build_exe.bat`（需要已装 Python），产物在 `dist/RustFlashKit/`，把 platform-tools 放进其 `tools/` 子目录即可分发。

## 配置档案格式

```json
{
  "schema": 1,
  "name": "UFI003 · RustSimHub 参考模板",
  "modules": [
    {
      "id": "boot",
      "name": "内核 boot",
      "partition": "boot",
      "image": "images/boot.img",
      "enabled": true,
      "description": "Linux 内核镜像",
      "sha256": "",
      "allow_missing": false,
      "allow_bootloader": false,
      "timeout": 300,
      "post_delay": 0
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `partition` | fastboot 分区名（自动校验合法性） |
| `image` | 镜像路径，相对路径基于配置文件所在目录 |
| `enabled` | 是否参与刷写（界面可勾选） |
| `required` | 必需模块：界面不可停用/删除 |
| `allow_missing` | 镜像缺失时跳过而不是报错 |
| `allow_bootloader` | 允许引导链分区（sbl1/aboot/tz…，高危） |
| `sha256` | 刷写前校验（留空不校验） |
| `timeout` | 单次 `fastboot flash` 超时秒数 |

## 安全须知（务必阅读）

- **刷机有变砖风险**。引导链分区刷错需要 9008 工具救砖，EFS 分区刷错会丢 IMEI 且无法恢复——本工具对后者直接拒绝。
- 刷写前确认镜像与设备型号匹配（UFI003_MB_V02 为 MSM8916；同平台其他棒子分区表可能不同）。
- 拿不准时先只刷 `boot` 验证链路，再刷 `rootfs`。
- 镜像来源请自行验证（建议填写 `sha256`）。

## 开发

```
python -m unittest discover -s tests   # 34 项核心逻辑测试（含假 adb/fastboot 端到端）
python -m rustflashkit                 # 启动 GUI
```

仅依赖 Python 标准库（tkinter）；无第三方包。

## 项目结构

```
RustFlashKit/
├── run.bat / build_exe.bat / rustflashkit.spec   # 启动 / PyInstaller 打包
├── profiles/            # 配置档案（UFI003 参考模板 + 空白模板）
├── tools/               # 放 adb.exe / fastboot.exe
├── rustflashkit/
│   ├── core/            # runner(执行) device(探测) modules(模型)
│   │                    # plan(计划) safety(安全) flasher(执行器) config(设置)
│   └── gui/             # app(主窗口) dialogs(编辑器/预览/设置)
└── tests/               # 单元测试 + GUI 冒烟脚本
```

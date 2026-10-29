# RustSimHub-UsbTool

给 UFI003（高通 MSM8916）4G 随身 WiFi 棒子用的 Windows USB 调试工具，单文件 exe，带图形界面。

**当前版本 0.1.0：9008 部分只有只读和演练，没有写入设备分区的功能。Wi-Fi 重连通过设备自己的接口完成（设备端还需要配合改动，见下文）。**

| 功能 | 状态 |
|---|---|
| 识别 9008（EDL）设备：高通 QDLoader 驱动（COM 口）或 WinUSB/libusb | 已实现，模拟测试通过，**未经实机验证** |
| 上传 Firehose 加载器、读主/备 GPT 并校验 CRC 和一致性 | 已实现，模拟测试通过，**未经实机验证** |
| 按分区或全盘备份到本地，长度 + SHA256 + 可选二次回读校验，离线复核 | 已实现，模拟测试通过，**未经实机验证** |
| 刷写演练：只核对，不连接设备、不写入 | 已实现，测试通过 |
| 真实刷写 | **未实现**（以后实现时每次写入都需要用户明确批准） |
| 经 USB 重连 Wi-Fi：工具自有密钥配对/登录、读取网络列表、试连接/确认/取消（45 秒自动回滚） | 工具端已实现，模拟测试通过；**需要设备端先改 3 处**（`docs/DEVICE-SIDE-WIFI-AUTH.md`），未经实机验证 |
| 导出短信和通话记录 | **未实现**，见 `docs/PLAN.md` |

## 使用

1. 准备 Firehose 加载器 `prog_emmc_firehose_8916.mbn`（高通专有文件，本工具不提供）。RustSimHub 用过的那份 SHA256 为 `959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14`，工具会识别它；其他加载器在使用前会再次要你确认。
2. 让棒子进入 9008：关机后按住按键插入 USB（或短接测试点）。设备管理器里应出现
   “Qualcomm HS-USB QDLoader 9008 (COMx)”；如果显示为未知设备，可以安装高通驱动，或用 Zadig 装 WinUSB。
3. 双击 `RustSimHub-UsbTool.exe`：
   - 选择加载器 → “检测 9008 设备” → “读取分区表”；
   - 在列表里选择分区（可多选，可勾选全盘备份）→ “备份所选分区”；
   - 备份保存在 exe 同目录的 `.local\backups\<时间>\`，内有 `manifest.json`（每个镜像的扇区范围、SHA256、校验状态）和主/备 GPT 原样副本；
   - “刷写演练”页：选择写前备份目录和镜像，点击“运行演练”。报告保存在 `.local\dryruns\`。
4. 默认操作结束后**不会**重启设备。如需让它回到正常系统，请勾选“完成后让设备正常重启”，或者拔插 USB。

命令行请用控制台版 `RustSimHub-UsbTool-cli.exe`（图形版从命令行运行时看不到输出）。每次任务的完整日志在 exe 同目录 `.local\logs\`。设备进入 9008 后只发一次 Sahara Hello，请进入后尽快读取；连接阶段超过 90 秒、读取停滞超过 120 秒都会自动停止并报告（不写入、不重试）。

命令行（功能相同）：

```
RustSimHub-UsbTool-cli.exe selfcheck
RustSimHub-UsbTool-cli.exe detect
RustSimHub-UsbTool-cli.exe probe  --loader prog_emmc_firehose_8916.mbn
RustSimHub-UsbTool-cli.exe backup --loader prog_emmc_firehose_8916.mbn --partition boot --partition __full_disk__
RustSimHub-UsbTool-cli.exe verify .local\backups\20261029-120000
RustSimHub-UsbTool-cli.exe dryrun --backup-dir .local\backups\20261029-120000 --write boot=boot.img
```

## Wi-Fi 重连（经 USB）

1. 用 USB 线连接棒子和电脑。Windows 上会出现一块新网卡（NCM）。在“网络连接”里把它的 IPv4 手动设为 `10.55.0.1`，掩码 `255.255.255.248`，不填网关（需要管理员权限，由你自己操作）。
2. “Wi-Fi 重连”页 → “连接并核对设备”。首次连接时会显示设备网址、证书指纹和设备公钥，请核对后再信任。之后证书一变，工具就会拒绝连接。
3. 首次授权：在设备网页上（主人登录）打开配对窗口 → 回到工具点“申请授权” → 在网页上核对申请编号和工具公钥后批准。工具只申请 `status.read` 和 `network.manage` 两项权限。
4. 以后每次只需“登录” → 读取网络列表或手动输入 → “试连接” → 状态变成“已连上”后点“确认保存”。45 秒内不确认，设备会自动恢复原网络。结果不明时只能“重新查询”（用同一个编号），不会自动重做。
5. 工具私钥用 Windows DPAPI 加密保存在 `.local/edge/client.key`，只有当前 Windows 账号能解开；在设备网页上撤销授权即可让它失效。

## 安全设计

- **只读护栏**：每条发往设备的 Firehose XML 都要先通过白名单，只允许以下几种：`configure`（必须 SkipWrite=1、eMMC）、`read`（不超出磁盘范围）、`getstorageinfo`、`nop`，以及显式请求时的一次 `power reset`。edl 中 12 个会改写存储或内存的方法（program/erase/patch/poke/rawxml 等）在运行时被替换为直接报错。`selfcheck` 命令会验证这一点。
- **硬编码禁写名单**：modem、fsg、modemst1/2、fsc、sec、persist、sbl1、rpm、tz、hyp、aboot 等分区（基带/NV/校准/引导链）。只有 boot、rootfs、NSHSTATE 可以作为演练目标，并且会按扇区范围检查是否与其他分区重叠。
- **演练要求**：目标分区必须在同一个备份目录里有已校验的写前备份，备份必须能通过离线复核，镜像（含 sparse 展开后的大小）不能超过分区大小。会生成计划摘要，供以后真实写入时逐次确认。
- **失败不重试**：读取失败或超时时，保留 `.partial` 文件和清单，不自动重试，也不自动重启设备。
- **隐私**：备份、日志、任务文件、设备序列号都只写到 `.local/`，`.gitignore` 已排除。不要把它们提交或发给别人。

## 风险

- 9008 是高通芯片的底层下载模式，加载器直接在设备内存里运行。来源不明的加载器有风险。
- 本工具不写设备，但进入和退出 9008 的物理操作本身，可能让设备停在下载模式，需要拔插恢复。
- 只读功能还**没有在实机上验证过**；第一次实机使用请按 RustSimHub 的设备约定，在有人值守、已有恢复手段时进行。
- 备份可能包含 IMEI、校准数据等设备身份信息，请妥善保管。

## 构建

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File build.ps1   # 先跑测试，再生成 dist\RustSimHub-UsbTool.exe
```

## 许可证

本程序按 **GPL-3.0-only** 发布（见 `LICENSE`）。它打包并调用了 [bkerler/edl](https://github.com/bkerler/edl)（GPLv3，© B. Kerler，固定在提交 `1cda1a6`），因此整个程序必须以 GPLv3 发布。分发 exe 时必须同时提供源码。只读护栏和 GPT 校验规则移植自 RustSimHub `tools/recovery/edl_readonly.py`。全部第三方组件见 `NOTICE.md`。Firehose 加载器是高通专有文件，不在本项目中分发。

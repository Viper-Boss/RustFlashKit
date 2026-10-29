# RustSimHub-UsbTool 方案与进度

2026-10-29 起草。目标设备：UFI003（高通 MSM8916）4G 随身 WiFi 棒子，主项目 RustSimHub。

## 决定（2026-10-29 用户确认）

用户表示这个工具只是偶尔使用，选最简单的方案：**Python + tkinter + bkerler/edl，用 PyInstaller 打包成单文件 exe，整体按 GPL-3.0-only 发布**。下面第 2 节中原来的 Rust 方案作废，分期和门禁设计仍然有效。

## 进度

| 期 | 状态 |
|---|---|
| M0 骨架 / 构建 exe / 许可证 | 完成：`build.ps1` 先跑测试再生成 `dist/RustSimHub-UsbTool.exe`；exe 的 `selfcheck`、`verify`、`dryrun` 已冒烟通过，界面窗口能启动 |
| M1 只读 EDL | 代码完成，模拟测试通过；**未经实机验证**（需要用户按键进入 9008） |
| M2 演练模式 | 完成，测试通过 |
| M3 真实写入 | 未开始 |
| M4 网络配置 | 未开始（待确认配对方式） |
| M5 导入导出 | 未开始（短信/通话导入需要主项目新增接口） |

本项目完成度约 **45%**：M0 10% + M1 按 70% 计 24.5% + M2 15%，取整。M1 剩下的 30% 是实机验证。

## 1. 调研结论

| 项目 | 语言/许可证 | 能做什么 | 本项目怎么用 |
|---|---|---|---|
| [bkerler/edl](https://github.com/bkerler/edl) | Python，GPLv3 | Sahara/Firehose 全功能，社区刷 UFI003/UZ801 的事实标准（`rl`/`rf` 备份、`qfil` 回刷） | **不打包、不复制代码**。只作协议行为参考和交叉验证工具。RustSimHub 已审过固定版本 `1cda1a6`，发现 program 在无正向 ACK 时可返回 true、USB 写循环按请求长度而非实际长度推进等问题——这正是不直接复用它做写入的理由 |
| [linux-msm/qdl](https://github.com/linux-msm/qdl) | C，BSD-3-Clause | 上传 Firehose 加载器、读写存储、rawprogram/patch XML | 协议实现主要参考（宽松许可证，可借鉴结构，需保留版权声明） |
| inomotech/qcom-edl（[镜像](https://zff.dev/inomotech/qcom-edl)） | Rust，MIT/Apache-2.0 | Sahara 基础支持（2025-10 起），Firehose 进度未知 | 编码前核对其现状；若可用则作为依赖或参考，保留许可证 |
| [qedl-rs](https://deepwiki.com/baiyao105/qedl-rs) | Rust，许可证未确认 | 号称纯 Rust EDL 栈 | 许可证不明前**不复用** |
| OpenStick / [stavros 笔记](https://notes.stavros.io/build-notes/msm8916-lte-stick-linux-port/) / [OpenWrt 论坛 UF896 帖](https://forum.openwrt.org/t/uf896-qualcomm-msm8916-lte-router-384mib-ram-2-4gib-flash-android-openwrt/131712/160) | 文档/脚本 | 通用流程：先 EDL 全盘备份，再 fastboot 刷 boot/rootfs | 流程参考；QFIL/Miko 找不到 9008 口的经验说明需要同时支持两种驱动 |
| RustSimHub 自身 `tools/recovery/`（AGPL-3.0，用户自有） | Python | 只读 GPT/全盘捕获、离线校验、恢复计划、严格写入协议策略 | **设计直接移植**：GPT 校验规则、固定分区布局、保护区前后摘要、ACK 严格解析、不重试未知写入 |

Firehose 加载器（`prog_emmc_firehose_8916.mbn`）是高通专有二进制，**不随本工具分发**；由用户从私有恢复材料提供，用 SHA256 钉住（RustSimHub 已用过的加载器摘要 `959439aa…dcab14`）。

## 2. 技术栈（建议 Rust）

- **Rust 单文件 exe**，GUI 用 egui/eframe（MIT/Apache），目标 `x86_64-pc-windows-msvc`，静态 CRT，便携免安装。另附同功能 CLI 子命令，便于测试和脚本化。
- USB 两种后端都支持：
  1. 高通官方 QDLoader 驱动 → 设备表现为 COM 口，用 `serialport` crate；
  2. WinUSB/libusb（Zadig 装驱动）→ `rusb`。
- GPT/CRC/SHA256：`crc32fast`、`sha2`；sparse 镜像解析自写（格式简单，便于测试）。
- 网络配置/导入导出走 HTTPS（`rustls`+`ureq`）和 Ed25519（`ed25519-dalek`，与 Core 一致）。
- 为什么不选 Python+bkerler/edl+PyInstaller：开发快，但整个程序被 GPLv3 约束、exe 体积大且易被杀软误报，写入路径还要绕开上游已知问题；Rust 与主项目一致，测试更容易控制。
- **许可证建议**：AGPL-3.0-only（与 RustSimHub 一致，可直接移植主项目代码）。若引入 BSD/MIT 代码，按原文保留版权声明到 `NOTICE.md` 和 `licenses/`。不包含任何 GPL 代码；若以后必须并入 bkerler/edl，则整体改为 GPLv3 兼容并在 README 写明。

## 3. 功能分期

| 期 | 内容 | 设备写入 | 验收 |
|---|---|---|---|
| M0 | 工程骨架、CI 在 Windows 构建 exe、许可证/NOTICE、README 风险说明 | 无 | GitHub Actions/本机产出 exe |
| M1 只读 EDL | 识别 05c6:9008（两种驱动）；Sahara hello、读芯片 ID（MSM8916 校验）；上传钉住的加载器；Firehose `configure`、`getstorageinfo`；读主/备 GPT 并校验 CRC 与 RustSimHub 已知布局（boot=p12、rootfs=p14、NSHSTATE=p15，盘长 3,909,091,328）；按分区/全盘备份到 `.local/` 并 SHA256 + 二次回读比对；正常 reset | **无**（客户端代码层面没有 program/erase/patch 指令） | 单元测试 + 模拟设备测试；实机只读需用户按键进 9008 |
| M2 演练模式 | 载入刷写计划（分区→镜像）；核对：目标分区存在、镜像长度/sparse 展开后不超分区、禁写名单、镜像摘要、本机已有**同一设备**（GPT 摘要匹配）且校验通过的备份、恢复入口记录；输出差异报告 | **无** | 测试覆盖各拒绝路径 |
| M3 真实写入（门禁） | 仅 boot/rootfs/NSHSTATE 等系统分区；先全盘前像备份；写→全量回读→保护区摘要不变→才允许 reset | 有，**每次用户逐次批准** | 先模拟设备测试；实机须用户在场并单独授权 |
| M4 网络配置 | 对 RustSimHub 设备：通过 USB 网络（usb0）连 Edge HTTPS，以**已配对客户端**身份调用现有 `wifi_networks`/`wifi_begin`/`wifi_status`/`wifi_confirm`/`wifi_cancel`（含 45 秒回滚）；不绕过 owner 鉴权 | 设备配置变更（由 Core 自身事务完成） | 依赖 owner 在网页批准配对 |
| M5 导入/导出 | 导出：经已授权 API 读取短信/通话（`history_page` 等）存为 JSON/CSV 到用户选定目录；或经已有维护 SSH 拉取 Core 的 SQLite Backup API 备份后本地只读解析。导入：配置（Wi-Fi 等）经 M4 接口；短信/通话**只能**按 `rustsimhub-migrate` 规则导入为只读归档 | 视功能 | 见第 5 节待确认 |

## 4. 安全门禁设计

1. **硬编码禁写名单**（编译进程序、无配置可改）：`modem`、`fsg`、`modemst1`、`modemst2`、`fsc`、`sec`、`sbl1`/`sbl1bak`、`rpm`/`rpmbak`、`tz`/`tzbak`、`hyp`/`hypbak`、`aboot`/`abootbak`、`DDR`、`ssd`、`persist`、`devinfo`、`config`、`misc`、`keystore`、`oem`、`splash`，以及 GPT 本身和 boot0/boot1；未在**允许名单**（初版仅 `boot`、`rootfs`、`NSHSTATE`）中的分区一律拒绝。允许/禁写都按扇区范围复核，不只看名字（防同名或改名）。
2. **双层隔离**：默认构建根本不含写指令代码；真实写入只在单独的 `write` 特性构建中存在，界面用醒目颜色区分。
3. **演练优先**：任何写入前必须先对同一计划跑通演练，得到计划摘要。
4. **逐次批准**：每次写入弹窗显示设备芯片 ID、目标分区、扇区范围、镜像摘要；用户手动输入分区名 + 计划摘要前 8 位才放行；批准只对这一次有效，记录“尝试已消耗”标记，结果不明**不自动重试**。
5. **前像与回读**：写前全盘备份并校验；写后全量回读目标分区摘要、保护区（含 modem/NV）**当前**摘要前后一致，才允许 reset。失败保留现场，不自动 reset。
6. **加载器钉住**：只接受 SHA256 匹配的加载器文件；芯片 ID 非 MSM8916 拒绝。
7. **隐私**：备份、日志、设备序列号、GPT 摘要只写到 `.local/`（工具目录内，`.gitignore`），界面截图/报告默认脱敏；仓库不提交任何设备材料。
8. **不提供任意命令**：无自由 Firehose XML、无任意 shell/AT/QMI；网络与导入导出只走 Core 已有鉴权接口。
9. **超时与单例**：每个 USB 操作有上限；全局文件锁防止两个实例同时操作同一设备。

## 5. 需要用户确认的问题

1. 技术栈按 Rust + egui 单文件 exe？（备选：Python + bkerler/edl，GPLv3）
2. 许可证选 AGPL-3.0-only（与主项目一致）？
3. Windows 驱动：你电脑上棒子进 9008 时，显示为“Qualcomm HS-USB QDLoader 9008 (COMx)”还是需要 Zadig/WinUSB？（两种都会做，决定先测哪种）
4. 导入导出：RustSimHub 目前**没有**把短信/通话写回正式数据库的接口，只有离线 `rustsimhub-migrate` 导入只读归档；Edge 也未见批量导出接口。是否接受：本工具先做“导出”和“配置导入”，短信/通话导入需主项目另加接口（属于 RustSimHub 改动，不在本仓库里做）？
5. 网络配置：本工具以“已配对客户端”身份调用（需要你在网页上批准一次配对），可以吗？

## 6. 完成度刻度（本项目自身）

M0 10%｜M1 35%｜M2 15%｜M3 15%（真实写入实机验收另需用户批准）｜M4 10%｜M5 15%。当前 0%。

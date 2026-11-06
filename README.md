# RustSimHub-UsbTool

给 UFI003（高通 MSM8916）4G 随身 WiFi 棒子用的 Windows USB 调试工具，单文件 exe，带图形界面。

**当前版本 0.1.0：9008 部分只有只读和演练，没有写入设备分区的功能。Wi-Fi 重连通过设备自己的接口完成（设备端还需要配合改动，见下文）。**

| 功能 | 状态 |
|---|---|
| 识别 9008（EDL）设备：高通 QDLoader 驱动（COM 口）或 WinUSB/libusb | 已实现；2026-10-29 实机通过（USB/WinUSB 方式） |
| 上传 Firehose 加载器、读主/备 GPT 并校验 CRC 和一致性 | 已实现；2026-10-29 实机通过（USB/WinUSB 方式） |
| 按分区或全盘备份到本地，长度 + SHA256 + 二次回读校验，离线复核；回读不一致时定位扇区，可选第三遍多数表决另存“尽力而为”镜像（标为未验证，演练不会采用） | 已实现；实机上正确识别出一块读不稳的二手 eMMC 并拒绝备份；在稳定设备上完整通过尚未实测 |
| 读取 eMMC 寿命（只读）：开机状态经维护 SSH 读 sysfs；9008 状态经 getstorageinfo，能否读到取决于加载器 | 已实现，模拟测试通过；**未经实机验证** |
| 刷写演练：只核对，不连接设备、不写入 | 已实现，测试通过 |
| 一键刷机：导入签名刷机包，自动预检、备份、分阶段写入 | 已实现，模拟设备测试通过；分区表、aboot、boot 的写入方式真机验证过，**完整一键流程和 NSHSTATE/rootfs 未经实机验证，刷机包还没有生成** |
| 受限写入（按计划文件一次一步，只限命令行、只限 USB 方式） | 已实现，模拟设备测试通过；**从未对真实设备写入过**，每次真实写入都需要用户单独批准 |
| 经 USB 重连 Wi-Fi：工具自有密钥配对/登录、读取网络列表、试连接/确认/取消（45 秒自动回滚） | 工具端已实现，模拟测试通过；**需要设备端先改 3 处**（`docs/DEVICE-SIDE-WIFI-AUTH.md`），未经实机验证 |
| 导出短信和通话记录 | **未实现**，见 `docs/PLAN.md` |

## 使用

1. Firehose 加载器 `prog_emmc_firehose_8916.mbn` 是高通专有文件，源码仓库不包含。如果本机有 `..\RustSimHub\.private\prog_emmc_firehose_8916.mbn` 且摘要相符，`build.ps1` 会把它打进 exe，运行时不用再选。**这样打出来的 exe 只能自用，不能公开发布或发给别人。**没有内置时需要自己选择。RustSimHub 用过的那份 SHA256 为 `959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14`，工具会识别它；其他加载器在使用前会再次要你确认。
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

## 一键刷机

把原厂 UFI003 刷成 RustSimHub 系统。在“一键刷机”页：

1. 导入刷机包（`.rshpack`）。工具校验 Ed25519 签名和每个镜像的摘要，签名不对就拒绝，没有跳过校验的选项。
2. 点“开始刷机”，输入“确认刷机”。这次确认覆盖下面整个流程。
3. 按提示拔插进 9008，工具自动备份 NV 和启动链（每个分区读两遍）并检查芯片，然后写新分区表。
4. 再按提示拔插一次（加载器只认连接时的一次配置，块大小也在那时定下），工具自动写 aboot、boot、NSHSTATE、rootfs。已经写对的会跳过，每一步都回读三遍并比对保护区。全部通过后请求棒子正常重启。

以下情况会在写入前拒绝：分区表不是支持的原厂或 RustSimHub 布局、芯片读不稳、tz 与 tzbak 不一致、原设备布局。基带/NV/校准分区不会被写，tz、sbl1、rpm、hyp 也不写。中途失败不会自动重试；重新开始时已写好的内容自动跳过，上次失败的那一步要你再确认一次才重试。

刷机包由发布方生成（镜像长度必须是 16 KiB 的整数倍，私钥不进 git）：

```
RustSimHub-UsbTool-cli.exe pack-create --output x.rshpack --name RustSimHub --version 1.0 --key <私钥> --aboot a.img --boot b.img --nshstate s.img --rootfs r.img
RustSimHub-UsbTool-cli.exe flash --pack x.rshpack --i-understand-this-erases-android
```

## 读取 eMMC 寿命

寿命和保留块状态存在 eMMC 的 EXT_CSD 里，由芯片自己估算，只分 10% 一档。eMMC 5.0 之前的芯片可能不提供，工具会显示“无法判断”，不会说成正常。

- **开机状态（可靠）**：棒子正常运行 RustSimHub 系统时，经 USB 网络和维护 SSH 执行一条固定的只读命令，读 `/sys/block/mmcblk0/device/` 下的 `life_time`、`pre_eol_info`。只用公钥登录，必须提供 known_hosts 严格核对主机。不读序列号，也没有任意命令入口。需要系统里有 OpenSSH 客户端。

```
RustSimHub-UsbTool-cli.exe emmc-life --identity <维护私钥> --known-hosts <known_hosts 文件>
```

- **9008 状态（看加载器）**：标准 Firehose 没有读 EXT_CSD 的命令，只能试 `getstorageinfo`。UFI003 常用的 8916 加载器不支持，工具会如实提示。工具不会为此读写芯片寄存器。

```
RustSimHub-UsbTool-cli.exe emmc-life --loader <加载器> --mode usb
```

图形界面在“芯片寿命”页。

## 受限写入（命令行）

用于把读不稳的二手 UFI003 改造成测试机（RustSimHub PR #73）。界面里没有写入功能，只能用命令行，且只支持 USB（WinUSB）方式。

1. 计划文件（JSON，`kind: usbtool-write-plan`）列出每一步：目标（只能是 `tz`、`aboot`、`boot`、`NSHSTATE`、`rootfs`、`gpt`）、写入段（起始扇区、扇区数、载荷文件、SHA256）、写入前设备 GPT 的摘要；GPT 步骤还要给出写入后的新摘要。另有保护区段列表，必须覆盖 modem、fsg、modemst1、modemst2、fsc、sec、persist 中设备上存在的每一个。
2. 先离线核对（不打开 USB）：`RustSimHub-UsbTool-cli.exe write-dryrun --plan plan.json --backup-dir <含当前 GPT 的备份目录>`。它按顺序核对每一步（GPT 步骤之后改用新表），并给出每一步的**步骤摘要**。
3. 用户在聊天里核对并批准某一步的步骤摘要后，才执行这一步：`RustSimHub-UsbTool-cli.exe write --loader <加载器> --mode usb --plan plan.json --step tz --confirm <步骤摘要>`。

硬性规则（代码写死，计划文件改不了）：
- sbl1、rpm、hyp、各备份分区（tzbak、abootbak 等）以及全部基带/NV/校准分区永远不能作为目标；重写 GPT 时，这些分区的 128 字节表项必须原样保留。
- tz、aboot、GPT 这类启动链写入，只允许在**不是原设备布局**的设备上进行。
- 设备当前的 GPT 摘要必须等于该步骤绑定的值。写入段必须从目标分区起点开始、不超过分区，且不与任何保护区段重叠。
- **每次写入前都要拔插、重新进入 9008，然后立即执行。** 写入会话必须是加载器启动后的第一次连接：连接时只发送一次 `configure`（SkipWrite=0，块大小也在这时确定）。2026-11-01 真机上，先发 SkipWrite=1、再发 SkipWrite=0，加载器两次都回 ACK，但数据没有写入。设备已处于 Firehose 模式（之前连接过）时，工具会拒绝写入。
- 连接时 configure 里的 MaxPayload 取能整除这一步每段长度的块大小（不超过 16384 的 2 的幂，最小 512），保证每一块都是满块。2026-10-30 真机上，GPT 主表按 16384+1024 发送时被加载器 NAK，且没有写入任何数据。`program` 必须先收到 `rawmode="true"` 的明确 ACK 才发数据；每块按实际写出字节核对，并补零长度包；数据发完必须收到 `rawmode="false"` 的明确 ACK。短写、超时、NAK 一律终止，不重发。
- 写前：保护区读两遍，记下哪些扇区读得稳；目标区的原内容另存为前像。写后：目标区回读三遍，每遍都要等于载荷；保护区再读一遍，逐扇区比较；重新读 GPT 核对摘要。
- 结果写在 `.local\writes\<时间>\report.json`，状态为下列之一：`verified`、`not_written`（设备回了 ACK，但回读仍是写入前的旧内容）、`written_majority`（每个扇区三次回读中至少两次等于载荷，但有读数在跳；不算通过，每次回读的原始数据另存在报告目录）、`written_unprovable`（有保护扇区本来就读不稳，无法证明它们未变）、`readback_mismatch`、`protected_changed`、`gpt_mismatch`、`write_failed`。
- 每一步执行前都会在 `.local\write-attempts\` 写一个标记，同一步不能被自动再次执行。不会自动重启设备。

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

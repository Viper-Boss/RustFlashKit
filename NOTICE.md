# 第三方组件与来源

本程序整体按 **GNU GPL v3（GPL-3.0-only）** 发布，全文见 `LICENSE`。原因是它在同一个进程/安装包中使用并打包了 GPLv3 的 bkerler/edl。

| 组件 | 版本 | 许可证 | 用途 | 来源 |
|---|---|---|---|---|
| bkerler/edl（edlclient） | 提交 `1cda1a616748d608667b2d13b73a28d3197ceb45`（3.62） | GPLv3，© B. Kerler 2018-2025 | Sahara/Firehose 通信；本工具不修改其源码，只在运行时禁用其写入方法并过滤 Firehose XML | https://github.com/bkerler/edl |
| edl 自带 libusb-1.0.dll | 随上述提交 | LGPL-2.1+（libusb 项目） | Windows 上的 WinUSB/libusb 访问 | https://libusb.info |
| pyusb | 1.3.1 | BSD-3-Clause | USB 枚举 | https://github.com/pyusb/pyusb |
| pyserial | 3.5 | BSD-3-Clause | COM 口（高通 QDLoader 驱动） | https://github.com/pyserial/pyserial |
| docopt | 0.6.2 | MIT | edl 依赖 | https://github.com/docopt/docopt |
| lxml | 6.0.2 | BSD-3-Clause | edl 依赖 | https://lxml.de |
| pycryptodome / pycryptodomex | 3.23.0 | BSD-2-Clause / 公有领域部分 | edl 依赖 | https://www.pycryptodome.org |
| colorama | 0.4.6 | BSD-3-Clause | edl 依赖 | https://github.com/tartley/colorama |
| cryptography | 46.0.3 | Apache-2.0 或 BSD-3-Clause | Ed25519 配对/登录签名、证书解析 | https://cryptography.io |
| Python 运行时（PyInstaller 打包） | 3.12 | PSF License | 解释器 | https://www.python.org |
| PyInstaller 引导程序 | 6.22.3 | GPLv2+ 带打包例外 | 生成单文件 exe | https://pyinstaller.org |

## 移植自 RustSimHub 的设计

GPT 主/备校验规则、Firehose XML 白名单、禁用 edl 写入方法的做法移植自同一作者的 RustSimHub 项目
`tools/recovery/edl_readonly.py`（原为 AGPL-3.0-only，由版权人在本项目中以 GPL-3.0-only 重新发布）。
严格写入传输规则（明确 ACK 解析、按实际写出字节核对、短写不重试、零长度包）移植自同一项目的
`tools/recovery/restore_protocol.py`，许可证处理同上。

## 不包含的内容

- **高通 Firehose 加载器**（如 `prog_emmc_firehose_8916.mbn`）是高通专有二进制，本工具不分发，需由用户自己提供；
- 任何设备镜像、备份、设备标识。

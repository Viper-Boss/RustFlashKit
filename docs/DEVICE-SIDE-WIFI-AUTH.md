# 工具经 USB 改 Wi-Fi：需要 RustSimHub 设备端配合的改动

2026-10-29 用户决定：Wi-Fi 重连由本工具通过 USB 直接完成，给工具单独授权，不走网页配网。

工具端已实现（本仓库 `usbtool/edge.py`、`usbtool/gui_wifi.py`）：工具在 `.local/edge/client.key` 生成自己的 Ed25519 密钥（用 Windows DPAPI 加密）；首次经 USB 连接时钉住设备证书 SHA256 和设备公钥，由用户核对；之后按 `proof.js` 相同的规则，用 `pair_challenge`/`pair_proof` 申请 `status.read + network.manage`，用 `challenge`/`authenticate` 登录，再调用 `wifi_networks`、`wifi_begin`、`wifi_status`、`wifi_confirm`、`wifi_cancel`。这部分只在模拟 Core 和本地 TLS 服务器上测试过，**没有连接真实设备**。

按当前 RustSimHub 代码，下面三处还不支持，需要在 RustSimHub 仓库里另开任务修改（本仓库不改 RustSimHub）：

1. **Core 权限**：`crates/core/src/network_trial/provision.rs` 的 `owner()` 要求主人身份（`recent_owner`）。需要改为：主人，或已被主人批准、且带 `network.manage` 的 client，可以调用 `wifi_networks` 和 `wifi_begin/status/confirm/cancel`。`wifi_networks` 在 `crates/core/src/network/wifi.rs` 中同理。必须补失败路径测试：未批准、已撤销、generation 变化、缺少 `network.manage`、kind=nas 都要拒绝。
2. **网页批准入口**：Core 已有 `open_pairing`、`pending_pairings`、`approve`、`revoke`，但网页没有对应界面（`crates/edge/src/web/` 中搜不到）。需要在主人页面加入：“允许新设备配对（5 分钟）”、待批准列表（显示申请编号、公钥全文、权限）、批准按钮和撤销按钮。
3. **USB 上可访问 Edge**：工具连接 `10.55.0.2:8443`（usb0），发送的 Host/Origin 是证书里的 `.local` 网址。需要确认日常 Edge 也监听 usb0 地址（`additional_listen_ips` 包含 10.55.0.2），并且证书 SAN 含该 `.local` 名称。

部署到设备需要用户逐次批准。电脑端：Windows 10/11 自带 NCM 驱动，棒子插上后会出现一块新网卡，需要手动设为 `10.55.0.1`、掩码 `255.255.255.248`、不设网关（设备端没有 DHCP）。

## 进展

2026-10-29：三处已在 RustSimHub PR #68（https://github.com/Viper-Boss/RustSimHub/pull/68，分支 `claude/wifi-tool-auth-e30f13`，提交 `70a6241`）实现，本地测试通过；CI 因 GitHub 账户付费问题未运行；未合并、未部署、未连真实设备。对接差异：Core 拒绝统一为 `{"ok":false,"error":"denied"}`，工具已改为列出可能原因；其他字段与工具假设一致。

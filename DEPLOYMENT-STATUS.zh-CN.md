# 当前服务器部署状态

- 服务器：`192.144.235.126`，SSH 用户 `ubuntu`；实际系统为 Ubuntu 24.04。
- 本机现有 `~/.ssh/id_rsa` 能直接登录，不需要重新生成 SSH 密钥。
- API 已部署到 `/opt/doubao-cloud/server`，由 `doubao-cloud.service` 管理并开机自启。
- API 仅监听服务器本机 `127.0.0.1:8000`；`/healthz` 返回 200，未带令牌访问任务接口返回 401。
- 通过 SSH 隧道用真实令牌测试创建和查询任务成功；测试任务已删除。
- 凭据保存在本机单独的 `outputs/doubao-cloud-credentials.json`，未放入源码 ZIP。服务器环境文件位于 `/etc/doubao-cloud.env`，权限为 root 只读。
- 公网 API 地址：`https://192.144.235.126`。访问 `/healthz` 返回 200，TLS 证书验证通过；未带令牌的任务请求返回 401。
- 域名 API 地址：`https://www.zcbox.top`。2026-09-27 已由 Caddy 自动签发并启用 Let's Encrypt 域名证书；域名和原 IP 地址的 HTTPS `/healthz` 均验证返回 200。HTTP 域名请求自动跳转 HTTPS。systemd 部署的 Caddy 配置见 `deploy/Caddyfile.systemd`。
- 证书是 Let's Encrypt 签发给公网 IP 的短期证书，约 6 天有效。`doubao-cert-renew.timer` 每天两次检查续期，更新后自动重载 Caddy；续期演练已通过。
- Caddy、任务 API、续期定时器均已启用开机自启。已从 Mac 通过公网 HTTPS 验证客户端鉴权和 Worker 心跳。
- 2026-09-28 已升级服务器 API，加入七牛直传的限时凭证、对象核验和下载地址接口。已配置公开空间 `doubao-box`（区域 `z2`）及默认测试域名 `http://tm2vodhyi.hn-bkt.clouddn.com`；线上链路测试通过后，测试任务和测试对象均已清理。升级后域名 HTTPS 健康检查与未授权 401 均通过。
- 配置使用的七牛 AK/SK 来自用户截图；截图已暴露完整密钥，必须在七牛控制台轮换密钥后再继续生产使用，并把新密钥只更新到 `/etc/doubao-cloud.env`。

下一步：在 Windows 云电脑上配置并启动 Worker，运行一条真实豆包生成任务。截图中的云平台防火墙仍允许入站 TCP 8000；API 实际只监听 `127.0.0.1:8000`，建议在控制台删除这条多余规则，保留 22、80、443。当前参考图逐任务保存、MP4 保存在服务器，尚无去重或自动清理。

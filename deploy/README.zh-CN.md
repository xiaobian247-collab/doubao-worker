# Linux 服务器部署

当前服务器 `192.144.235.126` 已使用 systemd + Caddy 部署，无需再执行下面的 Docker 步骤。
实际部署状态见上一级的 `DEPLOYMENT-STATUS.zh-CN.md`。

适用于已安装 Docker Engine 和 Docker Compose 插件的 Linux 服务器。
域名的 A/AAAA 记录须指向该服务器；开放入站 TCP 80、443。

1. 将整个 `doubao-cloud` 目录传到服务器，例如 `/opt/doubao-cloud`。
2. 进入 `deploy`，把 `env.example` 复制为 `.env`，填入真实域名和各端令牌。
3. 创建 `data` 目录，让 UID 10001 可写：

   ```sh
   mkdir -p data
   sudo chown 10001:10001 data
   chmod 700 data
   chmod 600 .env
   ```

4. 启动并检查：

   ```sh
   docker compose up -d --build
   docker compose ps
   curl -f https://你的域名/healthz
   ```

日志查看：`docker compose logs -f api`。数据库、参考图和视频存于
`deploy/data`，升级容器时不会随容器删除。定期备份该目录。

不要公开 8000 端口；Compose 只将 Caddy 的 80/443 暴露到主机。
Worker 用 `.env` 中对应的 Worker 令牌与 HTTPS 域名连接服务端。

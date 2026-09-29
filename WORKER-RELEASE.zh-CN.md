# GitHub 自动构建与 Worker 更新

推荐把本目录上传到 GitHub Private 仓库。不要上传 `config.json`、服务器 `.env`
或任何令牌；`.gitignore` 已排除常见本地密钥文件。

## 自动构建

每次推送 `main`，GitHub Actions 会在 Windows x64 环境打包并保存
`DoubaoWorker-windows-x64` 构建产物，里面有 exe、配置模板和说明。
云机只需下载这个产物，无须安装 Python。

发布正式更新时，先修改 `worker/VERSION`，例如从 `0.1.0` 改为 `0.1.1`，
推送代码并创建相同版本的 tag：

```sh
git tag worker-v0.1.1
git push origin main worker-v0.1.1
```

Actions 会创建 GitHub Release，并把 exe 上传到 API 服务器。上传成功后，
Worker 会在下次启动或空闲时检查版本；只有版本号更高时才下载，校验大小、
SHA-256 和 Windows EXE 文件头，退出旧进程，替换 exe 并重启。

## 服务器配置

服务器设置 `DOUBAO_WORKER_RELEASE_TOKEN`，使用独立的高强度随机 ASCII 值。
GitHub 仓库的 Actions Secret `WORKER_RELEASE_TOKEN` 使用同一个值。它只供
GitHub Actions 发布 exe，不能放进 Worker 配置。服务端将 exe 默认保存在
`DOUBAO_DATA_DIR/worker-release`；该目录必须持久化，且不能公开共享。

Worker 使用已有的 `worker_id` 和 `worker_token` 访问更新接口：

- `GET /api/workers/{worker_id}/update`：版本、大小、SHA-256。
- `GET /api/workers/{worker_id}/update/download`：下载 exe。

`auto_update` 默认开启。设为 `false` 或启动时加 `--no-update` 可暂停自动更新。
生成任务执行期间不会更新。

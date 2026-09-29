# 豆包云任务系统（第一版）

Windows Worker 的 GitHub Actions 自动打包与服务器更新流程见
`WORKER-RELEASE.zh-CN.md`。云机可以直接下载构建好的 EXE，不需要安装 Python。

当前云服务器已部署，公网地址是 `https://192.144.235.126`。
实际安装状态和验收结果见 `DEPLOYMENT-STATUS.zh-CN.md`。

目录中有三个程序：`server/app.py` 是云端任务调度服务，`worker/agent.py`
运行在每台 Windows 云电脑，`client.py` 在 Mac 上提交任务和下载视频。
其他 Python 程序可直接导入 `doubao_client.py`，调用方法见
`INTEGRATION.zh-CN.md`；HTTP 请求格式见 `API.zh-CN.md`。
Worker 已带上你提供的 v0.7.17 插件核心代码；豆包管理器原程序不在包内，
也不会被修改。

## 运行条件

- 三端使用 Python 3.11。Windows 云电脑要保持已登录的图形桌面会话。
- 豆包管理器里先登录好账号，并保持整个绿色版目录完整。
- 云端对外提供 HTTPS；不要将管理器的 `9223` 端口暴露到公网。
- Mac 客户端和每台 Worker 分别使用不同的长随机 ASCII 令牌。

## 1. 云服务器

若服务器已安装 Docker 和 Compose，可以按 `deploy/README.zh-CN.md`
完成 HTTPS、容器和持久化目录部署。以下是不使用容器的运行方式。

在 Linux 服务器上进入 `server` 目录：

```sh
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
export DOUBAO_DATA_DIR=/var/lib/doubao-cloud
export DOUBAO_CLIENT_TOKEN='replace_with_random_ascii_client_token'
export DOUBAO_WORKER_TOKENS='{"worker-01":"random_ascii_worker_01_token","worker-02":"random_ascii_worker_02_token"}'
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

用 Nginx 或 Caddy 将你的 HTTPS 域名转发到 `127.0.0.1:8000`。
`DOUBAO_DATA_DIR` 必须由服务进程可写。这里保存 SQLite 数据库、参考图和视频；
第一版视频会经过这台服务器，单文件上限 2 GB。SQLite 版本只启动一个
Uvicorn 进程。增加多个 API 进程时，需要迁移到 PostgreSQL。

## 2. 每台 Windows 云电脑

把 `worker` 文件夹复制到云电脑的可写目录。把
`config.example.json` 复制为 `config.json`，填写：

- `server_url`：填写 `https://192.144.235.126`。
- `worker_id`：每台云机唯一，例如 `worker-01`、`worker-02`。
- `worker_token`：与服务器 `DOUBAO_WORKER_TOKENS` 中对应的令牌一致。
- `manager_exe`：这台云机上豆包管理器 EXE 的完整路径。

在 PowerShell 中首次运行：

```powershell
cd C:\DoubaoCloud\worker
py -3.11 -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\python.exe agent.py --check
.\.venv\Scripts\python.exe agent.py
```

`--check` 只验证 HTTPS、Worker 令牌和本机管理器状态，不领取任务；
先确认它显示 `worker_auth: ok`，再启动正式 Worker。

Worker 会先连接本机 `127.0.0.1:9223`；管理器未启动时尝试按
`manager_exe` 启动。每台云机只运行一个 Worker 实例，每次只执行一个任务。
需要 EXE 时，在 Windows PowerShell 运行 `build-worker.ps1`，输出位于
`dist\DoubaoWorker.exe`。部署 EXE 时必须保留旁边的 `dist\plugin` 文件夹，
并把填写好的 `config.json` 放在 EXE 旁。可通过 Windows 任务计划程序设置
“用户登录时启动”，但云电脑平台侧的自动关机策略仍需单独解决。

## 3. Mac 提交任务

在当前这台 Mac 上，本机凭据已保存在项目外层的
`outputs/doubao-cloud-credentials.json`。先在项目根目录运行：

```sh
python3.11 outputs/doubao-cloud/local_client.py check
```

它只检查服务器、令牌和任务数量，不会创建任务或消耗生成额度。
Windows Worker 安装并启动后，可用以下命令提交和查询一条真实任务：

```sh
python3.11 outputs/doubao-cloud/local_client.py submit '夜晚城市街道，电影感运镜' --duration 10 --ratio 16:9
python3.11 outputs/doubao-cloud/local_client.py status 任务ID
python3.11 outputs/doubao-cloud/local_client.py download 任务ID --output result.mp4
```

提交返回的 `id` 就是后两条命令使用的任务 ID。没有 Worker 时，任务会
停留在 `queued`，不会自动生成视频。

如果换到其他电脑、没有这份本机凭据文件，再使用下面的通用方式：

```sh
python3.11 -m pip install requests
export DOUBAO_SERVER_URL='https://192.144.235.126'
export DOUBAO_CLIENT_TOKEN='replace_with_random_ascii_client_token'
python3.11 client.py submit '夜晚城市街道，电影感运镜' --duration 10 --ratio 16:9
python3.11 client.py status 任务ID
python3.11 client.py download 任务ID --output result.mp4
```

参考图可重复传入 `--reference image.png`，最多九张。提交时使用
`--key 项目-镜头-版本` 可防止网络重试创建重复任务。
`client.py workers` 查看各 Worker 上次心跳和账号页面数量。

## 任务恢复规则

Worker 每 5 秒尝试领单，每 15 秒发送心跳。任务进入生成入口之前，租约
过期后可以重新派发。进入生成入口之后，插件没有可靠的外部“豆包已受理”
回调，因此发生失败或失联时会变为 `recovery_required`，不会自动换号
重发。应先检查原账号的生成结果和 Worker 本地 `data/jobs/任务ID`，
避免重复扣额度。上传失败时，成片可能仍保存在该本地目录中。

当前已通过调度和模拟 Worker 的自动测试，但还没有在 Windows 云机上跑
真实豆包生成，也没有在 Windows 上验证 PyInstaller 产出的 EXE。先在目标
云机完成一条真实任务，验证登录状态、页面自动化和视频下载，再长期运行。

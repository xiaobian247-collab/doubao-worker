# Windows Worker 安装

GitHub 仓库的 Actions 每次推送 `main` 会自动生成 Windows x64 版
`DoubaoWorker.exe`。在 Actions 页面下载 `DoubaoWorker-windows-x64`，解压后
直接使用，云机不需要 Python。正式版本也会发布到 GitHub Releases。

此源码包还包含 Windows 本地构建脚本。当前 Mac 环境没有 Windows
编译器，所以不能直接在 Mac 上可靠地产出 `.exe`；请在你的 Windows x64
云机上构建一次。云机不需要预装 Python，脚本会自动下载便携 Python、安装依赖
并生成 `DoubaoWorker.exe`。

## 1. 配置

复制 `config.example.json` 为 `config.json`，填写：

- `server_url`：填写 `https://www.zcbox.top`；原 IP HTTPS 地址仍可用。
- `worker_id`：第一台云电脑填 `worker-01`。
- `worker_token`：从 Mac 的 `doubao-cloud-credentials.json` 取对应值，切勿发送到聊天或写入公开目录。
- `manager_exe`：这台云电脑上 `豆包管理器.exe` 的完整路径。

七牛密钥只配置在服务器，Worker 不需要 AccessKey 或 SecretKey。

## 1.1 多云机自动注册（个人使用）

服务器设置一个私有的 `DOUBAO_WORKER_ENROLL_TOKEN` 后，每台新云机只需运行一次
`install-worker.ps1`。脚本会从同一服务器下载豆包管理器、自动生成 `wkr-UUID`、
向服务器注册、写入本机 `config.json`，之后每台云机会使用自己的 token 参与排队，
不需要手动修改服务器的 Worker token 列表：

```powershell
$env:DOUBAO_WORKER_ENROLL_TOKEN = '服务器上的同一个安装密钥'
powershell -ExecutionPolicy Bypass -File .\install-worker.ps1
```

服务器上的压缩包包含完整的 `豆包管理器.exe` 绿色版目录。安装脚本会校验
SHA-256、下载 Worker、解压管理器、自动注册当前云机，并设置用户登录时启动。
已安装的云机再次运行脚本时会复用本机已有身份。不要把安装密钥放进公开
GitHub 仓库或公开脚本 URL。

## 2. 无 Python 构建 exe（推荐）

在 PowerShell 中进入解压后的 `worker` 目录，执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build-worker-portable.ps1
```

脚本第一次运行需要联网访问 `python.org`、`bootstrap.pypa.io` 和 PyPI，
会在 `.portable-build` 中缓存构建环境。完成后生成：

```text
dist\DoubaoWorker.exe
dist\plugin\
dist\config.example.json
```

把 `config.example.json` 复制为 `dist\config.json` 并填写 token。之后只要把
`dist` 目录整体保留，运行 `dist\DoubaoWorker.exe` 就不再需要 Python。
GitHub Actions 构建的 exe 已内置 plugin 和版本号，直接放在 `config.json` 旁即可。
Worker 启动和每五分钟空闲时会从服务器检查正式更新；生成任务执行期间不会更新。

## 3. 检查连接

在 `dist` 目录中执行：

```powershell
.\DoubaoWorker.exe --check
```

应显示 `server_health: {'ok': True}` 和 `worker_auth: ok`。
如果管理器已经启动且开放本机端口 9223，还会显示账号页面数量。
该命令不会领取任务。确认后运行：

```powershell
.\DoubaoWorker.exe
```

## 4. 源码方式（可选）

如果云机已经安装 Python 3.11，也可以跳过构建，直接运行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe agent.py --check
.\.venv\Scripts\python.exe agent.py
```

服务器配置七牛后，Worker 会领取限时上传凭证并直接上传视频；尚未配置时保持
原有的上传服务器路径。

说明：GitHub Actions 已在 Windows 构建出 exe，但还需要在你的云机上用
`--check` 验证管理器环境和网络连接。

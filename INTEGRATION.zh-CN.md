# 其他 Python 程序接入

可导入的客户端文件是 `doubao_client.py`。它只负责和云服务器交互：提交
提示词、上传参考图、查询状态、下载视频。Windows Worker 会自己从服务器
领取任务和下载参考图；调用方不用连接 Worker 或豆包管理器。

依赖：Python 3.11 和 `requests`（`python3.11 -m pip install requests`）。
把 `doubao_client.py` 放到你的程序可导入的目录，或者把本项目目录加入
`PYTHONPATH`。

## 在当前这台 Mac 上调用

```python
from doubao_client import DoubaoClient

client = DoubaoClient.from_credentials_file(
    "/Users/xiaobian/Documents/Codex/2026-09-26/new-chat-4/outputs/doubao-cloud-credentials.json"
)

job = client.submit(
    "让参考图中的人物在街道上行走",
    references=["/Users/xiaobian/Pictures/人物.png"],
    duration=10,
    ratio="16:9",
    idempotency_key="scene-001-v1",
)
job_id = job["id"]
print(job_id, job["status"])

# 后续在你的程序中按需查询；成功后下载。
job = client.status(job_id)
if job["status"] == "succeeded":
    client.download(job_id, "result.mp4")
```

`submit()` 返回的任务 ID 应保存在你的程序里。`references` 填本地图片路径；
客户端会逐张上传，然后将任务入队。无参考图时省略 `references`。
`idempotency_key` 建议使用你业务中稳定的任务编号，重试时传同一个值。

如果想等待生成结束，也可以调用：

```python
result = client.wait(job_id, timeout=1800, interval=5)
if result["status"] == "succeeded":
    client.download(job_id, "result.mp4")
else:
    print(result["status"], result["message"])
```

一键调用 `client.generate(prompt, "result.mp4", references=[...])` 会一直等待
任务完成并下载视频。没有在线 Worker 时请使用 `submit()` 和 `status()`，
不要在网页请求处理函数中等待生成。

## 在其他电脑上调用

通过环境变量提供地址和客户端令牌，再创建客户端：

```python
from doubao_client import DoubaoClient

client = DoubaoClient.from_env()
job = client.submit("一段城市夜景视频", duration=10)
print(job["id"])
```

环境变量名为 `DOUBAO_SERVER_URL` 和 `DOUBAO_CLIENT_TOKEN`。也可以直接
使用 `DoubaoClient(server_url, client_token)`。客户端令牌只应保存在你的
程序运行环境或私有配置中，不要提交到代码仓库。

其他可用方法：`client.check()` 检查连接并返回 Worker 列表，
`client.jobs()` 列出最近任务，`client.workers()` 查看 Worker 状态。

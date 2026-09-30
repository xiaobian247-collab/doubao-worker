# 云服务器接口说明

## 一、三层关系

```text
你的 Mac / 业务程序
        │ HTTPS + client_token
        ▼
云服务器 API（192.144.235.126）
        ▲ HTTPS + worker_token
        │
Windows Worker
        │ 本机调用 Python 插件
        ▼
豆包管理器（127.0.0.1:9223）
        │
已登录的豆包网页
```

云服务器 API 是给你的客户端调用的公网入口。Worker 的接口只给 Worker
使用。豆包插件没有单独的公网 HTTP 接口，也不应该把管理器的 9223 端口
暴露到公网。

插件内部接收的主要内容是：`prompt`、`duration`、`ratio`、`model` 和参考
图片路径。Worker 从服务器下载参考图片后，再把这些参数交给插件。

## 二、客户端提交任务

地址：`POST https://192.144.235.126/api/jobs`

请求头：

```http
Authorization: Bearer <client_token>
Content-Type: application/json
```

请求 JSON：

```json
{
  "prompt": "夜晚城市街道，电影感运镜",
  "duration": 10,
  "ratio": "16:9",
  "model": "Seedance 2.0 Fast",
  "idempotency_key": "demo-001",
  "reference_count": 0
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `prompt` | 是 | 视频描述，1 到 2000 个字符 |
| `duration` | 否 | 秒数，1 到 30，默认 10 |
| `ratio` | 否 | `16:9`、`9:16`、`1:1`、`4:3`、`3:4`，默认 `16:9` |
| `model` | 否 | 模型名称，默认 `Seedance 2.0 Fast` |
| `idempotency_key` | 否 | 重试时使用相同值，避免重复创建任务 |
| `reference_count` | 否 | 参考图数量，0 到 9，默认 0 |

成功响应示例：

```json
{
  "id": "任务UUID",
  "prompt": "夜晚城市街道，电影感运镜",
  "duration": 10,
  "ratio": "16:9",
  "model": "Seedance 2.0 Fast",
  "status": "queued",
  "progress": 0,
  "reference_count": 0
}
```

`id` 是任务 ID，后续查询和下载都使用它。常见状态为：
`queued`（排队）、`leased`（Worker 已领取）、`running`（豆包生成中）、
`uploading`（上传视频）、`succeeded`（成功）、`failed`（失败）、
`recovery_required`（生成过程中 Worker 失联，需要人工确认后再处理）。

## 三、查询和下载

查询任务：

```http
GET /api/jobs/<job_id>
Authorization: Bearer <client_token>
```

下载成片（只有 `succeeded` 才可下载）：

```http
GET /api/jobs/<job_id>/video
Authorization: Bearer <client_token>
```

参考图片不是直接塞进 JSON，而是先创建任务，再逐张上传：

```http
POST /api/jobs/<job_id>/references/0
Authorization: Bearer <client_token>
Content-Type: multipart/form-data
```

上传完成后由客户端调用 `/api/jobs/<job_id>/enqueue`，任务才进入队列。
`client.py submit --reference image.png` 已经自动完成这些步骤。

## 四、Worker 与服务器的内部接口

Worker 使用单独的 `worker_token`，不会使用客户端令牌：

| 方法 | 地址 | 用途 |
| --- | --- | --- |
| `POST` | `/api/workers/<worker_id>/lease` | 领取一个排队任务 |
| `POST` | `/api/workers/heartbeat` | 汇报 Worker 在线和租约续期 |
| `GET` | `/api/jobs/<job_id>/references/<index>` | 下载参考图片 |
| `POST` | `/api/jobs/<job_id>/report` | 汇报生成状态和进度 |
| `POST` | `/api/jobs/<job_id>/cos-upload` | 获取该任务专用的限时 COS 上传地址 |
| `POST` | `/api/jobs/<job_id>/cos-complete` | 核对 COS 对象后标记成功 |
| `POST` | `/api/jobs/<job_id>/artifact` | COS 不可用时上传 MP4 到服务器保底 |

这些接口由 `worker/agent.py` 自动调用，你不需要手动拼请求。
Worker 优先通过临时签名地址直传私有 COS，长期 COS 密钥只保存在服务器。
COS 未配置或暂时不可用时，Worker 将视频上传到原服务器本地目录保底。
客户端通过受保护的 `GET /api/jobs/<job_id>/video-location` 获取 COS 临时下载
地址，或统一通过 `/video` 接口下载。

## 五、最简单的使用方式

```sh
python3.11 outputs/doubao-cloud/local_client.py check
python3.11 outputs/doubao-cloud/local_client.py submit '夜晚城市街道，电影感运镜' --duration 10 --ratio 16:9
python3.11 outputs/doubao-cloud/local_client.py status <上一步返回的任务ID>
python3.11 outputs/doubao-cloud/local_client.py download <任务ID> --output result.mp4
```

第一条只检查连接，不创建任务。没有 Windows Worker 时，第二条创建的任务
会停留在 `queued`；Worker 上线后才会真正调用豆包插件生成视频。

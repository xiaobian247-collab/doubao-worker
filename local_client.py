"""Use the private local credentials file with the existing command-line client."""

import json
import os
import sys
from pathlib import Path

import requests


credentials_path = Path(__file__).resolve().parent.parent / "doubao-cloud-credentials.json"
try:
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    server_url = "https://" + credentials["server_ip"]
    token = credentials["client_token"]
except (OSError, ValueError, KeyError) as exc:
    raise SystemExit(f"无法读取本机凭据文件 {credentials_path}: {exc}") from exc

os.environ["DOUBAO_SERVER_URL"] = server_url
os.environ["DOUBAO_CLIENT_TOKEN"] = token


def check_connection():
    try:
        health = requests.get(server_url + "/healthz", timeout=15)
        health.raise_for_status()
        headers = {"Authorization": "Bearer " + token}
        jobs = requests.get(server_url + "/api/jobs", headers=headers, timeout=15)
        jobs.raise_for_status()
        workers = requests.get(server_url + "/api/workers", headers=headers, timeout=15)
        workers.raise_for_status()
    except requests.RequestException as exc:
        raise SystemExit(f"连接检查失败: {exc}") from exc

    print("服务器连接：正常")
    print("客户端令牌：有效")
    print(f"已有任务：{len(jobs.json())} 个")
    print(f"已登记 Worker：{len(workers.json())} 个")
    if not workers.json():
        print("当前没有 Worker；提交任务后会等待，不会生成视频。")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "check":
        check_connection()
    else:
        from client import main

        main()

import importlib.util
import hashlib
import socket
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import requests
import uvicorn
import pytest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_worker_http_cycle(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("DOUBAO_DATA_DIR", str(tmp_path / "server"))
    monkeypatch.setenv("DOUBAO_CLIENT_TOKEN", "client-test-token")
    monkeypatch.setenv("DOUBAO_WORKER_TOKENS", '{"worker-01":"worker-test-token"}')
    video = tmp_path / "mock.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"0" * 100)
    monkeypatch.setenv("DOUBAO_MOCK_VIDEO", str(video))
    monkeypatch.setenv("DOUBAO_WORKER_DATA", str(tmp_path / "worker"))
    monkeypatch.setenv("DOUBAO_WORKER_ID", "worker-01")
    monkeypatch.setenv("DOUBAO_WORKER_TOKEN", "worker-test-token")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = "http://127.0.0.1:%d" % port
    monkeypatch.setenv("DOUBAO_SERVER_URL", url)
    server_module = load("server_e2e_" + uuid.uuid4().hex, root / "server" / "app.py")
    server = uvicorn.Server(uvicorn.Config(server_module.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        client_headers = {"Authorization": "Bearer client-test-token"}
        job = requests.post(url + "/api/jobs", json={"prompt": "test video"}, headers=client_headers, timeout=5).json()
        worker = load("worker_e2e_" + uuid.uuid4().hex, root / "worker" / "agent.py")
        worker.check_environment()
        pending = requests.get(url + "/api/jobs/" + job["id"], headers=client_headers, timeout=5).json()
        assert pending["status"] == "queued"
        worker.main_loop(once=True)
        result = requests.get(url + "/api/jobs/" + job["id"], headers=client_headers, timeout=5).json()
        assert result["status"] == "succeeded", result
        response = requests.get(url + "/api/jobs/" + job["id"] + "/video", headers=client_headers, timeout=5)
        assert response.content == video.read_bytes()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_worker_uses_scoped_qiniu_token(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    worker = load("worker_qiniu_" + uuid.uuid4().hex, root / "worker" / "agent.py")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"0" * 100)
    key = "doubao/job-1.mp4"
    calls = []

    class Response:
        def __init__(self, status_code=200):
            self.status_code = status_code

        def raise_for_status(self):
            pass

        def json(self):
            return {"token": "temporary-token", "key": key}

    def post(url, **kwargs):
        if url.endswith("/cos-upload"):
            calls.append(("cos-unavailable", url))
            return Response(503)
        calls.append(("token", url, kwargs["json"]))
        return Response()

    def put_file(token, upload_key, path, **kwargs):
        assert (token, upload_key, path) == ("temporary-token", key, str(video))
        calls.append(("upload", upload_key))
        return {"key": key, "hash": "etag"}, types.SimpleNamespace(status_code=200)

    def api(method, path, **kwargs):
        calls.append(("complete", path, kwargs["payload"]))

    fake_qiniu = types.ModuleType("qiniu")
    fake_qiniu.put_file = put_file
    monkeypatch.setitem(sys.modules, "qiniu", fake_qiniu)
    monkeypatch.setattr(worker.requests, "post", post)
    monkeypatch.setattr(worker, "api", api)
    worker.upload_video("job-1", "lease-1", video)

    assert [call[0] for call in calls] == ["cos-unavailable", "token", "upload", "complete"]
    assert calls[-1][2]["sha256"] == hashlib.sha256(video.read_bytes()).hexdigest()
    assert calls[-1][2]["qiniu_hash"] == "etag"


def test_worker_uses_cos_presigned_url(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    worker = load("worker_cos_" + uuid.uuid4().hex, root / "worker" / "agent.py")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"0" * 100)
    calls = []

    class Response:
        status_code = 200
        headers = {"ETag": '"cos-etag"'}

        def raise_for_status(self):
            pass

        def json(self):
            return {"url": "https://cos.example/upload", "key": "videos/job-1.mp4"}

    def post(url, **kwargs):
        calls.append(("sign", url, kwargs["json"]))
        return Response()

    def put(url, **kwargs):
        assert url == "https://cos.example/upload"
        assert kwargs["data"].read() == video.read_bytes()
        calls.append(("upload", url))
        return Response()

    def api(method, path, **kwargs):
        calls.append(("complete", path, kwargs["payload"]))

    monkeypatch.setattr(worker.requests, "post", post)
    monkeypatch.setattr(worker.requests, "put", put)
    monkeypatch.setattr(worker, "api", api)
    worker.upload_video("job-1", "lease-1", video)

    assert [call[0] for call in calls] == ["sign", "upload", "complete"]
    assert calls[-1][2]["cos_etag"] == "cos-etag"
    assert calls[-1][2]["sha256"] == hashlib.sha256(video.read_bytes()).hexdigest()


def test_worker_rejects_corrupt_update(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    worker = load("worker_update_" + uuid.uuid4().hex, root / "worker" / "agent.py")
    target = tmp_path / "DoubaoWorker.exe"
    target.write_bytes(b"MZold")
    payload = b"MZ" + b"new" * 30

    class Response:
        def raise_for_status(self):
            pass

        def iter_content(self, _):
            yield payload

    monkeypatch.setattr(worker, "is_windows_executable", lambda: True)
    monkeypatch.setattr(worker, "APP_VERSION", "0.1.0")
    monkeypatch.setattr(worker, "DATA", tmp_path)
    monkeypatch.setattr(worker, "sys", types.SimpleNamespace(executable=str(target)))
    monkeypatch.setattr(worker.requests, "get", lambda *args, **kwargs: Response())
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Updater should not run"))

    manifest = {"version": "0.1.1", "download_path": "/download", "size": len(payload), "sha256": "0" * 64}
    assert worker.schedule_update(manifest) is False
    assert target.read_bytes() == b"MZold"
    assert not target.with_name(target.name + ".new").exists()

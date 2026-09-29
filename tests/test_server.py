import importlib.util
import hashlib
import sys
import time
import types
import uuid
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from doubao_client import DoubaoClient


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DOUBAO_CLIENT_TOKEN", "client-test-token")
    monkeypatch.setenv("DOUBAO_WORKER_TOKENS", '{"a":"worker-a-token","b":"worker-b-token"}')
    monkeypatch.setenv("DOUBAO_WORKER_ENROLL_TOKEN", "enroll-test-token")
    monkeypatch.setenv("DOUBAO_WORKER_RELEASE_TOKEN", "release-test-token")
    path = Path(__file__).resolve().parents[1] / "server" / "app.py"
    name = "server_test_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    with TestClient(module.app) as client:
        yield module, client
    del sys.modules[name]


def auth(token):
    return {"Authorization": "Bearer " + token}


def create(client, **fields):
    response = client.post("/api/jobs", json={"prompt": "test video", **fields}, headers=auth("client-test-token"))
    assert response.status_code == 200, response.text
    return response.json()


def lease(client, worker, token):
    response = client.post("/api/workers/%s/lease" % worker, headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def test_worker_release_requires_separate_publisher_and_worker_tokens(setup):
    _, client = setup
    exe = b"MZ" + b"x" * 200
    upload = client.post("/api/worker-releases", data={"version": "0.1.1"},
                         files={"file": ("DoubaoWorker.exe", exe)},
                         headers={"X-Worker-Release-Token": "release-test-token"})
    assert upload.status_code == 200, upload.text
    assert upload.json()["sha256"] == hashlib.sha256(exe).hexdigest()
    assert client.post("/api/worker-releases", data={"version": "0.1.2"},
                       files={"file": ("DoubaoWorker.exe", exe)}).status_code == 401
    assert client.get("/api/workers/a/update", headers=auth("client-test-token")).status_code == 401
    manifest = client.get("/api/workers/a/update", headers=auth("worker-a-token"))
    assert manifest.status_code == 200
    assert manifest.json()["version"] == "0.1.1"
    assert manifest.json()["sha256"] == hashlib.sha256(exe).hexdigest()
    assert client.get(manifest.json()["download_path"], headers=auth("worker-b-token")).status_code == 401
    download = client.get(manifest.json()["download_path"], headers=auth("worker-a-token"))
    assert download.status_code == 200
    assert download.content == exe


def test_two_workers_reference_and_video(setup):
    _, client = setup
    job = create(client, reference_count=1, idempotency_key="shot-1")
    assert job["status"] == "draft"
    assert create(client, reference_count=1, idempotency_key="shot-1")["id"] == job["id"]
    image = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    response = client.post("/api/jobs/%s/references/0" % job["id"],
                           files={"file": ("ref.png", image)}, headers=auth("client-test-token"))
    assert response.status_code == 200
    response = client.post("/api/jobs/%s/enqueue?reference_count=1" % job["id"], headers=auth("client-test-token"))
    assert response.json()["status"] == "queued"
    owned = lease(client, "a", "worker-a-token")
    assert owned["job"]["id"] == job["id"]
    assert owned["job"]["reference_indexes"] == [0]
    assert lease(client, "b", "worker-b-token")["job"] is None
    response = client.get("/api/jobs/%s/references/0?worker_id=a" % job["id"], headers=auth("worker-a-token"))
    assert response.content == image
    report = {"worker_id": "a", "lease_token": owned["lease_token"]}
    for state in ("running", "uploading"):
        response = client.post("/api/jobs/%s/report" % job["id"],
                               json={**report, "status": state}, headers=auth("worker-a-token"))
        assert response.status_code == 200, response.text
    video = b"\x00\x00\x00\x18ftypisom" + b"0" * 100
    response = client.post("/api/jobs/%s/artifact" % job["id"],
                           params={**report}, files={"file": ("test.mp4", video)}, headers=auth("worker-a-token"))
    assert response.status_code == 200, response.text
    response = client.get("/api/jobs/%s/video" % job["id"], headers=auth("client-test-token"))
    assert response.content == video
    assert client.get("/api/jobs/%s" % job["id"], headers=auth("client-test-token")).json()["status"] == "succeeded"


def test_expiration_only_requeues_before_generation(setup):
    module, client = setup
    job = create(client)
    first = lease(client, "a", "worker-a-token")
    with module.db() as conn:
        conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"]))
    second = lease(client, "b", "worker-b-token")
    assert second["job"]["id"] == job["id"]
    assert first["lease_token"] != second["lease_token"]
    response = client.post("/api/jobs/%s/report" % job["id"],
                           json={"worker_id": "b", "lease_token": second["lease_token"], "status": "running"},
                           headers=auth("worker-b-token"))
    assert response.status_code == 200
    with module.db() as conn:
        conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"]))
    stale = client.get("/api/jobs/%s" % job["id"], headers=auth("client-test-token")).json()
    assert stale["status"] == "recovery_required"
    assert lease(client, "a", "worker-a-token")["job"] is None
    final = client.get("/api/jobs/%s" % job["id"], headers=auth("client-test-token")).json()
    assert final["status"] == "recovery_required"
    assert "lease_token" not in final


def test_auth_and_transition_restrictions(setup):
    _, client = setup
    job = create(client)
    assert client.get("/api/jobs/%s" % job["id"]).status_code == 401
    assert client.post("/api/workers/a/lease", headers=auth("wrong")).status_code == 401
    owned = lease(client, "a", "worker-a-token")
    response = client.post("/api/jobs/%s/report" % job["id"],
                           json={"worker_id": "b", "lease_token": owned["lease_token"], "status": "running"},
                           headers=auth("worker-b-token"))
    assert response.status_code == 409


def test_worker_registration_creates_independent_token(setup):
    module, client = setup
    body = {"worker_id": "wkr-test-001", "machine_name": "cloud-a"}
    assert client.post("/api/workers/register", json=body).status_code == 401
    response = client.post("/api/workers/register", json=body,
                           headers={"X-Worker-Enrollment-Token": "enroll-test-token"})
    assert response.status_code == 200, response.text
    credentials = response.json()
    assert credentials["worker_id"] == body["worker_id"]
    assert len(credentials["worker_token"]) >= 32
    heartbeat = client.post("/api/workers/heartbeat", json={
        "worker_id": body["worker_id"], "manager_ready": True, "accounts": 2,
    }, headers=auth(credentials["worker_token"]))
    assert heartbeat.status_code == 200, heartbeat.text
    assert client.post("/api/workers/register", json=body,
                       headers={"X-Worker-Enrollment-Token": "enroll-test-token"}).status_code == 409

    package = module.MANAGER_ASSET_FILE
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_bytes(b"PK" + b"manager-package" * 10)
    assert client.get("/api/worker-assets/manager/manifest").status_code == 401
    headers = {"X-Worker-Enrollment-Token": "enroll-test-token"}
    manifest = client.get("/api/worker-assets/manager/manifest", headers=headers)
    assert manifest.status_code == 200
    assert manifest.json() == {
        "size": package.stat().st_size,
        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
    }
    download = client.get("/api/worker-assets/manager", headers=headers)
    assert download.status_code == 200
    assert download.content == package.read_bytes()


def test_importable_client_reference_to_download(setup, tmp_path):
    _, server = setup

    class LocalSession:
        headers = {}

        def request(self, method, url, **kwargs):
            headers = {**self.headers, **kwargs.pop("headers", {})}
            kwargs.pop("timeout", None)
            kwargs.pop("stream", None)
            response = server.request(method, url.removeprefix("https://test.local"),
                                      headers=headers, **kwargs)
            result = requests.Response()
            result.status_code = response.status_code
            result._content = response.content
            result._content_consumed = True
            result.url = url
            return result

    image = tmp_path / "reference.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    client = DoubaoClient("https://test.local", "client-test-token", session=LocalSession())
    job = client.submit("walking in a city", references=[image], idempotency_key="api-client-1")
    assert job["status"] == "queued"
    assert client.submit("walking in a city", references=[image],
                         idempotency_key="api-client-1")["id"] == job["id"]
    owned = lease(server, "a", "worker-a-token")
    assert owned["job"]["reference_indexes"] == [0]
    assert server.get(f"/api/jobs/{job['id']}/references/0?worker_id=a",
                      headers=auth("worker-a-token")).content == image.read_bytes()

    report = {"worker_id": "a", "lease_token": owned["lease_token"]}
    for state in ("running", "uploading"):
        response = server.post(f"/api/jobs/{job['id']}/report",
                               json={**report, "status": state}, headers=auth("worker-a-token"))
        assert response.status_code == 200
    video = b"\x00\x00\x00\x18ftypisom" + b"0" * 100
    response = server.post(f"/api/jobs/{job['id']}/artifact", params=report,
                           files={"file": ("video.mp4", video)}, headers=auth("worker-a-token"))
    assert response.status_code == 200
    assert client.wait(job["id"], timeout=1)["status"] == "succeeded"
    output = tmp_path / "output.mp4"
    assert client.download(job["id"], output) == output
    assert output.read_bytes() == video
    assert client.status(job["id"])["sha256"] == hashlib.sha256(video).hexdigest()


def test_qiniu_completion_requires_verified_object(setup, monkeypatch):
    module, client = setup
    monkeypatch.setattr(module, "QINIU_ACCESS_KEY", "access")
    monkeypatch.setattr(module, "QINIU_SECRET_KEY", "secret")
    monkeypatch.setattr(module, "QINIU_BUCKET", "bucket")
    monkeypatch.setattr(module, "QINIU_TEST_DOMAIN", "http://test.example")

    stored = {"fsize": 100, "hash": "etag"}
    fake = types.ModuleType("qiniu")

    class Auth:
        def __init__(self, access, secret):
            assert (access, secret) == ("access", "secret")

        def upload_token(self, bucket, key, expires):
            assert (bucket, expires) == ("bucket", 3600)
            return "temporary-token-for-" + key

    class BucketManager:
        def __init__(self, auth):
            assert isinstance(auth, Auth)

        def stat(self, bucket, key):
            assert (bucket, key) == ("bucket", "doubao/" + job["id"] + ".mp4")
            return stored, types.SimpleNamespace(status_code=200)

    fake.Auth = Auth
    fake.BucketManager = BucketManager
    monkeypatch.setitem(sys.modules, "qiniu", fake)

    job = create(client)
    owned = lease(client, "a", "worker-a-token")
    identity = {"worker_id": "a", "lease_token": owned["lease_token"]}
    for state in ("running", "uploading"):
        assert client.post(f"/api/jobs/{job['id']}/report", json={**identity, "status": state},
                           headers=auth("worker-a-token")).status_code == 200
    upload = client.post(f"/api/jobs/{job['id']}/qiniu-upload", json=identity,
                         headers=auth("worker-a-token"))
    assert upload.status_code == 200
    assert upload.json()["key"] == "doubao/" + job["id"] + ".mp4"
    complete = {**identity, "size": 100, "sha256": "a" * 64, "qiniu_hash": "wrong"}
    assert client.post(f"/api/jobs/{job['id']}/qiniu-complete", json=complete,
                       headers=auth("worker-a-token")).status_code == 409
    assert client.get(f"/api/jobs/{job['id']}", headers=auth("client-test-token")).json()["status"] == "uploading"
    complete["qiniu_hash"] = "etag"
    assert client.post(f"/api/jobs/{job['id']}/qiniu-complete", json=complete,
                       headers=auth("worker-a-token")).status_code == 200
    location = client.get(f"/api/jobs/{job['id']}/video-location", headers=auth("client-test-token"))
    assert location.json() == {"url": "http://test.example/doubao/" + job["id"] + ".mp4"}
    assert not (module.ARTIFACTS / (job["id"] + ".mp4")).exists()

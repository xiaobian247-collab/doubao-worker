"""Small single-server scheduler. Run one API process against its SQLite database."""

import hmac
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
import re
from contextlib import contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

ROOT = Path(os.environ.get("DOUBAO_DATA_DIR", "./data")).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
DB = ROOT / "jobs.sqlite3"
ARTIFACTS = ROOT / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)
REFERENCES = ROOT / "references"
REFERENCES.mkdir(exist_ok=True)
CLIENT_TOKEN = os.environ.get("DOUBAO_CLIENT_TOKEN", "")
WORKER_TOKENS = json.loads(os.environ.get("DOUBAO_WORKER_TOKENS", "{}"))
WORKER_ENROLL_TOKEN = os.environ.get("DOUBAO_WORKER_ENROLL_TOKEN", "")
TENCENT_COS_SECRET_ID = os.environ.get("TENCENT_COS_SECRET_ID", "")
TENCENT_COS_SECRET_KEY = os.environ.get("TENCENT_COS_SECRET_KEY", "")
TENCENT_COS_BUCKET = os.environ.get("TENCENT_COS_BUCKET", "")
TENCENT_COS_REGION = os.environ.get("TENCENT_COS_REGION", "")
WORKER_RELEASE_DIR = Path(os.environ.get("DOUBAO_WORKER_RELEASE_DIR", str(ROOT / "worker-release"))).resolve()
WORKER_RELEASE_DIR.mkdir(parents=True, exist_ok=True)
WORKER_RELEASE_FILE = Path(os.environ.get("DOUBAO_WORKER_RELEASE_FILE", str(WORKER_RELEASE_DIR / "DoubaoWorker.exe"))).resolve()
WORKER_RELEASE_FILE.parent.mkdir(parents=True, exist_ok=True)
WORKER_RELEASE_MANIFEST = WORKER_RELEASE_DIR / "manifest.json"
WORKER_RELEASE_ADMIN_TOKEN = os.environ.get("DOUBAO_WORKER_RELEASE_TOKEN", "")
MANAGER_ASSET_FILE = Path(os.environ.get(
    "DOUBAO_MANAGER_ASSET_FILE", str(ROOT / "worker-assets" / "DoubaoManager.zip")
)).resolve()
INSTALL_SCRIPT_FILE = Path(os.environ.get(
    "DOUBAO_INSTALL_SCRIPT_FILE", str(ROOT / "worker-assets" / "install-worker.ps1")
)).resolve()
LEASE_SECONDS = 90
MAX_UPLOAD = 2 * 1024 * 1024 * 1024
app = FastAPI(title="Doubao Task Server", docs_url=None, redoc_url=None)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@contextmanager
def db():
    conn = sqlite3.connect(DB, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


with db() as conn:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, prompt TEXT NOT NULL,
          duration INTEGER NOT NULL, ratio TEXT NOT NULL, model TEXT NOT NULL,
          reference_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
          message TEXT NOT NULL DEFAULT '', worker_id TEXT, lease_token TEXT,
          lease_until REAL, artifact_name TEXT, size INTEGER, sha256 TEXT,
          cos_key TEXT, cos_etag TEXT,
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workers (
          id TEXT PRIMARY KEY, last_seen REAL NOT NULL, manager_ready INTEGER NOT NULL DEFAULT 0,
          accounts INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS worker_credentials (
          worker_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
          machine_name TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
        );
    """)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "reference_count" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN reference_count INTEGER NOT NULL DEFAULT 0")
    if "cos_key" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN cos_key TEXT")
    if "cos_etag" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN cos_etag TEXT")


def token_value(authorization):
    scheme, _, value = (authorization or "").partition(" ")
    return value if scheme.lower() == "bearer" else ""


def client_auth(authorization: str = Header(default="")):
    if not CLIENT_TOKEN or not hmac.compare_digest(token_value(authorization), CLIENT_TOKEN):
        raise HTTPException(401, "Invalid client token")


def worker_auth(worker_id: str, authorization: str = Header(default="")):
    expected = WORKER_TOKENS.get(worker_id)
    supplied = token_value(authorization)
    if expected and hmac.compare_digest(supplied, expected):
        return
    if supplied:
        conn = sqlite3.connect(DB, timeout=30)
        try:
            row = conn.execute("SELECT token_hash FROM worker_credentials WHERE worker_id=?", (worker_id,)).fetchone()
        finally:
            conn.close()
        if row and hmac.compare_digest(hashlib.sha256(supplied.encode("utf-8")).hexdigest(), row[0]):
            return
        raise HTTPException(401, "Invalid worker token")
    raise HTTPException(401, "Invalid worker token")


def worker_id_valid(value):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,80}", value or ""))


def enrollment_auth(enrollment_token):
    if not WORKER_ENROLL_TOKEN or not hmac.compare_digest(enrollment_token, WORKER_ENROLL_TOKEN):
        raise HTTPException(401, "Invalid worker enrollment token")


def file_info(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as src:
        while chunk := src.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


class WorkerRegistration(BaseModel):
    worker_id: str = Field(min_length=2, max_length=81)
    machine_name: str = Field(default="", max_length=128)


@app.post("/api/workers/register")
def register_worker(body: WorkerRegistration,
                    enrollment_token: str = Header(default="", alias="X-Worker-Enrollment-Token")):
    enrollment_auth(enrollment_token)
    if not worker_id_valid(body.worker_id):
        raise HTTPException(422, "Invalid worker id")
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db() as conn:
        if body.worker_id in WORKER_TOKENS or conn.execute(
                "SELECT 1 FROM worker_credentials WHERE worker_id=?", (body.worker_id,)).fetchone():
            raise HTTPException(409, "Worker id already registered")
        conn.execute(
            "INSERT INTO worker_credentials (worker_id,token_hash,machine_name,created_at) VALUES (?,?,?,?)",
            (body.worker_id, hashlib.sha256(token.encode("utf-8")).hexdigest(), body.machine_name, now))
    return {"worker_id": body.worker_id, "worker_token": token}


@app.get("/api/worker-assets/manager/manifest")
def manager_asset_manifest(
        enrollment_token: str = Header(default="", alias="X-Worker-Enrollment-Token")):
    enrollment_auth(enrollment_token)
    if not MANAGER_ASSET_FILE.is_file():
        raise HTTPException(404, "Doubao manager package is not available")
    return file_info(MANAGER_ASSET_FILE)


@app.get("/api/worker-assets/manager")
def download_manager_asset(
        enrollment_token: str = Header(default="", alias="X-Worker-Enrollment-Token")):
    enrollment_auth(enrollment_token)
    if not MANAGER_ASSET_FILE.is_file():
        raise HTTPException(404, "Doubao manager package is not available")
    return FileResponse(MANAGER_ASSET_FILE, media_type="application/zip", filename="DoubaoManager.zip")


@app.get("/api/worker-assets/worker/manifest")
def worker_asset_manifest(
        enrollment_token: str = Header(default="", alias="X-Worker-Enrollment-Token")):
    enrollment_auth(enrollment_token)
    info = worker_release_info()
    if not info:
        raise HTTPException(404, "Worker package is not available")
    return {"version": info["version"], "size": info["size"], "sha256": info["sha256"]}


@app.get("/api/worker-assets/worker")
def download_worker_asset(
        enrollment_token: str = Header(default="", alias="X-Worker-Enrollment-Token")):
    enrollment_auth(enrollment_token)
    if not worker_release_info():
        raise HTTPException(404, "Worker package is not available")
    return FileResponse(WORKER_RELEASE_FILE, media_type="application/octet-stream", filename="DoubaoWorker.exe")


@app.get("/api/worker-assets/install-script")
def download_install_script():
    if not INSTALL_SCRIPT_FILE.is_file():
        raise HTTPException(404, "Install script is not available")
    return FileResponse(INSTALL_SCRIPT_FILE, media_type="text/plain", filename="install-worker.ps1")


def worker_release_info():
    data = {}
    if WORKER_RELEASE_MANIFEST.is_file():
        try:
            data = json.loads(WORKER_RELEASE_MANIFEST.read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
    version = str(data.get("version") or os.environ.get("DOUBAO_WORKER_VERSION", "")).strip()
    if not version or not WORKER_RELEASE_FILE.is_file():
        return None
    digest = hashlib.sha256()
    size = 0
    with WORKER_RELEASE_FILE.open("rb") as src:
        while chunk := src.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return {
        "version": version,
        "size": size,
        "sha256": digest.hexdigest(),
        "download_path": "/api/workers/{}/update/download".format("{worker_id}"),
    }


def worker_release_admin(token: str):
    if not WORKER_RELEASE_ADMIN_TOKEN or not hmac.compare_digest(token, WORKER_RELEASE_ADMIN_TOKEN):
        raise HTTPException(401, "Invalid worker release token")


class NewJob(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    duration: int = Field(default=10, ge=1, le=30)
    ratio: str = Field(default="16:9", pattern=r"^(16:9|9:16|1:1|4:3|3:4)$")
    model: str = Field(default="Seedance 2.0 Fast", max_length=80)
    idempotency_key: str | None = Field(default=None, max_length=128)
    reference_count: int = Field(default=0, ge=0, le=9)


class WorkerReport(BaseModel):
    worker_id: str
    lease_token: str | None = None
    status: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    message: str = Field(default="", max_length=300)
    manager_ready: bool = False
    accounts: int = Field(default=0, ge=0)
    size: int | None = None
    sha256: str | None = None


class CosArtifact(BaseModel):
    worker_id: str
    lease_token: str
    size: int = Field(ge=64, le=MAX_UPLOAD)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cos_etag: str = Field(min_length=1, max_length=128)


def cos_enabled():
    return all((TENCENT_COS_SECRET_ID, TENCENT_COS_SECRET_KEY,
                TENCENT_COS_BUCKET, TENCENT_COS_REGION))


def cos_client():
    from qcloud_cos import CosConfig, CosS3Client
    config = CosConfig(
        Region=TENCENT_COS_REGION,
        SecretId=TENCENT_COS_SECRET_ID,
        SecretKey=TENCENT_COS_SECRET_KEY,
        Scheme="https",
    )
    return CosS3Client(config)


def cos_url(key, method="GET"):
    return cos_client().get_presigned_url(
        Method=method,
        Bucket=TENCENT_COS_BUCKET,
        Key=key,
        Expired=3600,
    )


def normalize_etag(value):
    return str(value or "").strip().strip('"')


def job_dict(row):
    data = {key: row[key] for key in row.keys() if key != "lease_token"}
    data["reference_indexes"] = [int(p.stem) for p in sorted((REFERENCES / row["id"]).glob("*.image"))] if (REFERENCES / row["id"]).exists() else []
    return data


def active_job(conn, job_id, report):
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row["worker_id"] != report.worker_id or not row["lease_token"] or not hmac.compare_digest(row["lease_token"], report.lease_token or ""):
        raise HTTPException(409, "Job lease is not owned by this worker")
    if row["status"] not in ("leased", "running", "uploading"):
        raise HTTPException(409, "Job is no longer active")
    return row


def expire_jobs(conn, now):
    conn.execute("UPDATE jobs SET status='queued',worker_id=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE status='leased' AND lease_until<?", (now, now))
    conn.execute("UPDATE jobs SET status='recovery_required',message='Worker lost after generation started; inspect original account before retry',lease_token=NULL,lease_until=NULL,updated_at=? WHERE status IN ('running','uploading') AND lease_until<?", (now, now))


@app.post("/api/jobs", dependencies=[Depends(client_auth)])
def create_job(body: NewJob):
    now = time.time()
    with db() as conn:
        if body.idempotency_key:
            old = conn.execute("SELECT * FROM jobs WHERE idempotency_key=?", (body.idempotency_key,)).fetchone()
            if old:
                return job_dict(old)
        job_id = str(uuid.uuid4())
        conn.execute("INSERT INTO jobs (id,idempotency_key,prompt,duration,ratio,model,reference_count,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (job_id, body.idempotency_key, body.prompt, body.duration, body.ratio, body.model,
                      body.reference_count, "draft" if body.reference_count else "queued", now, now))
        return job_dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


@app.get("/api/jobs", dependencies=[Depends(client_auth)])
def list_jobs(limit: int = 50):
    with db() as conn:
        expire_jobs(conn, time.time())
        return [job_dict(row) for row in conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 200),))]


@app.get("/api/jobs/{job_id}", dependencies=[Depends(client_auth)])
def get_job(job_id: str):
    with db() as conn:
        expire_jobs(conn, time.time())
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Job not found")
        return job_dict(row)


@app.get("/api/workers", dependencies=[Depends(client_auth)])
def list_workers():
    with db() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM workers ORDER BY id")]


@app.post("/api/jobs/{job_id}/references/{index}", dependencies=[Depends(client_auth)])
async def upload_reference(job_id: str, index: int, file: UploadFile = File(...)):
    if index < 0 or index > 8:
        raise HTTPException(422, "Reference index must be 0 through 8")
    with db() as conn:
        row = conn.execute("SELECT status,reference_count FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Job not found")
        if row["status"] != "draft":
            raise HTTPException(409, "References can only be changed before assignment")
        if index >= row["reference_count"]:
            raise HTTPException(422, "Reference index exceeds declared count")
    folder = REFERENCES / job_id
    folder.mkdir(exist_ok=True)
    dest = folder / (str(index) + ".image")
    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > 20 * 1024 * 1024:
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "Reference image too large")
            out.write(chunk)
    return {"index": index, "size": size}


@app.post("/api/jobs/{job_id}/enqueue", dependencies=[Depends(client_auth)])
def enqueue(job_id: str, reference_count: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Job not found")
        if row["status"] == "queued":
            return job_dict(row)
        if row["status"] != "draft" or reference_count != row["reference_count"]:
            raise HTTPException(409, "Job cannot be enqueued")
        files = list((REFERENCES / job_id).glob("*.image"))
        if len(files) != reference_count or any(not (REFERENCES / job_id / (str(i) + ".image")).is_file() for i in range(reference_count)):
            raise HTTPException(409, "Reference uploads are incomplete")
        conn.execute("UPDATE jobs SET status='queued',updated_at=? WHERE id=?", (time.time(), job_id))
        return job_dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


@app.get("/api/jobs/{job_id}/references/{index}")
def download_reference(job_id: str, index: int, worker_id: str, authorization: str = Header(default="")):
    worker_auth(worker_id, authorization)
    if index < 0 or index > 8:
        raise HTTPException(404, "Reference not found")
    with db() as conn:
        row = conn.execute("SELECT worker_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["worker_id"] != worker_id:
            raise HTTPException(403, "Job is not assigned to this worker")
    path = REFERENCES / job_id / (str(index) + ".image")
    if not path.is_file():
        raise HTTPException(404, "Reference not found")
    return FileResponse(path)


@app.get("/api/workers/{worker_id}/update")
def worker_update(worker_id: str, authorization: str = Header(default="")):
    worker_auth(worker_id, authorization)
    info = worker_release_info()
    if not info:
        return {"available": False, "version": ""}
    info["available"] = True
    info["download_path"] = "/api/workers/{}/update/download".format(worker_id)
    return info


@app.get("/api/workers/{worker_id}/update/download")
def download_worker_update(worker_id: str, authorization: str = Header(default="")):
    worker_auth(worker_id, authorization)
    if not worker_release_info():
        raise HTTPException(404, "Worker update is not available")
    return FileResponse(WORKER_RELEASE_FILE, media_type="application/octet-stream", filename="DoubaoWorker.exe")


@app.post("/api/worker-releases")
async def upload_worker_release(
    version: str = Form(...),
    file: UploadFile = File(...),
    release_token: str = Header(default="", alias="X-Worker-Release-Token"),
):
    """Publish the EXE from CI; Workers only consume the authenticated GET endpoints above."""
    worker_release_admin(release_token)
    version = version.strip()
    if not version or len(version) > 64:
        raise HTTPException(422, "Invalid release version")
    WORKER_RELEASE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = WORKER_RELEASE_FILE.with_suffix(WORKER_RELEASE_FILE.suffix + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with temp.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "Worker update is too large")
                out.write(chunk)
                digest.update(chunk)
        with temp.open("rb") as src:
            if src.read(2) != b"MZ":
                raise HTTPException(422, "Worker update is not a Windows executable")
        temp.replace(WORKER_RELEASE_FILE)
        manifest_temp = WORKER_RELEASE_MANIFEST.with_suffix(".json.part")
        manifest_temp.write_text(json.dumps({
            "version": version, "size": size, "sha256": digest.hexdigest(),
        }, ensure_ascii=False), encoding="utf-8")
        manifest_temp.replace(WORKER_RELEASE_MANIFEST)
        return {"version": version, "size": size, "sha256": digest.hexdigest()}
    except Exception:
        temp.unlink(missing_ok=True)
        raise


@app.post("/api/workers/heartbeat")
def heartbeat(body: WorkerReport, authorization: str = Header(default="")):
    worker_auth(body.worker_id, authorization)
    now = time.time()
    with db() as conn:
        conn.execute("INSERT INTO workers(id,last_seen,manager_ready,accounts,message) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,manager_ready=excluded.manager_ready,accounts=excluded.accounts,message=excluded.message",
                     (body.worker_id, now, int(body.manager_ready), body.accounts, body.message))
        if body.lease_token:
            row = conn.execute("SELECT * FROM jobs WHERE worker_id=? AND lease_token=? AND status IN ('leased','running','uploading')", (body.worker_id, body.lease_token)).fetchone()
            if row:
                conn.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE id=?", (now + LEASE_SECONDS, now, row["id"]))
    return {"ok": True}


@app.post("/api/workers/{worker_id}/lease")
def lease(worker_id: str, authorization: str = Header(default="")):
    worker_auth(worker_id, authorization)
    now = time.time()
    with db() as conn:
        expire_jobs(conn, now)
        current = conn.execute("SELECT id FROM jobs WHERE worker_id=? AND status IN ('leased','running','uploading')", (worker_id,)).fetchone()
        if current:
            return {"job": None}
        row = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return {"job": None}
        lease_token = secrets.token_urlsafe(32)
        conn.execute("UPDATE jobs SET status='leased',worker_id=?,lease_token=?,lease_until=?,updated_at=? WHERE id=?",
                     (worker_id, lease_token, now + LEASE_SECONDS, now, row["id"]))
        return {"job": job_dict(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()), "lease_token": lease_token}


@app.post("/api/jobs/{job_id}/report")
def report(job_id: str, body: WorkerReport, authorization: str = Header(default="")):
    worker_auth(body.worker_id, authorization)
    if body.status not in ("running", "uploading", "failed", "recovery_required"):
        raise HTTPException(422, "Invalid status")
    with db() as conn:
        row = active_job(conn, job_id, body)
        if row["status"] not in ("leased", "running") and body.status == "running":
            raise HTTPException(409, "Invalid transition")
        if row["status"] == "leased" and body.status == "uploading":
            raise HTTPException(409, "Invalid transition")
        if row["status"] != "leased" and body.status == "failed":
            raise HTTPException(409, "Generation may have started; use recovery_required")
        terminal = body.status in ("failed", "recovery_required")
        conn.execute("UPDATE jobs SET status=?,progress=?,message=?,lease_token=?,lease_until=?,updated_at=? WHERE id=?",
                     (body.status, body.progress if body.progress is not None else row["progress"], body.message,
                      None if terminal else row["lease_token"], None if terminal else row["lease_until"], time.time(), job_id))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/artifact")
async def upload(job_id: str, worker_id: str, lease_token: str, file: UploadFile = File(...), authorization: str = Header(default="")):
    worker_auth(worker_id, authorization)
    report = WorkerReport(worker_id=worker_id, lease_token=lease_token)
    with db() as conn:
        row = active_job(conn, job_id, report)
        if row["status"] != "uploading":
            raise HTTPException(409, "Job is not uploading")
    import hashlib
    digest = hashlib.sha256()
    size = 0
    temp = ARTIFACTS / (job_id + ".part")
    final = ARTIFACTS / (job_id + ".mp4")
    try:
        with temp.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "Video too large")
                out.write(chunk)
                digest.update(chunk)
        with temp.open("rb") as src:
            if size < 64 or b"ftyp" not in src.read(64):
                raise HTTPException(422, "Invalid MP4")
        temp.replace(final)
        with db() as conn:
            active_job(conn, job_id, report)
            conn.execute("UPDATE jobs SET status='succeeded',progress=100,message='',artifact_name=?,size=?,sha256=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                         (final.name, size, digest.hexdigest(), time.time(), job_id))
        return {"size": size, "sha256": digest.hexdigest()}
    except Exception:
        temp.unlink(missing_ok=True)
        raise


@app.post("/api/jobs/{job_id}/cos-upload")
def cos_upload_url(job_id: str, body: WorkerReport, authorization: str = Header(default="")):
    worker_auth(body.worker_id, authorization)
    if not cos_enabled():
        raise HTTPException(503, "COS storage is not configured")
    with db() as conn:
        row = active_job(conn, job_id, body)
        if row["status"] != "uploading":
            raise HTTPException(409, "Job is not uploading")
    key = "videos/" + job_id + ".mp4"
    return {"url": cos_url(key, method="PUT"), "key": key}


@app.post("/api/jobs/{job_id}/cos-complete")
def cos_complete(job_id: str, body: CosArtifact, authorization: str = Header(default="")):
    worker_auth(body.worker_id, authorization)
    if not cos_enabled():
        raise HTTPException(503, "COS storage is not configured")
    report = WorkerReport(worker_id=body.worker_id, lease_token=body.lease_token)
    with db() as conn:
        row = active_job(conn, job_id, report)
        if row["status"] != "uploading":
            raise HTTPException(409, "Job is not uploading")
    key = "videos/" + job_id + ".mp4"
    try:
        result = cos_client().head_object(Bucket=TENCENT_COS_BUCKET, Key=key)
    except Exception as exc:
        raise HTTPException(502, "COS upload could not be verified") from exc
    actual_size = int(result.get("Content-Length", result.get("ContentLength", -1)))
    actual_etag = normalize_etag(result.get("ETag"))
    if actual_size != body.size or not actual_etag or actual_etag != normalize_etag(body.cos_etag):
        raise HTTPException(409, "COS object does not match upload")
    with db() as conn:
        active_job(conn, job_id, report)
        conn.execute("UPDATE jobs SET status='succeeded',progress=100,message='',cos_key=?,cos_etag=?,size=?,sha256=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                     (key, actual_etag, body.size, body.sha256, time.time(), job_id))
    return {"size": body.size, "sha256": body.sha256}


@app.get("/api/jobs/{job_id}/video-location", dependencies=[Depends(client_auth)])
def video_location(job_id: str):
    with db() as conn:
        row = conn.execute("SELECT status,cos_key FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row["status"] != "succeeded":
        raise HTTPException(404, "Video unavailable")
    return {"url": cos_url(row["cos_key"]) if row["cos_key"] else None}


@app.get("/api/jobs/{job_id}/video", dependencies=[Depends(client_auth)])
def download(job_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "succeeded":
            raise HTTPException(404, "Video unavailable")
        if row["cos_key"]:
            return RedirectResponse(cos_url(row["cos_key"]), status_code=307)
        path = ARTIFACTS / row["artifact_name"]
    return FileResponse(path, media_type="video/mp4", filename=job_id + ".mp4")

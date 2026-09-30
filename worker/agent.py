"""Windows worker for the supplied v0.7.17 manager plugin."""

import argparse
import hashlib
import json
import os
import sys
import subprocess
import threading
import time
import types
from pathlib import Path

import requests

FROZEN = getattr(sys, "frozen", False)
BASE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
CONFIG = json.loads(CONFIG_PATH.read_text("utf-8")) if CONFIG_PATH.is_file() else {}


def setting(env, key, default):
    return os.environ.get(env) or CONFIG.get(key) or default


def setting_path(env, key, default):
    path = Path(setting(env, key, default))
    return (path if path.is_absolute() else BASE / path).resolve()


def setting_bool(env, key, default=True):
    value = os.environ.get(env)
    if value is None:
        value = CONFIG.get(key, default)
    return str(value).lower() not in ("0", "false", "no", "off")


def read_version():
    path = Path(getattr(sys, "_MEIPASS", BASE)) / "VERSION" if FROZEN else BASE / "VERSION"
    return path.read_text("utf-8").strip() if path.is_file() else "dev"


DATA = setting_path("DOUBAO_WORKER_DATA", "data_dir", "data")
DATA.mkdir(parents=True, exist_ok=True)
_plugin_override = os.environ.get("DOUBAO_PLUGIN_DIR") or CONFIG.get("plugin_dir")
if _plugin_override:
    _plugin_path = Path(_plugin_override)
    PLUGIN = (_plugin_path if _plugin_path.is_absolute() else BASE / _plugin_path).resolve()
elif FROZEN and Path(getattr(sys, "_MEIPASS", ""), "plugin").is_dir():
    PLUGIN = Path(sys._MEIPASS) / "plugin"
else:
    PLUGIN = (BASE / "plugin").resolve()
SERVER = setting("DOUBAO_SERVER_URL", "server_url", "http://127.0.0.1:8000").rstrip("/")
WORKER_ID = setting("DOUBAO_WORKER_ID", "worker_id", "worker-01")
TOKEN = setting("DOUBAO_WORKER_TOKEN", "worker_token", "")
MANAGER_EXE = str(setting_path("DOUBAO_MANAGER_EXE", "manager_exe", "豆包管理器.exe"))
MANAGER_PORT = int(setting("DOUBAO_MANAGER_PORT", "manager_port", 9223))
MOCK_VIDEO = os.environ.get("DOUBAO_MOCK_VIDEO", "")
APP_VERSION = read_version()
AUTO_UPDATE = setting_bool("DOUBAO_WORKER_AUTO_UPDATE", "auto_update", True)
POLL_SECONDS = 5


def api(method, path, *, payload=None, files=None, params=None, timeout=30):
    response = requests.request(method, SERVER + path,
                                headers={"Authorization": "Bearer " + TOKEN},
                                json=payload, files=files, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json() if response.content else {}


def manager_status(start=False):
    if MOCK_VIDEO:
        return True, 1
    if start:
        if not PLUGIN.is_dir():
            raise RuntimeError("Plugin folder not found: " + str(PLUGIN))
        sys.path.insert(0, str(PLUGIN))
        import doubao_manager
        doubao_manager.ensure_manager_running(port=MANAGER_PORT, exe=MANAGER_EXE)
    response = requests.get("http://127.0.0.1:%d/json/list" % MANAGER_PORT, timeout=3)
    response.raise_for_status()
    targets = response.json()
    count = sum(1 for t in targets if "doubao.com" in (t.get("url") or ""))
    return True, count


def plugin_generate(job, refs, progress):
    if MOCK_VIDEO:
        path = Path(MOCK_VIDEO).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        progress("模拟生成完成", 90)
        return path
    if not PLUGIN.is_dir():
        raise RuntimeError("Plugin folder not found: " + str(PLUGIN))
    sys.path.insert(0, str(PLUGIN))
    # The plugin imports one helper from its original host. Manager mode only
    # needs an empty settings dict because task parameters are supplied here.
    shim = types.ModuleType("plugin_utils")
    shim.load_plugin_config = lambda _: {}
    sys.modules["plugin_utils"] = shim
    import main
    import manager_run
    main._PENDING_FILE = DATA / "pending_nowm.json"
    manager_run._STATE_FILE = DATA / "manager_state.json"
    output_dir = DATA / "jobs" / job["id"]
    output_dir.mkdir(parents=True, exist_ok=True)
    context = {
        "prompt": job["prompt"],
        "output_dir": str(output_dir),
        "filename": job["id"] + ".mp4",
        "reference_images": {"参考图片MAP": {i: str(p) for i, p in enumerate(refs)}},
        "plugin_params": {
            "backend": "manager", "manager_port": MANAGER_PORT,
            "manager_exe": MANAGER_EXE, "duration": str(job["duration"]),
            "ratio": job["ratio"], "model": job["model"],
            "allow_paid_generation": False,
        },
        "progress_callback": progress,
    }
    result = manager_run.generate(context, main_module=main)
    path = Path(result[0] if isinstance(result, (list, tuple)) else result).resolve()
    if not path.is_file() or path.stat().st_size < 64:
        raise RuntimeError("Plugin did not return a valid video file")
    return path


def image_extension(path):
    with path.open("rb") as src:
        head = src.read(16)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    raise RuntimeError("Unsupported reference image format")


def download_refs(job):
    folder = DATA / "jobs" / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in job.get("reference_indexes", []):
        response = requests.get(SERVER + "/api/jobs/%s/references/%d" % (job["id"], index),
                                headers={"Authorization": "Bearer " + TOKEN},
                                params={"worker_id": WORKER_ID}, stream=True, timeout=60)
        response.raise_for_status()
        temp = folder / ("ref_%d.tmp" % index)
        with temp.open("wb") as out:
            for chunk in response.iter_content(1024 * 1024):
                out.write(chunk)
        final = folder / ("ref_%d" % index + image_extension(temp))
        temp.replace(final)
        paths.append(final)
    return paths


def report(job_id, lease_token, status, message="", progress=None):
    return api("POST", "/api/jobs/%s/report" % job_id, payload={
        "worker_id": WORKER_ID, "lease_token": lease_token,
        "status": status, "message": str(message)[:300], "progress": progress,
    })


def upload_video(job_id, lease_token, path):
    identity = {"worker_id": WORKER_ID, "lease_token": lease_token}
    digest = hashlib.sha256()
    with path.open("rb") as video:
        while chunk := video.read(1024 * 1024):
            digest.update(chunk)
    sha256 = digest.hexdigest()

    response = requests.post(SERVER + "/api/jobs/%s/cos-upload" % job_id,
                             headers={"Authorization": "Bearer " + TOKEN},
                             json=identity, timeout=30)
    if response.status_code != 503:
        response.raise_for_status()
        upload = response.json()
        with path.open("rb") as video:
            uploaded = requests.put(upload["url"], data=video,
                                    headers={"Content-Type": "video/mp4"}, timeout=(30, 1800))
        uploaded.raise_for_status()
        etag = uploaded.headers.get("ETag", "").strip().strip('"')
        if not etag:
            raise RuntimeError("COS upload did not return an ETag")
        api("POST", "/api/jobs/%s/cos-complete" % job_id, payload={
            **identity, "size": path.stat().st_size, "sha256": sha256,
            "cos_etag": etag,
        }, timeout=60)
        return
    with path.open("rb") as video:
        api("POST", "/api/jobs/%s/artifact" % job_id,
            params=identity, files={"file": (job_id + ".mp4", video, "video/mp4")},
            timeout=1800)


def version_key(value):
    parts = []
    for part in str(value).lstrip("v").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple((parts + [0, 0, 0])[:3])


def is_windows_executable():
    return FROZEN and os.name == "nt"


def schedule_update(manifest):
    """Download and atomically replace the running Windows executable."""
    if not is_windows_executable():
        return False
    remote_version = str(manifest.get("version") or "")
    if not remote_version or version_key(remote_version) <= version_key(APP_VERSION):
        return False
    download_path = manifest.get("download_path") or manifest.get("download_url")
    if not download_path:
        print("worker update skipped: server did not provide a download path", flush=True)
        return False
    url = download_path if str(download_path).startswith(("http://", "https://")) else SERVER + str(download_path)
    target = Path(sys.executable).resolve()
    new_path = target.with_name(target.name + ".new")
    try:
        response = requests.get(url, headers={"Authorization": "Bearer " + TOKEN}, stream=True, timeout=60)
        response.raise_for_status()
        digest = hashlib.sha256()
        size = 0
        with new_path.open("wb") as out:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > 2 * 1024 * 1024 * 1024:
                    raise RuntimeError("update file is too large")
                out.write(chunk)
                digest.update(chunk)
        expected_size = manifest.get("size")
        expected_hash = str(manifest.get("sha256") or "").lower()
        if expected_size is not None and int(expected_size) != size:
            raise RuntimeError("update size verification failed")
        if expected_hash and digest.hexdigest() != expected_hash:
            raise RuntimeError("update SHA-256 verification failed")
        with new_path.open("rb") as check:
            if check.read(2) != b"MZ":
                raise RuntimeError("update is not a Windows executable")
        update_script = DATA / "apply-worker-update.ps1"
        script = """param([int]$ParentPid, [string]$New, [string]$Target, [string]$WorkingDir, [string]$Script)
Wait-Process -Id $ParentPid -ErrorAction SilentlyContinue
$updated = $false
for ($i = 0; $i -lt 30; $i++) {
  try { Move-Item -LiteralPath $New -Destination $Target -Force; $updated = $true; break }
  catch { Start-Sleep -Milliseconds 500 }
}
if ($updated) { Start-Process -FilePath $Target -WorkingDirectory $WorkingDir }
Remove-Item -LiteralPath $Script -Force -ErrorAction SilentlyContinue
"""
        update_script.write_text(script, encoding="utf-8")
        subprocess.Popen([
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(update_script),
            "-ParentPid", str(os.getpid()), "-New", str(new_path), "-Target", str(target),
            "-WorkingDir", str(target.parent), "-Script", str(update_script),
        ], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print("worker update scheduled:", APP_VERSION, "->", remote_version, flush=True)
        return True
    except Exception as exc:
        new_path.unlink(missing_ok=True)
        print("worker update skipped:", exc, flush=True)
        return False


def check_for_update():
    if not FROZEN or not AUTO_UPDATE:
        return False
    try:
        manifest = api("GET", "/api/workers/%s/update" % WORKER_ID, timeout=15)
        return schedule_update(manifest)
    except Exception as exc:
        print("worker update check failed:", exc, flush=True)
        return False


def heartbeat_loop(stop, lease_token_getter):
    while not stop.is_set():
        try:
            ready, accounts = manager_status()
        except Exception:
            ready, accounts = False, 0
        try:
            api("POST", "/api/workers/heartbeat", payload={
                "worker_id": WORKER_ID, "lease_token": lease_token_getter(),
                "manager_ready": ready, "accounts": accounts,
            })
        except Exception as exc:
            print("heartbeat failed:", exc, flush=True)
        stop.wait(15)


def run_job(job, lease_token):
    job_id = job["id"]
    started = False
    try:
        refs = download_refs(job)
        report(job_id, lease_token, "running", "生成入口已启动，禁止自动重派", 0)
        started = True
        last_report = [0.0]

        def progress(message, percent=None):
            now = time.time()
            if now - last_report[0] >= 8:
                last_report[0] = now
                try:
                    report(job_id, lease_token, "running", message, max(0, min(95, int(percent or 0))))
                except Exception as exc:
                    print("progress report failed:", exc, flush=True)

        path = plugin_generate(job, refs, progress)
        report(job_id, lease_token, "uploading", "视频已生成，正在上传", 96)
        upload_video(job_id, lease_token, path)
        print("completed", job_id, flush=True)
    except Exception as exc:
        state = "recovery_required" if started else "failed"
        print("job", job_id, state, str(exc)[:300], flush=True)
        try:
            report(job_id, lease_token, state, str(exc)[:300])
        except Exception as report_exc:
            print("failure report failed:", report_exc, flush=True)


def main_loop(once=False):
    if not TOKEN:
        raise SystemExit("Set DOUBAO_WORKER_TOKEN")
    lockfile = (DATA / "worker.lock").open("a+b")
    if lockfile.tell() == 0:
        lockfile.write(b"0")
        lockfile.flush()
    try:
        if os.name == "nt":
            import msvcrt
            lockfile.seek(0)
            msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lockfile.close()
        raise SystemExit("Worker is already running for this data directory") from exc
    current = [None]
    stop = threading.Event()
    heartbeat = threading.Thread(target=heartbeat_loop, args=(stop, lambda: current[0]), daemon=True)
    heartbeat.start()
    last_update_check = time.monotonic()
    try:
        while True:
            try:
                manager_status(start=True)
                result = api("POST", "/api/workers/%s/lease" % WORKER_ID)
                if result.get("job"):
                    current[0] = result["lease_token"]
                    try:
                        run_job(result["job"], current[0])
                    finally:
                        current[0] = None
                elif once:
                    return
                elif time.monotonic() - last_update_check >= 300:
                    last_update_check = time.monotonic()
                    if check_for_update():
                        return
            except Exception as exc:
                print("worker loop:", exc, flush=True)
                if once:
                    raise
            if once:
                return
            time.sleep(POLL_SECONDS)
    finally:
        stop.set()
        heartbeat.join(timeout=5)
        lockfile.close()


def check_environment():
    if not TOKEN:
        raise SystemExit("Set DOUBAO_WORKER_TOKEN")
    print("server_url:", SERVER, flush=True)
    response = requests.get(SERVER + "/healthz", timeout=10)
    response.raise_for_status()
    print("server_health:", response.json(), flush=True)
    manager_error = None
    try:
        ready, accounts = manager_status(start=False)
    except Exception as exc:
        ready, accounts = False, 0
        manager_error = exc
    api("POST", "/api/workers/heartbeat", payload={
        "worker_id": WORKER_ID,
        "manager_ready": ready,
        "accounts": accounts,
        "message": "worker connectivity check",
    })
    print("worker_auth: ok", flush=True)
    if ready:
        print("manager_ready:", ready, "account_pages:", accounts, flush=True)
    else:
        print("manager_ready: false", str(manager_error), flush=True)
        print("manager_exe:", MANAGER_EXE, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Process at most one queued task")
    parser.add_argument("--check", action="store_true", help="Check server and manager without leasing a job")
    parser.add_argument("--no-update", action="store_true", help="Disable the startup update check")
    args = parser.parse_args()
    if args.check:
        check_environment()
    else:
        if not args.no_update and check_for_update():
            raise SystemExit(0)
        main_loop(args.once)

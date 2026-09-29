"""Mirror the latest public GitHub Worker release into the API data directory."""

import hashlib
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path

REPO = os.environ.get("DOUBAO_WORKER_GITHUB_REPO", "xiaobian247-collab/doubao-worker")
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", REPO):
    raise SystemExit("Invalid GitHub repository name")
ROOT = Path(os.environ.get("DOUBAO_DATA_DIR", "./data")).resolve() / "worker-release"
ROOT.mkdir(parents=True, exist_ok=True)
EXE = ROOT / "DoubaoWorker.exe"
MANIFEST = ROOT / "manifest.json"
MAX_SIZE = 256 * 1024 * 1024


def request(url):
    return urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "doubao-worker-release-sync",
        "X-GitHub-Api-Version": "2022-11-28",
    })


def version_number(value):
    match = re.fullmatch(r"(?:worker-v)?(\d+)\.(\d+)\.(\d+)", value)
    return tuple(map(int, match.groups())) if match else None


def sync():
    api = "https://api.github.com/repos/" + REPO + "/releases/latest"
    with urllib.request.urlopen(request(api), timeout=20) as response:
        release = json.load(response)
    tag = str(release.get("tag_name", ""))
    version = version_number(tag)
    if not tag.startswith("worker-v") or version is None:
        raise RuntimeError("Latest GitHub release is not a Worker version")
    assets = [asset for asset in release.get("assets", []) if asset.get("name") == "DoubaoWorker.exe"]
    if len(assets) != 1:
        raise RuntimeError("GitHub release has no unique DoubaoWorker.exe asset")
    asset = assets[0]
    url = str(asset.get("browser_download_url", ""))
    prefix = "https://github.com/" + REPO + "/releases/download/"
    if not url.startswith(prefix):
        raise RuntimeError("Unexpected GitHub release download URL")
    if MANIFEST.is_file() and EXE.is_file():
        current = json.loads(MANIFEST.read_text("utf-8"))
        current_version = version_number(str(current.get("version", "")))
        if current_version is not None and current_version >= version:
            print("Worker release already current:", current.get("version"))
            return

    fd, temp_name = tempfile.mkstemp(prefix="DoubaoWorker-", suffix=".part", dir=ROOT)
    temp = Path(temp_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(request(url), timeout=90) as response:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                size += len(chunk)
                if size > MAX_SIZE:
                    raise RuntimeError("Worker release is too large")
                out.write(chunk)
                digest.update(chunk)
        if size != int(asset.get("size", -1)):
            raise RuntimeError("Worker release size mismatch")
        expected_digest = str(asset.get("digest") or "")
        if expected_digest and expected_digest != "sha256:" + digest.hexdigest():
            raise RuntimeError("Worker release SHA-256 mismatch")
        with temp.open("rb") as src:
            if src.read(2) != b"MZ":
                raise RuntimeError("Worker release is not a Windows executable")
        temp.replace(EXE)
        manifest_temp = MANIFEST.with_suffix(".json.part")
        manifest_temp.write_text(json.dumps({
            "version": tag.removeprefix("worker-v"),
            "size": size,
            "sha256": digest.hexdigest(),
        }), encoding="utf-8")
        manifest_temp.replace(MANIFEST)
        print("Worker release mirrored:", tag, size, digest.hexdigest())
    finally:
        temp.unlink(missing_ok=True)


if __name__ == "__main__":
    sync()

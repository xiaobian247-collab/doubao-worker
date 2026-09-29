import hashlib
import importlib.util
import io
import json
import sys
import uuid
from pathlib import Path

import pytest


def test_release_sync_verifies_digest_and_keeps_last_good_version(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("DOUBAO_DATA_DIR", str(tmp_path))
    path = root / "server" / "sync_worker_release.py"
    spec = importlib.util.spec_from_file_location("release_sync_" + uuid.uuid4().hex, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    exe = b"MZ" + b"windows" * 16
    release = {
        "tag_name": "worker-v0.1.0",
        "assets": [{
            "name": "DoubaoWorker.exe",
            "browser_download_url": "https://github.com/xiaobian247-collab/doubao-worker/releases/download/worker-v0.1.0/DoubaoWorker.exe",
            "size": len(exe),
            "digest": "sha256:" + hashlib.sha256(exe).hexdigest(),
        }],
    }

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda request, **kwargs: Response(
        json.dumps(release).encode() if "api.github.com" in request.full_url else exe
    ))
    module.sync()
    assert module.EXE.read_bytes() == exe
    assert json.loads(module.MANIFEST.read_text())["version"] == "0.1.0"

    release["tag_name"] = "worker-v0.1.1"
    release["assets"][0]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(RuntimeError, match="SHA-256"):
        module.sync()
    assert module.EXE.read_bytes() == exe
    assert json.loads(module.MANIFEST.read_text())["version"] == "0.1.0"

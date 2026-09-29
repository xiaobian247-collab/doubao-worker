"""Small importable client for the Doubao cloud task API."""

import hashlib
import json
import os
import time
from pathlib import Path

import requests


class DoubaoClient:
    def __init__(self, server_url, client_token, *, timeout=120, session=None):
        if not server_url or not client_token:
            raise ValueError("server_url and client_token are required")
        self.server_url = server_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + client_token})
        self.timeout = timeout

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("DOUBAO_SERVER_URL"),
                   os.environ.get("DOUBAO_CLIENT_TOKEN"))

    @classmethod
    def from_credentials_file(cls, path):
        credentials = json.loads(Path(path).read_text(encoding="utf-8"))
        server_url = credentials.get("server_url") or "https://" + credentials["server_ip"]
        return cls(server_url, credentials["client_token"])

    def _request(self, method, path, **kwargs):
        response = self.session.request(method, self.server_url + path,
                                        timeout=self.timeout, **kwargs)
        response.raise_for_status()
        return response

    def check(self):
        self._request("GET", "/healthz")
        return self._request("GET", "/api/workers").json()

    def submit(self, prompt, *, references=(), duration=10, ratio="16:9",
               model="Seedance 2.0 Fast", idempotency_key=None):
        paths = [Path(path) for path in references]
        if len(paths) > 9:
            raise ValueError("At most 9 reference images are supported")
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(path)
            if path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError(f"Reference image exceeds 20 MB: {path}")

        job = self._request("POST", "/api/jobs", json={
            "prompt": prompt, "duration": duration, "ratio": ratio,
            "model": model, "idempotency_key": idempotency_key,
            "reference_count": len(paths),
        }).json()
        if job["reference_count"] != len(paths):
            raise ValueError("Existing idempotency_key belongs to a job with a different reference count")
        if not paths or job["status"] != "draft":
            return job

        job_id = job["id"]
        for index, path in enumerate(paths):
            with path.open("rb") as image:
                self._request("POST", f"/api/jobs/{job_id}/references/{index}",
                              files={"file": (path.name, image)})
        return self._request("POST", f"/api/jobs/{job_id}/enqueue",
                             params={"reference_count": len(paths)}).json()

    def status(self, job_id):
        return self._request("GET", f"/api/jobs/{job_id}").json()

    def jobs(self):
        return self._request("GET", "/api/jobs").json()

    def workers(self):
        return self._request("GET", "/api/workers").json()

    def wait(self, job_id, *, timeout=1800, interval=5):
        if timeout <= 0 or interval <= 0:
            raise ValueError("timeout and interval must be positive")
        deadline = time.monotonic() + timeout
        while True:
            job = self.status(job_id)
            if job["status"] in ("succeeded", "failed", "recovery_required"):
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for job {job_id}; last status: {job['status']}")
            time.sleep(min(interval, remaining))

    def download(self, job_id, output):
        output = Path(output)
        job = self.status(job_id)
        if job["status"] != "succeeded":
            raise RuntimeError(f"Job {job_id} is {job['status']}; video is not ready")
        response = self._request("GET", f"/api/jobs/{job_id}/video", stream=True)
        temp = output.with_name(output.name + ".part")
        digest = hashlib.sha256()
        try:
            with temp.open("wb") as target:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        target.write(chunk)
                        digest.update(chunk)
            if job.get("sha256") and digest.hexdigest() != job["sha256"]:
                raise RuntimeError("Downloaded video checksum mismatch")
            temp.replace(output)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        finally:
            response.close()
        return output

    def generate(self, prompt, output, *, wait_timeout=1800, **submit_options):
        job = self.submit(prompt, **submit_options)
        job = self.wait(job["id"], timeout=wait_timeout)
        if job["status"] != "succeeded":
            raise RuntimeError(f"Job {job['id']} ended as {job['status']}: {job['message']}")
        return self.download(job["id"], output)

"""Submit jobs and retrieve generated videos from a Mac or any other computer."""

import argparse
import hashlib
import json
import os
from pathlib import Path

import requests

SERVER = os.environ.get("DOUBAO_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("DOUBAO_CLIENT_TOKEN", "")


def call(method, path, **kwargs):
    response = requests.request(method, SERVER + path,
                                headers={"Authorization": "Bearer " + TOKEN},
                                timeout=120, **kwargs)
    response.raise_for_status()
    return response


def main():
    parser = argparse.ArgumentParser(description="Doubao cloud task client")
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("prompt")
    submit.add_argument("--duration", type=int, default=10)
    submit.add_argument("--ratio", default="16:9")
    submit.add_argument("--model", default="Seedance 2.0 Fast")
    submit.add_argument("--reference", action="append", default=[])
    submit.add_argument("--key", help="Idempotency key for retries")
    status = commands.add_parser("status")
    status.add_argument("job_id")
    commands.add_parser("jobs")
    commands.add_parser("workers")
    download = commands.add_parser("download")
    download.add_argument("job_id")
    download.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not TOKEN:
        parser.error("Set DOUBAO_CLIENT_TOKEN")

    if args.command == "submit":
        body = {"prompt": args.prompt, "duration": args.duration, "ratio": args.ratio,
                "model": args.model, "idempotency_key": args.key,
                "reference_count": len(args.reference)}
        job = call("POST", "/api/jobs", json=body).json()
        if args.reference and job["status"] == "draft":
            for index, name in enumerate(args.reference):
                path = Path(name)
                with path.open("rb") as src:
                    call("POST", "/api/jobs/%s/references/%d" % (job["id"], index),
                         files={"file": (path.name, src)})
            job = call("POST", "/api/jobs/%s/enqueue" % job["id"],
                       params={"reference_count": len(args.reference)}).json()
        print(json.dumps(job, ensure_ascii=False, indent=2))
    elif args.command == "status":
        print(json.dumps(call("GET", "/api/jobs/" + args.job_id).json(), ensure_ascii=False, indent=2))
    elif args.command == "jobs":
        print(json.dumps(call("GET", "/api/jobs").json(), ensure_ascii=False, indent=2))
    elif args.command == "workers":
        print(json.dumps(call("GET", "/api/workers").json(), ensure_ascii=False, indent=2))
    else:
        path = args.output or Path(args.job_id + ".mp4")
        expected = call("GET", "/api/jobs/" + args.job_id).json().get("sha256")
        temp = path.with_name(path.name + ".part")
        digest = hashlib.sha256()
        with call("GET", "/api/jobs/" + args.job_id + "/video", stream=True) as response:
            with temp.open("wb") as out:
                for chunk in response.iter_content(1024 * 1024):
                    out.write(chunk)
                    digest.update(chunk)
        if expected and digest.hexdigest() != expected:
            temp.unlink(missing_ok=True)
            raise SystemExit("Downloaded video checksum mismatch")
        temp.replace(path)
        print(path.resolve())


if __name__ == "__main__":
    main()

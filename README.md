# Doubao Cloud MVP

The server is currently deployed at `https://www.zcbox.top`. See
`DEPLOYMENT-STATUS.zh-CN.md` for the live deployment status.

Windows EXE builds and automatic updates are documented in
`WORKER-RELEASE.zh-CN.md`. GitHub Actions builds the EXE, so Windows cloud
desktops do not need Python.

This package has three parts: `server/app.py` (scheduler and file storage),
`worker/agent.py` (one Windows worker per cloud desktop), and `client.py` (Mac
command-line client). The Worker includes a copy of the supplied v0.7.17 plugin
core. It controls an already running manager through its local CDP port 9223,
or starts `豆包管理器.exe` if that port is unavailable. Keep the manager's entire
original directory intact; do not copy only the EXE.

## Requirements

- Python 3.11 on the server and Mac client; the Windows EXE needs no Python.
- An HTTPS reverse proxy for the server in production. Only port 443 needs to
  be public. Never expose the manager's port 9223.
- A logged-in Windows desktop session with the manager's Doubao accounts.
- Separate long random tokens for the client and each Worker.

## Server

```sh
cd server
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
export DOUBAO_DATA_DIR=/var/lib/doubao-cloud
export DOUBAO_CLIENT_TOKEN='replace-with-a-long-random-secret'
export DOUBAO_WORKER_TOKENS='{"worker-01":"another-long-random-secret"}'
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

Use one Uvicorn process with SQLite. Keep the data directory private and back it
up. Workers upload videos directly to COS and use server-local storage as a fallback.

## Windows Worker

Download `DoubaoWorker-windows-x64` from the latest successful GitHub Actions
run, or download `DoubaoWorker.exe` from Releases. Copy `config.example.json`
to `config.json` beside the EXE and fill in `https://www.zcbox.top`, the Worker
ID and token, and the path to `豆包管理器.exe`. Keep `config.json` private. The
manager's entire portable directory must stay intact. In PowerShell:

```powershell
cd C:\DoubaoWorker
.\DoubaoWorker.exe --check
.\DoubaoWorker.exe
```

The GitHub-built EXE embeds the plugin and checks the API server for updates
at startup and every five idle minutes. A running generation is never
interrupted. Environment variables override the config file. The optional
`worker\build-worker.ps1` script builds the same EXE on a Windows machine.

Run the Worker once per Windows session. Use Task Scheduler's **At log on**
trigger for unattended startup. The cloud desktop must keep an interactive
logged-in session; Session 0 services cannot reliably host the Electron UI.
The Worker has no inbound network listener. `DOUBAO_WORKER_DATA` changes its
state/output directory; install the Worker in a writable location.

## Mac client

```sh
python3.11 -m pip install requests
export DOUBAO_SERVER_URL='https://www.zcbox.top'
export DOUBAO_CLIENT_TOKEN='replace-with-a-long-random-secret'
python3.11 client.py submit '夜晚城市街道，电影感运镜' --duration 10 --ratio 16:9
python3.11 client.py status JOB_ID
python3.11 client.py download JOB_ID --output result.mp4
```

Up to nine reference images may be added with repeated `--reference` options.
The job remains in `draft` until every image upload succeeds, then is queued.
Use `--key project-shot-version` to make a repeated submit return the same job.

## Reliability rules

Each Worker executes only one job at a time. `leased` jobs can be requeued
after a lost lease. Immediately before invoking the plugin, the Worker reports
`running`. Because the plugin has no trustworthy external "submitted" event,
any lost or failed `running` job becomes `recovery_required`. Inspect the
original account and its pending video before creating a new job. Do not
automatically retry it: it may already have consumed quota.

The worker keeps downloaded videos under `worker/data/jobs/JOB_ID`. A server
upload failure may leave a complete local file there. Server job status and
error messages are available through `client.py status` and `client.py jobs`.

No Windows/cloud-manager generation has been verified on this Mac. Test one
real task on the target Windows desktop before relying on unattended jobs.

Run the automated scheduler and mock Worker checks with:

```sh
python3.11 -m pip install -r requirements-dev.txt
python3.11 -m pytest -q tests
```

# Doubao Cloud MVP

The server is currently deployed at `https://192.144.235.126`. See
`DEPLOYMENT-STATUS.zh-CN.md` for the live deployment status.

This package has three parts: `server/app.py` (scheduler and file storage),
`worker/agent.py` (one Windows worker per cloud desktop), and `client.py` (Mac
command-line client). The Worker includes a copy of the supplied v0.7.17 plugin
core. It controls an already running manager through its local CDP port 9223,
or starts `豆包管理器.exe` if that port is unavailable. Keep the manager's entire
original directory intact; do not copy only the EXE.

## Requirements

- Python 3.11 on the server, Windows cloud desktops, and Mac client.
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

Use one Uvicorn process with SQLite. For multiple API processes or multiple
server hosts, migrate the transactional scheduler to PostgreSQL. Keep the data
directory private; it contains prompts and videos. Back it up. This MVP stores
videos on the server, with a 2 GB per-video limit; object storage can replace
the artifact endpoints when transfer volume grows.

## Windows Worker

Copy the entire `worker` folder to the Windows cloud desktop. Place it beside
the manager's complete portable directory, or set `DOUBAO_MANAGER_EXE` to the
actual EXE path. Copy `config.example.json` to `config.json` and edit the URL,
Worker ID, token, and manager path. Keep `config.json` private. In PowerShell:

```powershell
cd C:\DoubaoCloud\worker
py -3.11 -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\python.exe agent.py
```

To create `DoubaoWorker.exe` on Windows, run `worker\build-worker.ps1` in
PowerShell. Deploy `worker\dist\DoubaoWorker.exe` together with its sibling
`worker\dist\plugin` folder. Set the same environment variables before
starting the EXE, or copy `config.example.json` to `config.json` beside it.
Environment variables override the config file. Packaging cannot be verified
on this Mac.

Run the Worker once per Windows session. Use Task Scheduler's **At log on**
trigger for unattended startup. The cloud desktop must keep an interactive
logged-in session; Session 0 services cannot reliably host the Electron UI.
The Worker has no inbound network listener. `DOUBAO_WORKER_DATA` changes its
state/output directory. The plugin copy writes some debug output to
`worker/plugin`, so install this folder in a writable location.

## Mac client

```sh
python3.11 -m pip install requests
export DOUBAO_SERVER_URL='https://192.144.235.126'
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

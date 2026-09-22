python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 7531


# NumbatAPI (unified under Kelpie)

A FastAPI backend that a web UI can call. It validates an email against a
configured domain, creates a per-user profile folder under the shared Kelpie
data root, and exposes task management + task-log reading endpoints.

This is the standalone NumbatAPI project brought under `Kelpie/NumbatAPI` so it
shares Kelpie's central configuration and logger instead of maintaining its own.

## What changed vs. the standalone project

- **Configuration is shared.** `app/config.py` no longer hard-codes its own
  data root. It reads `data_root` from Kelpie's central config
  (`Kelpie/config.py` backed by `kelpie_config.json`), so profile folders land
  under the same root the rest of Kelpie uses. The allowed email domain comes
  from the `NUMBAT_ALLOWED_DOMAIN` env var, then an optional `allowed_domain`
  key in `kelpie_config.json`, then a built-in default (`teamglobalexp.com`).
- **Logging is shared.** All service and startup logging goes through the
  Kelpie logger (`Kelpie/Logger/kelpie_logger.py`). The previous `print` in the
  "run now" path is now a `logger.log_info` call, so NumbatAPI activity lands in
  the global Kelpie log alongside everything else.

## Project structure

```
NumbatAPI/
├── app/
│   ├── __init__.py
│   ├── config.py      # Settings backed by Kelpie's central config
│   ├── main.py        # FastAPI app + endpoints (Kelpie logger wired in)
│   ├── models.py      # Request/response schemas
│   └── services.py    # Business logic (validation, tasks, log reading)
├── requirements.txt
└── README.md
```

## Setup

From the Kelpie project root (`d:\Dev\Others\Kelpie`):

```powershell
# 1. Create a virtual environment
python -m venv .venv

# 2. Activate it (PowerShell)
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r NumbatAPI\requirements.txt
```

> If activation is blocked by execution policy, run once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

## Run

Run uvicorn from the Kelpie project root so the `NumbatAPI.app` package and the
shared Kelpie modules resolve:

```powershell
python -m uvicorn NumbatAPI.app.main:app --reload --host 127.0.0.1 --port 7531
```

- API base URL: http://127.0.0.1:7531
- Interactive docs (Swagger UI): http://127.0.0.1:7531/docs
- Health check: http://127.0.0.1:7531/health

## Configuration

`data_root` is shared with Kelpie via `kelpie_config.json` (or the
`NUMBAT_DATA_ROOT` env override). The allowed domain can be set per run:

```powershell
$env:NUMBAT_ALLOWED_DOMAIN = "teamglobalexp.com"
```

Or in `kelpie_config.json`:

```json
{
    "data_root": "D:\\Data\\Numbat",
    "log_dir": "D:\\Logs\\KelpieLogs",
    "save_script_name": false,
    "allowed_domain": "teamglobalexp.com"
}
```

## Endpoints

| Method & path                                     | Purpose                                        |
| ------------------------------------------------- | ---------------------------------------------- |
| `GET /health`                                     | Service status and configured data root.       |
| `POST /profile`                                   | Validate an email and create/detect a profile. |
| `GET /tasks?email=`                               | List a user's tasks.                           |
| `GET /tasks/{task_name}?email=`                   | Get a task's config + prompt.                  |
| `POST /tasks`                                      | Create or update a task.                       |
| `POST /tasks/{task_name}/run`                     | Trigger an immediate run (logs the request).   |
| `GET /tasks/{task_name}/logs?email=`              | List log dates and runs (no lines).            |
| `GET /tasks/{task_name}/logs/{date}/{task_run_id}?email=` | Get all log lines for one run.         |

### `POST /profile` example

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:7531/profile -Method Post `
  -ContentType "application/json" `
  -Body '{"email":"jane.doe@teamglobalexp.com"}'
```

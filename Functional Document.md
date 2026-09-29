# Kelpie — Functional Document

> **Purpose of this document.** This is a detailed, functional description of the
> existing Kelpie project as it stands today. It is written to be a living
> reference: it will be upgraded and enhanced over time, and the updated
> document is intended to be consumed by AI models to develop the system
> further. Where a behavior is a placeholder, a stub, or "future work," this
> document says so explicitly so that later changes are grounded in how the
> code actually works today.

**Last reviewed against source:** 2026-09-23

---

## 1. What Kelpie is

Kelpie is a Windows-based, filesystem-driven **task scheduler and orchestration
layer** for a family of cooperating bots (referred to collectively as the
"Numbat" bots — Kelpie, Magpie, Swagman, and others). It runs on a fixed cadence
via Windows Task Scheduler, discovers per-user tasks stored as folders on disk,
decides which are due, and executes them. Execution currently routes work to a
web-extraction capability through a RabbitMQ message queue, and can also call a
local Ollama LLM.

There is **no database**. The on-disk folder layout *is* the data model. This is
a deliberate design choice repeated throughout the codebase.

The project bundles several cooperating pieces:

| Piece | Role |
| ----- | ---- |
| **Kelpie core** (`main.py`, `Tasks/`, `config.py`, `Logger/`) | The scheduler: discovers tasks, checks due-ness, runs them, logs. |
| **Ollama client** (`Ollama/`) | Wrapper around a local Ollama LLM install (list/start/chat). |
| **Messaging** (`Messaging/`) | Shared, self-contained RabbitMQ publisher used by all bots. |
| **Swagman** (`Swagman/`) | Web-page capture via the "Lyrebird" browser extension, plus a queue consumer. |
| **NumbatAPI** (`NumbatAPI/`) | FastAPI backend a web/mobile UI calls to manage profiles, tasks, and logs. |

---

## 2. High-level execution flow

The intended pipeline (from `notes.txt`) is:

```
Windows Task Scheduler
        │  (every N minutes)
        ▼
     main.py  ── single-instance lock ──► run_all_tasks()
        │
        ▼
  Discover tasks on disk  ──►  For each enabled + due task:
        │
        ├─ derive/persist a per-profile session counter
        ├─ load the task's prompt (TaskPrompt.txt)
        ├─ stage InProgress/<session>/ (session_context.txt + status.json)
        └─ publish a web-extract job to RabbitMQ (Swagman/WebExtract queue)
                                   │
                                   ▼
                    Swagman queue_consumer.py (separate process)
                                   │
                                   ▼
                    extract_web_page() → Lyrebird bridge → Edge downloads page
                                   │
                                   ▼
                     capture moved into the destination folder
```

In parallel, **NumbatAPI** exposes the same on-disk task model to a UI so users
can create/edit/delete/run tasks and read per-task logs. NumbatAPI reuses the
Kelpie task runner's `execute_task` for its "Run Now" endpoint.

> **Note on the conceptual flow vs. the code today.** `notes.txt` describes an
> LLM-centric flow (prompt → Ollama → structured response → action/report).
> The Ollama path is fully implemented in `Ollama/ollama_client.py` and wired
> into `Tasks/task_runner.py`'s first `execute_task` definition, but the
> **active** `execute_task` (the second definition, which shadows the first)
> currently publishes a **web-extract job** rather than calling Ollama. See
> [§6.4](#64-important-there-are-two-execute_task-definitions).

---

## 3. Repository layout

```
Kelpie/
├── main.py                     # Scheduler entry point (single-instance lock)
├── config.py                   # Project-wide config loader (kelpie_config.json)
├── kelpie_config.json          # data_root, log_dir, save_script_name
├── verify_imports.py           # Smoke test for core imports + config load
├── notes.txt                   # Scheduler command + conceptual flow notes
│
├── Logger/
│   └── kelpie_logger.py        # Global + per-task logging, session counter stamping
│
├── Tasks/
│   ├── task_runner.py          # Task discovery, due-ness, execution, state
│   ├── prompt_builders.py      # Prompt assembly (currently pass-through)
│   ├── task_execution_state.json  # Last-run timestamps per task
│   └── __init__.py
│
├── Ollama/
│   ├── ollama_client.py        # CLI + library wrapper around local Ollama
│   ├── ollama_config.json      # host, keep_alive, models, default_model
│   └── __init__.py
│
├── Messaging/
│   ├── queue_publisher.py      # Self-contained RabbitMQ publisher (pika)
│   ├── queue.config            # Broker connection + per-queue settings (JSON)
│   └── __init__.py
│
├── Swagman/
│   ├── swagman.py              # Public entry point: extract_web_page()
│   ├── web_extract.py          # Lyrebird bridge client + download handling
│   └── queue_consumer.py       # RabbitMQ consumer for Swagman/WebExtract
│
├── NumbatAPI/
│   ├── app/
│   │   ├── main.py             # FastAPI app + endpoints
│   │   ├── services.py         # Business logic (profiles, tasks, logs)
│   │   ├── models.py           # Pydantic request/response + TaskConfig
│   │   ├── config.py           # data_root + allowed_domain settings
│   │   └── __init__.py
│   ├── requirements.txt
│   └── README.md
│
└── Reference/
    ├── TASKS.md                # End-to-end task subsystem documentation
    └── RabbitMQ ... Swagman_WebExtract ... .html   # Saved queue mgmt page
```

---

## 4. On-disk data model

Everything a task needs lives on disk under a **data root** (default
`D:\Data\Numbat`, key `data_root` in `kelpie_config.json`, overridable via the
`NUMBAT_DATA_ROOT` environment variable).

```
<DATA_ROOT>/
└── <email>/                          # profile folder (e.g. jane@teamglobalexp.com)
    ├── Task_Config.json              # per-profile session_counter (runtime state)
    └── Tasks/
        └── <task_name>/              # folder name == task name
            ├── TaskConfig.json       # persisted task configuration
            ├── TaskPrompt.txt        # prompt text, stored verbatim
            ├── Logs/                 # per-task logs, one file per date
            │   └── task_YYYY-MM-DD.log
            └── InProgress/           # active session artifacts
                └── <session_counter>/
                    ├── session_context.txt   # the prompt handed to the next bot
                    └── status.json           # handoff status (current/next bot)
```

Rules that the code enforces:

- A folder is treated as a task **only if** it contains a `TaskConfig.json`.
- The task name doubles as its folder name, so it must be filesystem-safe.
- A task folder suffixed with `_DELETED` is a soft-deleted task and is skipped by
  listings (see [§9.4](#94-soft-delete-and-stop)).
- Presence of any subfolder under a task's `InProgress/` means the task has an
  active, unfinished session — a second run is refused (see
  [§6.5](#65-single-run-guard-inprogress)).

### 4.1 TaskConfig fields

Validated by `NumbatAPI/app/models.py` (`TaskConfig`, Pydantic):

| Field | Type | Rules / meaning |
| ----- | ---- | --------------- |
| `name` | string | 1–100 chars, pattern `^[A-Za-z0-9][A-Za-z0-9 _-]*$`. Also the folder name. |
| `frequency_in_minutes` | integer | `>= 1` and `<= 525600` (one year). How often the task is due. |
| `enabled` | boolean | Default `true`. Disabled tasks are skipped by the scheduler. |
| `web_extract` | boolean | Default `false`. Whether the task may extract from the web. |
| `web_urls` | string | Default `""`. One or more URLs separated by `;` or `,`. Persisted regardless of `web_extract`. |

> **Documentation drift to be aware of.** `Reference/TASKS.md` (§2.1) describes a
> `web_access` boolean instead of `web_extract`/`web_urls`. The **source of
> truth is `models.py`**, which uses `web_extract` + `web_urls`. This document
> follows the code. This inconsistency is a candidate for cleanup.

### 4.2 Execution state (`Tasks/task_execution_state.json`)

A single JSON file, local to the Kelpie install (not per-profile), tracks the
last run of every task. Keyed by `"<email>/<task_name>"` so tasks with the same
name in different profiles never collide.

```json
{
  "tasks": {
    "pm@teamglobalexp.com/News Extract": {
      "email": "pm@teamglobalexp.com",
      "name": "News Extract",
      "last_execution": "2026-09-22T16:57:20.251877",
      "last_status": "success"
    }
  }
}
```

`last_status` is `"success"` when execution produced a response string, else
`"failed"`. The timestamp is saved **regardless of success** to avoid retry
storms.

### 4.3 Session counter (`<profile>/Task_Config.json`)

The logging session counter is **runtime state, not configuration**, so it is
persisted per profile rather than in the shared config. Each qualifying run
increments the profile's `session_counter`, writes it back immediately, and
stamps it on the logger so every log line for that run carries it.

```json
{ "session_counter": 42 }
```

---

## 5. Global configuration

### 5.1 `config.py` + `kelpie_config.json`

`config.py` is the single project-wide source of truth for shared settings. It
reads `kelpie_config.json` once per process (cached), layering file values over
built-in defaults.

| Key | Default | Meaning | Env override |
| --- | ------- | ------- | ------------ |
| `data_root` | `D:\Data\Numbat` | Root of profile/task data. | `NUMBAT_DATA_ROOT` |
| `log_dir` | `D:\Logs\KelpieLogs` | Directory for global Kelpie logs. | `KELPIE_LOG_DIR` |
| `save_script_name` | `false` | Whether log lines include the source script name. | — |

Accessors: `get(key, default)`, `get_data_root()`, `get_log_dir()`,
`get_save_script_name()`. Missing file or malformed JSON falls back to defaults
silently so logging and discovery never crash on a bad config.

### 5.2 Other config files

- **`Ollama/ollama_config.json`** — `ollama_host`, `keep_alive`, `models` list,
  `default_model`.
- **`Messaging/queue.config`** — a `connection` block (host, port, credentials,
  vhost, retry) and a `queues` map. The `web_extract` queue entry names the
  `Swagman/WebExtract` queue.
- **`NumbatAPI/app/config.py`** — `data_root` (env `NUMBAT_DATA_ROOT`) and
  `allowed_domain` (env `NUMBAT_ALLOWED_DOMAIN`, default `teamglobalexp.com`).

---

## 6. Kelpie core: scheduler and task runner

### 6.1 `main.py` — scheduled entry point

Invoked by Windows Task Scheduler on a fixed cadence. The `notes.txt` install
command schedules it every 5 minutes:

```
schtasks /Create /TN "Kelpie Scheduled Task" /TR "py -3 D:\AISpace\Kelpie\main.py" /SC MINUTE /MO 5 /RL HIGHEST /F
```

> Note: the scheduled path in `notes.txt` (`D:\AISpace\Kelpie`) differs from the
> current workspace path (`d:\Dev\Others\Kelpie`); update the scheduler command
> to the actual install location.

**Single-instance lock.** Because runs could overlap, `main.py` acquires a lock
file (`kelpie.lock` at the project root) atomically via
`os.open(..., O_CREAT | O_EXCL | O_WRONLY)`:

- If the lock exists and the owning PID is still running (checked via `tasklist`
  on Windows, `os.kill(pid, 0)` elsewhere), the run logs "Previous run still in
  progress; skipping this execution." and exits.
- If the lock is stale (owner PID dead), it is removed and reclaimed.
- The lock is released in a `finally` block, but only if it belongs to the
  current PID.

On acquiring the lock, `main()` calls `run_all_tasks()`, logs completion, and
appends a separator line to the global log.

### 6.2 Task discovery (`discover_tasks`)

Walks `<DATA_ROOT>/<email>/Tasks/<task_name>`. Any folder containing a
`TaskConfig.json` becomes a `DiscoveredTask` carrying:

- `email`, `name`, parsed `config`
- `task_dir`, `profile_dir`
- derived `prompt_path` (`TaskPrompt.txt`), `logs_dir` (`Logs/`)
- `state_key` = `"<email>/<name>"`

Malformed or missing configs are logged and skipped; a profile without a `Tasks`
folder simply has no tasks.

### 6.3 Due-ness (`is_task_due`) and the run loop (`run_all_tasks`)

For each discovered task, `run_all_tasks`:

1. Resolves the display name (config `name`, else folder name).
2. Skips if `enabled` is false.
3. Parses `frequency_in_minutes`; skips if invalid or `<= 0`.
4. Checks due-ness: a task is due if it has never run, has an invalid last-run
   timestamp, or `now >= last_execution + frequency_in_minutes`.
5. Calls `execute_task` for due tasks.

### 6.4 IMPORTANT: there are two `execute_task` definitions

`Tasks/task_runner.py` defines `execute_task` **twice**. In Python the second
definition **shadows** the first, so the second is the one that actually runs.

- **First `execute_task(task, prompt_content)`** — builds the prompt, loads the
  Ollama config, resolves the model, and calls `send_message` (the LLM path).
  This function is **dead code** as written because it is overwritten.
- **Second `execute_task(task, config_name, state)`** — the **active** one. It:
  1. Guards against an existing in-progress session (see [§6.5](#65-single-run-guard-inprogress)).
  2. Derives + persists a new session counter for the profile.
  3. Loads the task's prompt from `TaskPrompt.txt`.
  4. Stages `InProgress/<session>/` with `session_context.txt` + `status.json`.
  5. Publishes a **web-extract job** to the `Swagman/WebExtract` queue via
     `publish_web_extract`. The published payload currently **hardcodes**
     `web_url` to `https://www.news.com.au/` and uses the session directory as
     both `task_identifier` and `destination_folder_name`.
  6. Records `last_execution` + `last_status` in the execution state.

> **Enhancement flags for AI models working on this file:**
> - The two `execute_task` definitions should be reconciled — either merge the
>   LLM path and the queue path, or rename one. As-is, the LLM path is
>   unreachable through the runner.
> - The web-extract `web_url` is hardcoded to a news site and ignores the task's
>   `web_urls`/`web_extract` config fields. A real implementation should read the
>   URL(s) from `TaskConfig`.
> - The return values are human-readable strings (e.g. "Request queued for web
>   extract successfully"). `last_status` is derived from truthiness of that
>   string, so it is always `"success"` unless the string is empty.

### 6.5 Single-run guard (`InProgress`)

Before running, `execute_task` checks the task's `InProgress/` folder. If **any**
subfolder exists there, a prior run has not finished its handoff, so the current
execution is terminated with a message rather than running the same task
concurrently.

### 6.6 Session artifacts (`prepare_session_context`)

Creates `InProgress/<session_counter>/` and writes:

- **`session_context.txt`** — the raw prompt content handed to the next bot.
- **`status.json`** — created only if absent, describing the handoff:
  ```json
  {
    "Current_bot": "Kelpie - Task Scheduler",
    "Next_bot": "Magpie - AI Agent",
    "Next_Action": "Analyse and start the Task"
  }
  ```

### 6.7 Prompt building (`prompt_builders.py`)

`build_prompt(task_name, template_content)` currently returns the template
content **unchanged** — the prompt is sent to the model verbatim, with no
preamble or wrapping. It exists as an extension point for future prompt shaping.

---

## 7. Logging (`Logger/kelpie_logger.py`)

Two log destinations, sharing one format:

```
HH:MM:SS [<session_counter>] [LEVEL] <message>
```

When `save_script_name` is enabled the source script name is inserted:
`HH:MM:SS <source> [<session_counter>] [LEVEL] <message>`.

- **Global log** — `<log_dir>/kelpie_YYYY-MM-DD.log`, one file per day. Written
  by `log_info` / `log_warning` / `log_error` / `log_debug` / `log_critical`.
- **Per-task log** — `<task_dir>/Logs/task_YYYY-MM-DD.log`, one file per day.
  Written by `log_task_info` / `log_task_warning` / `log_task_error`. Failures
  in per-task logging are swallowed so they can never abort a running task.

**Session counter.** The logger does not own or derive the counter. The task
runner calls `set_session_counter(n)` (from `next_session_counter`) so all
subsequent lines carry that run's id. Before the first task run in a process it
defaults to `0`.

This log line format is a **contract** consumed by NumbatAPI's log parser
(§9.5) — the `[<run_id>]` and `HH:MM:SS` prefix must be preserved.

---

## 8. Ollama client (`Ollama/ollama_client.py`)

A CLI and importable wrapper around a locally running Ollama server. Requires
the `ollama` Python package and a running Ollama install.

**Library functions:**

- `load_config()` — reads `ollama_config.json`; warns if no `default_model`.
- `get_client(config)` — builds an `ollama.Client` for `ollama_host`
  (default `http://localhost:11434`).
- `resolve_model(requested, config)` — explicit request wins, else
  `default_model`; raises if neither is available.
- `list_models(client)` / `list_running_models(client)` — installed vs. loaded.
- `start_model(client, model, keep_alive)` — preloads a model with an empty
  generate call.
- `send_message(client, model, message, keep_alive)` — single-shot chat; returns
  the reply text or `None` on error.
- `interactive_chat(...)` — multi-turn console session with history.

**CLI subcommands:** `list`, `running`, `config`, `start [--model]`,
`chat [--model] [--message]`. When `--message` is omitted, `chat` starts an
interactive session.

`ollama_config.json` (current): host `http://localhost:11434`, `keep_alive` `5m`,
a `models` list, and `default_model` `qwen3.5:4b`.

---

## 9. Messaging + Swagman (web extraction path)

### 9.1 Publisher (`Messaging/queue_publisher.py`)

A **self-contained** RabbitMQ publisher depending only on the standard library
and `pika`. It has no coupling to Kelpie config/logger, so the whole `Messaging`
folder can be vendored verbatim into any bot ("option 3 — shared script copied
between bots").

- `QueueSettings` dataclass — host, port, queue, credentials, vhost, exchange,
  routing key, durable, retry. `from_mapping` ignores unknown keys.
- `publish_message(settings, payload, log=None)` — opens a short-lived
  connection, declares the queue durable, publishes a **persistent** JSON
  message, closes. Raises `PublishError` on failure. `dict`/`list` payloads are
  JSON-encoded; `str` is sent as-is.
- Configuration comes from `queue.config` (a `connection` block + per-queue
  entries). `_settings_for(config_key)` layers connection defaults, then the
  named queue's block.
- `publish_web_extract(payload, log=None)` — convenience wrapper that targets the
  `web_extract` queue entry (`Swagman/WebExtract`), so callers pass only the
  payload.

**Message contract:**

```json
{
  "task_identifier": "Extract_Coles_Menu",
  "web_url": "https://www.coles.com.au/browse",
  "destination_folder_name": "coles_item_categories"
}
```

### 9.2 Consumer (`Swagman/queue_consumer.py`)

The consuming counterpart. Listens on `Swagman/WebExtract` (vhost `/`) and hands
each job to `Swagman.swagman.extract_web_page`.

- `ConsumerSettings` dataclass mirrors the publisher's connection settings, plus
  `prefetch` (default 1 — one job at a time) and `durable`.
- Accepts field **aliases**: `task`/`name` → `task_identifier`, `url` →
  `web_url`, `destination`/`folder` → `destination_folder_name`.
- **Acknowledgement policy:** manual ack, prefetch 1.
  - Malformed payload → **ack** (dropped, never redelivered in a loop).
  - Successful/clean job → **ack**.
  - Unexpected processing error → **nack** with `requeue=False` (dropped or
    dead-lettered).
- Three run modes:
  - `consume(settings)` — blocking subscription until Ctrl+C / connection drop.
  - `consume_forever(settings, reconnect_delay=5)` — always-on, auto-reconnecting
    service. **This is the CLI default.**
  - `drain(settings)` — process everything currently queued, then return (for
    scheduled/one-shot invocation, `--once`).
- CLI: `--host --port --queue --username --password --virtual-host --prefetch`
  and `--once` (drain and exit instead of running forever).

### 9.3 Swagman public entry point (`Swagman/swagman.py`)

`extract_web_page(task_identifier, web_url, destination_folder_name=None)`:

- Validates `task_identifier` and `web_url` as non-empty strings (`ValueError`
  otherwise). `destination_folder_name` is optional; blank normalizes to `None`.
- Assembles a task dict and delegates to `run_web_extract_task`.
- Also runnable as a CLI (`--task-identifier --web-url
  --destination-folder-name`), exit 0 on success / 1 on failure.

### 9.4 Web extraction mechanics (`Swagman/web_extract.py`)

Captures a web page through the **Lyrebird** browser extension rather than
cloning the site directly:

```
Python  <-- TCP 127.0.0.1:8787 -->  Lyrebird bridge host  <-- stdio -->  Lyrebird extension (Edge)
```

Flow of `run_web_extract_task(task)`:

1. **Build filename** — from destination label + URL host/path slug + timestamp,
   e.g. `2026-09-09_14-06-11_www.coles.com.au_browse.html`. Filename only, no
   path separators (the bridge treats it as a filename).
2. **Send to bridge** — one newline-terminated JSON request
   `{ "url": ..., "filename": ... }` over a short-lived TCP socket. Bridge
   timeout 15s. A `status: "forwarded"` response means the extension accepted it.
3. **Wait for download** — poll the user's `~/Downloads` folder (up to 45s,
   every 1s) for the file, tolerating browser de-dup suffixes like `name (1).html`
   and skipping while an in-progress `.crdownload` partial exists.
4. **Move into destination** — an absolute `destination_folder` is used as-is; a
   plain name is created under a `.web` folder at the project root; when omitted,
   the capture goes to `<task_identifier>/web_extract`.

Returns the final moved path on success, or `None` on failure/timeout.

> **External dependency:** this path requires Edge running with the Lyrebird
> extension and the native-messaging bridge host registered and listening on
> `127.0.0.1:8787`. Without it, `_send_to_bridge` returns a structured error and
> the capture fails cleanly.

---

## 10. NumbatAPI (backend for the UI)

A FastAPI backend (`NumbatAPI/app`) that a web/mobile UI calls to manage
profiles, tasks, and logs. It reuses Kelpie's central config and logger, and
reuses the task runner's `execute_task` for immediate runs.

**Run it (from the Kelpie project root):**

```powershell
python -m uvicorn NumbatAPI.app.main:app --reload --host 127.0.0.1 --port 7531
```

Swagger UI at `/docs`, health at `/health`. CORS is wide open (`allow_origins=["*"]`)
and should be tightened for production.

### 10.1 Endpoints

| Method & path | Purpose |
| ------------- | ------- |
| `GET /health` | Service status + configured data root. |
| `POST /profile` | Validate email domain; create/detect the profile folder. |
| `GET /tasks?email=` | List a user's tasks (`{ name, enabled }`). |
| `GET /tasks/{task_name}?email=` | Get a task's config + prompt. |
| `POST /tasks` | Create or update a task (`create_only` guards creates). |
| `DELETE /tasks/{task_name}?email=` | Soft-delete a task. |
| `POST /tasks/{task_name}/run` | Trigger an immediate run (calls `execute_task`). |
| `POST /tasks/{task_name}/stop` | Hard-delete the task's `InProgress/` folder. |
| `GET /tasks/{task_name}/logs?email=` | List log dates + runs (no lines). |
| `GET /tasks/{task_name}/logs/{date}/{task_run_id}?email=` | Get one run's lines. |

### 10.2 Profiles and domain validation

- `validate_domain(email)` — rejects any email not under `allowed_domain`
  (default `teamglobalexp.com`) with `DomainNotAllowedError` → HTTP 400. Returns
  the local part.
- `process_profile(email)` — creates `<data_root>/<email>` if missing
  ("A profile for <local> is created"), else welcomes back
  ("Well come back <local>").

### 10.3 Task CRUD (`services.py`)

- `list_tasks` — lists subfolders of `<profile>/Tasks` that contain
  `TaskConfig.json`, skipping `_DELETED` folders; reads `enabled` cheaply.
- `get_task` — returns validated `TaskConfig` + prompt text (empty string if no
  prompt file).
- `save_task(create_only=...)` — full rewrite of `TaskConfig.json`
  (4-space indent) + `TaskPrompt.txt`. With `create_only=True`, refuses to
  overwrite an existing task (`TaskExistsError` → 409).

### 10.4 Soft-delete and stop

- `delete_task` — **soft delete**: renames `<task>` to `<task>_DELETED` so it is
  hidden from listings and the name is freed. If a `_DELETED` folder already
  exists it is hard-deleted first.
- `stop_task` — hard-deletes `<task>/InProgress` recursively to cancel staged
  work. Idempotent (succeeds even if the folder is absent).

### 10.5 Run Now

`run_task(email, task_name)` builds the same `DiscoveredTask` shape the scheduler
uses, loads the shared execution state, and calls the task runner's active
`execute_task` directly — **bypassing** the enabled/frequency/due checks. So a
"Run Now" goes straight through the web-extract publish path described in §6.4.

> `Reference/TASKS.md` §7 still describes Run Now as a stub that only logs the
> request. That is **out of date**: `services.run_task` now performs a real run
> via `execute_task`. This document reflects the code.

### 10.6 Log reading (parser contract)

Log filenames must match `^task_(\d{4}-\d{2}-\d{2})\.log$`. Each parsed log line
must start with `^(\d{2}:\d{2}:\d{2})\s+\[(\d+)\]` (time + run id); non-matching
lines are skipped.

- `list_log_runs` — scans each date file once, discovers distinct run ids and
  each run's first-seen start time; returns dates newest→oldest with runs
  newest→oldest by id. Missing `Logs/` → empty list (not 404).
- `get_log_run_lines` — returns the lines for one run in file order
  (oldest→newest). Missing file/run → `LogRunNotFoundError` → 404.

Both read UTF-8 with `errors="replace"` so a stray byte never fails a read. This
is why the logger's line format (§7) is a hard contract.

### 10.7 Error mapping

| Condition | Status | Exception |
| --------- | ------ | --------- |
| Email domain not allowed | 400 | `DomainNotAllowedError` |
| Profile folder missing | 404 | `ProfileNotFoundError` |
| Task folder/config missing | 404 | `TaskNotFoundError` |
| Log file/run not found | 404 | `LogRunNotFoundError` |
| `create_only` but task exists | 409 | `TaskExistsError` |

Errors surface as `{ "detail": "<message>" }`.

---

## 11. Cross-cutting conventions

- **Config format is JSON everywhere** (`kelpie_config.json`, `ollama_config.json`,
  `queue.config`, `TaskConfig.json`, `task_execution_state.json`) to avoid
  third-party dependencies and stay consistent.
- **`sys.path` bootstrapping.** Most modules insert the project root and
  `Logger/`/`Ollama/` onto `sys.path` at import time so they run regardless of
  the working directory Task Scheduler picks.
- **Fail-safe logging.** Bad configs, unreadable files, and per-task log write
  failures are swallowed/degraded rather than crashing a run.
- **Stateless publishing.** The RabbitMQ publisher opens a short-lived connection
  per call rather than sharing a fragile long-lived one across scheduler runs.
- **`verify_imports.py`** is a quick smoke test that core Ollama imports resolve
  and the config loads with a `default_model`.

---

## 12. Known gaps, drift, and enhancement candidates

This section is intentionally explicit so future AI-driven development starts
from an accurate picture.

1. **Two `execute_task` definitions** (`Tasks/task_runner.py`) — the LLM path is
   shadowed/dead; only the web-extract publish path runs. Reconcile them.
2. **Hardcoded extraction URL** — the active `execute_task` always publishes
   `https://www.news.com.au/` and ignores `web_extract`/`web_urls` from the
   task's config. Wire the real URL(s) through.
3. **`last_status` is effectively always `"success"`** — it is derived from the
   truthiness of a human-readable return string. Introduce a real success/failure
   signal.
4. **Config field naming drift** — `models.py` uses `web_extract` + `web_urls`;
   `Reference/TASKS.md` documents `web_access`. Align docs and code.
5. **Run Now docs are stale** — `TASKS.md` §7 calls it a stub; it now runs for
   real. Update the reference doc.
6. **Scheduler path mismatch** — `notes.txt` schedules `D:\AISpace\Kelpie`, but
   the project lives at `d:\Dev\Others\Kelpie`.
7. **`ollama_config.json` model names** look inconsistent (e.g. `qwen3.8:27b`,
   `qwen3.5:4b`); confirm against actually installed models.
8. **CORS wide open** in NumbatAPI — tighten `allow_origins` for production.
9. **External runtime dependencies** — web extraction needs Edge + the Lyrebird
   extension + bridge on `127.0.0.1:8787`; the messaging path needs a running
   RabbitMQ broker. Neither is bundled or health-checked by Kelpie.
10. **Handoff to "Magpie"** — `status.json` names Magpie as the next bot, but no
    Magpie consumer exists in this repo yet; the handoff is staged but not
    consumed by an agent.

---

## 13. Glossary

| Term | Meaning |
| ---- | ------- |
| **Kelpie** | The task scheduler / orchestrator (this project's core). |
| **Numbat** | The overall bot family / product; also the data root name. |
| **Magpie** | The intended downstream AI agent bot (named in handoff, not in this repo). |
| **Swagman** | The web-page capture capability + its queue consumer. |
| **Lyrebird** | The Edge browser extension (+ native bridge) that performs the actual page capture. |
| **Session counter** | A per-profile, monotonically increasing run id stamped into every log line of a run. |
| **Profile** | A per-user folder (named by email) under the data root, holding that user's tasks. |
| **Run / task_run_id** | All log lines sharing one session counter within a date's log file. |
```
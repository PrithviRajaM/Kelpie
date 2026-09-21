# Tasks: Technical Documentation

How tasks are created, stored, managed, executed, and logged across **NumbatUI**
(Expo / React Native Web) and **NumbatAPI** (FastAPI). This covers the task
config, the task prompt, and the shape/expectations of task logs.

> Companion docs: the top-level [`../README.md`](../README.md) explains build
> and run; this document explains the task subsystem end to end.

---

## 1. Concepts at a glance

A **task** is a named, per-user unit of scheduled work. Each task owns:

- a **config** (`TaskConfig.json`) — name, run frequency, enabled flag, web access flag;
- a **prompt** (`TaskPrompt.txt`) — free-form multiline instructions describing what the task should do;
- a **logs** folder (`Logs/`) — one log file per calendar date, each holding one or more runs.

Everything is stored on the backend filesystem under the owning user's profile
folder. There is no database; the folder layout **is** the data model.

```
<NUMBAT_DATA_ROOT>/
└── <email>/                         # profile folder (e.g. jane.doe@teamglobalexp.com)
    └── Tasks/
        └── <task_name>/             # one folder per task; folder name == task name
            ├── TaskConfig.json      # persisted TaskConfig
            ├── TaskPrompt.txt       # persisted prompt text
            └── Logs/
                ├── task_2026-09-10.log
                └── task_2026-09-09.log
```

- `NUMBAT_DATA_ROOT` defaults to `D:\Data\Numbat` (override via env var).
- The **task name doubles as its folder name**, so it must be filesystem-safe.
- A folder is only treated as a task if it contains a `TaskConfig.json`.

---

## 2. Data model

### 2.1 TaskConfig

The persisted config, validated identically on both ends.

| Field                  | Type    | Rules                                                                                     |
| ---------------------- | ------- | ----------------------------------------------------------------------------------------- |
| `name`                 | string  | 1–100 chars; pattern `^[A-Za-z0-9][A-Za-z0-9 _-]*$` (starts alphanumeric; filesystem-safe) |
| `frequency_in_minutes` | integer | `>= 1` and `<= 525600` (one year in minutes)                                              |
| `enabled`              | boolean | Whether the task is active. Default `true`.                                               |
| `web_access`           | boolean | Whether the task may access the web. Default `false`.                                     |

Backend definition: `NumbatAPI/app/models.py` (`TaskConfig`, Pydantic).
Frontend mirror: `NumbatUI/src/services/taskService.ts` (`TaskConfig` type).

Persisted `TaskConfig.json` example (written with 4-space indent):

```json
{
    "name": "Morning digest",
    "frequency_in_minutes": 60,
    "enabled": true,
    "web_access": false
}
```

### 2.2 TaskPrompt

Free-form multiline text stored verbatim as `TaskPrompt.txt`. There is no length
or content validation; an empty prompt is allowed and persists as an empty file.
The prompt is what will eventually drive task execution (see
[§7 Execution](#7-execution-run-now)).

### 2.3 Summaries and detail

- **TaskSummary** — `{ name, enabled }`, used for the left-hand list. `enabled`
  is read cheaply from each config without loading the full detail.
- **TaskDetail** — `{ config, prompt }`, returned when a single task is opened.

---

## 3. Backend API surface

All task endpoints live in `NumbatAPI/app/main.py`; logic is in
`NumbatAPI/app/services.py`. Every endpoint validates the email domain first
(`@teamglobalexp.com` by default) and requires the user's profile folder to
already exist.

| Method & path                                        | Purpose                                   | Success body                          |
| ---------------------------------------------------- | ----------------------------------------- | ------------------------------------- |
| `GET /tasks?email=`                                  | List a user's tasks                       | `{ tasks: [{ name, enabled }] }`      |
| `GET /tasks/{task_name}?email=`                      | Get one task's config + prompt            | `{ config, prompt }`                  |
| `POST /tasks`                                        | Create or update a task                   | `{ message, name, created }`          |
| `POST /tasks/{task_name}/run`                        | Trigger an immediate run ("Run Now")      | `{ message, name }`                   |
| `GET /tasks/{task_name}/logs?email=`                 | List log dates + runs (no lines)          | `{ dates: [{ date, runs }] }`         |
| `GET /tasks/{task_name}/logs/{date}/{run_id}?email=` | Get all log lines for one run             | `{ date, task_run_id, lines }`        |

### 3.1 Create / update contract

`POST /tasks` request body:

```json
{
  "email": "jane.doe@teamglobalexp.com",
  "config": {
    "name": "Morning digest",
    "frequency_in_minutes": 60,
    "enabled": true,
    "web_access": false
  },
  "prompt": "Summarize overnight alerts and email me the highlights.",
  "create_only": true
}
```

- `create_only: true` means "this is a create": the backend refuses to overwrite
  an existing task and returns **409 Conflict**. The UI sends `true` when no task
  is selected (create mode) and `false` when editing an existing task.
- On success, `created` is `true` when the task folder did not previously exist.

Writing a task is a full rewrite: `save_task` writes `TaskConfig.json`
(serialized from the validated model) and `TaskPrompt.txt` (verbatim prompt)
into `<profile>/Tasks/<name>`, creating the folder if needed.

### 3.2 Error mapping

| Condition                                   | Status | Detail source                     |
| ------------------------------------------- | ------ | --------------------------------- |
| Email domain not allowed                    | 400    | `DomainNotAllowedError`           |
| Profile folder missing                      | 404    | `ProfileNotFoundError`            |
| Task folder / config missing                | 404    | `TaskNotFoundError`               |
| Log file for date, or run id, not found     | 404    | `LogRunNotFoundError`             |
| `create_only` but task already exists       | 409    | `TaskExistsError`                 |

Errors are returned as `{ "detail": "<message>" }`, which the UI surfaces
directly.

---

## 4. Frontend service layer

Two services isolate all HTTP from the screens, each with a real and a mock
implementation selected at build time by `USE_MOCK_AUTH`
(`NumbatUI/src/config/index.ts`, driven by `EXPO_PUBLIC_USE_MOCK_AUTH`).

- **`taskService`** (`src/services/taskService.ts`) — `listTasks`, `getTask`,
  `saveTask`, `runNow`. `HttpTaskService` calls the API; `MockTaskService`
  keeps an in-memory per-email store for offline UI work.
- **`logService`** (`src/services/logService.ts`) — `listRuns`, `getLines`.
  Split into two calls on purpose so the UI can render the date/run tree fast
  and fetch (potentially large) log lines only when a run is expanded.

Every service method returns a normalized result object
(`{ success, message, ...payload }`) and never throws for network/HTTP errors:

- network failure → `success: false`, message `"Cannot reach the server."`
- non-2xx → `success: false`, message taken from the response `detail`
- 2xx → `success: true` with the parsed payload

This keeps screen code free of try/catch and consistent across platforms.

---

## 5. Frontend UI flow

The task UI is `NumbatUI/src/screens/TasksScreen.tsx`, a two-column layout:

- **Left rail** — the task list. "Add" clears the form for a new task;
  "Refresh" reloads the list; each item shows an On/Off badge from `enabled`.
- **Right panel** — a Create/Edit area containing:
  - **Task Config** section: `Name`, `Frequency (minutes)`, an **Enabled**
    toggle, and a **Web Access** toggle (boolean, saved as `true`/`false`).
  - **Task Prompt** section: a multiline text area.
  - **Save** / **Clear** actions, plus a **Run Now** button (enabled only for a
    saved, selected task).
  - A full-height **Task Log** panel alongside (see [§6](#6-logs)).

Key behaviors:

- **Create vs edit** is derived from `selected`. When nothing is selected the
  screen is in create mode and `saveTask` is called with `createOnly: true`,
  so a duplicate name surfaces as a save error.
- **Validation** runs client-side before save via
  `validateTaskConfig` (`src/utils/validation.ts`), which mirrors the backend
  rules for `name` and `frequency`. Field errors show inline; the backend
  re-validates as the source of truth.
- **Loading a task** maps the API's snake_case config onto the form's fields
  (`web_access` → `webAccess`, `frequency_in_minutes` → `frequency` string).
- **Panel management** — the Config/Prompt column and the Log panel can each be
  collapsed to a rail, but never both at once.

Form-to-config mapping on save:

| Form field  | Config field           | Transform                     |
| ----------- | ---------------------- | ----------------------------- |
| `name`      | `name`                 | trimmed                       |
| `frequency` | `frequency_in_minutes` | `Number(trim)`                |
| `enabled`   | `enabled`              | as-is (boolean)               |
| `webAccess` | `web_access`           | as-is (boolean)               |
| `prompt`    | (sent alongside)       | verbatim                      |

---

## 6. Logs

### 6.1 On-disk format (the contract)

Logs are plain text files in each task's `Logs/` folder, **one file per date**:

```
Logs/task_YYYY-MM-DD.log
```

Filenames must match `^task_(\d{4}-\d{2}-\d{2})\.log$` (case-insensitive) to be
recognized; other files are ignored.

Each **log line** is expected to begin with a time and a run id:

```
HH:MM:SS [<task_run_id>] <rest of the line>
```

Matched by `^(\d{2}:\d{2}:\d{2})\s+\[(\d+)\]`. Lines that don't match this
prefix are skipped by the parser (so blank lines or free-form output between
entries won't break listing, but also won't be returned).

Example `task_2026-09-10.log`:

```
12:20:23 [32] [INFO] Scheduled execution started.
12:20:28 [32] [INFO] Task runner started.
12:21:05 [32] [INFO] Scheduled execution completed.
12:28:02 [33] [INFO] Scheduled execution started.
12:29:25 [34] [INFO] Manual run requested.
```

Definitions:

- A **run** is the set of all lines sharing the same `task_run_id` within a file.
- A run's **start_time** is the timestamp of its first line in the file.
- Multiple runs coexist in one date's file; run ids are integers.

### 6.2 How the backend reads logs

- `list_log_runs` scans each date file once, discovers distinct run ids and
  their first-seen start times, and returns dates **newest-to-oldest** with runs
  within each date sorted **newest-to-oldest by run id**. No log lines are read
  into the response. A missing `Logs/` folder yields an empty list (not a 404).
- `get_log_run_lines` opens one date file and returns only the lines whose run
  id matches, **in file order (oldest-to-newest as written)**, with trailing
  newlines stripped. A missing file, unreadable file, or absent run id raises
  `LogRunNotFoundError` → 404.

Both use UTF-8 with `errors="replace"` so a stray byte never fails a read.

### 6.3 How the UI renders logs

`NumbatUI/src/components/TaskLogPanel.tsx` renders a lazy, collapsible tree:

```
date (newest → oldest)
  └─ Run #<id>  <start_time>   (newest → oldest)
       └─ log lines (as written: oldest → newest)
```

- The date/run listing loads via `logService.listRuns` on mount and whenever the
  selected task changes.
- Log lines load via `logService.getLines` **lazily**, the first time a run is
  expanded, and are cached per `date#runId`. A slow log fetch never blocks
  editing or saving because it is independent of the config/prompt form.
- Multiple dates and runs can be expanded simultaneously. Log lines render in a
  monospace, selectable block.

---

## 7. Execution ("Run Now")

`POST /tasks/{task_name}/run` is wired end to end at the API/UI boundary but is
**not yet a real executor**. Today `run_task` validates the profile and task,
then logs the request server-side (`print`) and returns an acknowledgement
(`Run requested for '<name>'`). The scheduler that runs tasks on their
`frequency_in_minutes` cadence and the executor that consumes the prompt and
`web_access` flag are future work.

**Expectation for when execution lands:** each run should append lines to
`Logs/task_<today>.log` using the format in [§6.1](#61-on-disk-format-the-contract)
with a fresh, monotonically increasing `task_run_id`, so the existing log
listing and viewer keep working without changes.

---

## 8. End-to-end example

1. User signs in (`jane.doe@teamglobalexp.com`); their profile folder exists.
2. In the UI, they click **Add**, fill Name = `Morning digest`, Frequency = `60`,
   leave **Enabled** on, leave **Web Access** off, and write a prompt.
3. **Save** → `POST /tasks` with `create_only: true`.
   - Backend creates `Tasks/Morning digest/` and writes `TaskConfig.json` +
     `TaskPrompt.txt`.
   - Response `{ created: true }`; the UI selects the task and refreshes the list.
4. Selecting the task later → `GET /tasks/Morning digest` loads config + prompt
   back into the form, and the **Task Log** panel calls
   `GET /tasks/Morning digest/logs`.
5. Expanding a date then a run → `GET /tasks/Morning digest/logs/2026-09-10/34`
   returns that run's lines for display.

---

## 9. File reference

| Concern                         | File                                             |
| ------------------------------- | ------------------------------------------------ |
| Config, allowed domain, root    | `NumbatAPI/app/config.py`                         |
| Pydantic models (contract)      | `NumbatAPI/app/models.py`                         |
| Task/log business logic         | `NumbatAPI/app/services.py`                       |
| HTTP endpoints                  | `NumbatAPI/app/main.py`                           |
| Task service (HTTP + mock)      | `NumbatUI/src/services/taskService.ts`            |
| Log service (HTTP + mock)       | `NumbatUI/src/services/logService.ts`             |
| Create/Edit task screen         | `NumbatUI/src/screens/TasksScreen.tsx`            |
| Log tree panel                  | `NumbatUI/src/components/TaskLogPanel.tsx`         |
| Client-side validation          | `NumbatUI/src/utils/validation.ts`                |
| API base URL + feature flags    | `NumbatUI/src/config/index.ts`                    |

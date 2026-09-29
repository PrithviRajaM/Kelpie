# Kelpie — Unified execute_task + Action Plan Orchestration

Progress file for a large, multi-session change. If this work is re-executed in a
different session, read this file first to see what is done and what remains.

## Goal (from the user)

Unify the two `execute_task` methods in `Tasks/task_runner.py` into a single
primary method `execute_task(task, config_name: str, state: dict)` that builds
and starts executing a **step-by-step action plan** for a task. The local AI
model (Ollama) cannot reach the internet, so web content is fetched
asynchronously by Swagman via RabbitMQ, and the AI resumes afterwards.

### Required behaviour

1. `execute_task(task, config_name, state)` is the single primary method.
   The functionality below is inserted **after `session_counter` is derived**
   (the old "line 470" spot).
2. On task start, check if `web_extract` is enabled in the task config.
   - If enabled, create a message containing all URLs listed in `web_urls`
     (task config) and publish web-extract jobs.
3. Create a step-by-step **action plan** first, stored in the InProgress session
   folder as `action_plan.json`. Universal step format:
   - `step_id`
   - `action_owner` (e.g. `Swagman` to extract web content, `Ollama` to engage
     the local AI model)
   - `action_description` (what the step does)
   - `status` (`not started`, `in progress`, `completed`, `failed`)
4. The action plan may be modified as steps progress, but **only `not started`
   steps may be modified**. A dedicated Python module under `Tasks/` owns all
   action-plan create/modify logic (create first, then update status / add /
   modify steps).
5. New message queue `local_AI`: on a message (task identifier + step id), a
   consumer looks into task progress and resumes the local AI model action. It
   can complete the task or request more web content / resources by posting to
   the respective queues.
6. New message queue `fallback`: receives a message when other queue owners
   finish their steps. It receives step action status to update the action
   plan, then posts messages to the respective action owner's queue based on
   the next step. When it initiates an action (posts to an owner's queue), it
   sets that step's status to `in progress`.
7. If web content is to be extracted for the AI to act on, step 1 is Swagman
   (web extract) and step 2 is the local AI model.
8. `action_detail.txt` is created once the action plan is prepared. It contains
   the task prompt, then each owner details its outcome under a `Step ID`
   heading. E.g. web-extract step records each URL and its downloaded filename;
   the local AI step records what it did so a future AI step understands
   progress and resource locations.

### Constraints / environment
- Windows, PowerShell. Python project. RabbitMQ via `pika`. Ollama local.
- Messaging settings live in `Messaging/queue.config` (connection + per-queue).
- Publisher: `Messaging/queue_publisher.py` (generic `publish_message` +
  `publish_web_extract`). Consumer pattern: `Swagman/queue_consumer.py`.
- InProgress session folder: `<task_dir>/InProgress/<session_counter>`.
- Existing config field per TASKS.md is `web_access`; the user now refers to
  `web_extract` (bool) + `web_urls` (list). Support the new keys; keep
  `web_access` as a fallback for the enable flag.

## Design decisions
- New module `Tasks/action_plan.py`: sole owner of action_plan.json + action_detail.txt.
- New module `Tasks/action_detail.py` folded into action_plan.py (single owner) OR kept together — decided: keep both concerns in `Tasks/action_plan.py`.
- Add `local_AI` and `fallback` queues to `Messaging/queue.config` and helper
  publishers in `queue_publisher.py`.
- New consumer `Tasks/local_ai_consumer.py` for the `local_AI` queue.
- New consumer `Tasks/fallback_consumer.py` (orchestrator) for the `fallback` queue.
- Swagman consumer, on completion, publishes a step-status message to `fallback`.
- Ollama/local-AI consumer, on completion, publishes a step-status message to `fallback`.

## Task checklist — ALL COMPLETE
- [x] 1. Add `local_AI` and `fallback` queue entries to `Messaging/queue.config`.
- [x] 2. Add helper publishers (`publish_local_ai`, `publish_fallback`) + config keys in `queue_publisher.py` / `Messaging/__init__.py`.
- [x] 3. Create `Tasks/action_plan.py` (create/modify action_plan.json, action_detail.txt; enforce "only not-started steps modifiable").
- [x] 4. Unify `execute_task` in `task_runner.py`: single `execute_task(task, config_name, state)`; after session_counter, build plan + kick off first step.
- [x] 5. Create `Tasks/fallback_consumer.py` (orchestrator: update plan from status, advance to next step, set in-progress, post to owner queue).
- [x] 6. Create `Tasks/local_ai_consumer.py` (resume AI on step; record outcome; post status to fallback; may request more web content).
- [x] 7. Update Swagman consumer to record web-extract outcome into action_detail.txt and post step status to `fallback`.
- [x] 8. Verify: python import/compile checks on all changed modules.

## Runtime wiring (how to run the new pieces)
The orchestration runs across three long-lived consumer processes plus the
existing scheduler (`main.py` -> `run_all_tasks` -> `execute_task`):

    py -3 Swagman/queue_consumer.py        # Swagman/WebExtract  (already existed)
    py -3 Tasks/local_ai_consumer.py       # Ollama/LocalAI      (new)
    py -3 Tasks/fallback_consumer.py       # Kelpie/Fallback     (new orchestrator)

Flow: execute_task builds action_plan.json + action_detail.txt and publishes the
first step. Swagman finishes -> posts {task_identifier, step_id, status} to
fallback -> fallback marks next step in progress and publishes to Ollama/LocalAI
-> local_ai_consumer runs Ollama, records outcome, posts status to fallback ->
plan complete.

## Notes / status log
- (session start) Read task_runner, web_extract, queue_consumer, queue_publisher,
  ollama_client, swagman, config, main, logger, queue.config, TASKS.md. Architecture understood.
- (done) All 8 tasks implemented and verified: py_compile exit 0 for every module;
  runtime imports OK (pika + ollama installed; no circular imports); action_plan
  logic test PASS (step ordering, in-progress immutability, not-started editability,
  multi-URL web-extract completion, plan completion, add_step clamping, detail
  seeding/appending).

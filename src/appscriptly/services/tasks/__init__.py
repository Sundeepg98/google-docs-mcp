"""Google Tasks service (4th new service after Sheets + Slides).

Mirrors the per-service folder layout proven by:

  * Phase A (PR #94)        — services/docs/
  * Phase B (PR #96)        — services/drive/{api,tools}
  * Phase C (PR #109)       — services/gas_deploy/
  * Gap #7 (PR #113)        — services/admin/ (closes ISP asymmetry)
  * v2.3.0 (PR #117)        — services/drive/sharing.py (1st bolt-on)
  * v2.3.1 (PR #119)        — services/sheets/                (2nd service)
  * v2.3.2                  — services/slides/                (3rd service)
  * this PR                 — services/tasks/                 ← here

Layout:

    services/tasks/
    ├── __init__.py         — this file
    ├── api.py              — Tasks REST wrapper (tasklists + tasks CRUD)
    ├── tools.py            — @workspace_tool decorators (registered via
                              server.py's auto-discovery walk)
    └── _expected_tools.py  — the decentralized tool-surface witness

**Scope.** The Tasks tools require the SENSITIVE (not RESTRICTED) scope
``https://www.googleapis.com/auth/tasks``. It is absent from Google's
closed restricted-scope list (Gmail / Drive / Fit / Chat / Data
Portability / Photos / Health), so adding it keeps the app CASA-free —
sensitive-scope verification only, no third-party security assessment.
The scope is declared ONCE in the single source
(``auth.WORKSPACE_SCOPES``) per the #187 derivation, so it flows into
both ``auth.SCOPES`` (stdio) and ``oauth_google.GOOGLE_API_SCOPES``
(connector) with no twin-list edit.

**Tools** (7 — tasklist + task CRUD over the Tasks API v1):

    gtasks_list_tasklists   — list the user's task lists
    gtasks_create_tasklist  — create a new task list
    gtasks_list_tasks       — list tasks in a list (completed/hidden opts)
    gtasks_create_task      — create a task (title/notes/due/parent)
    gtasks_update_task      — patch a task's fields
    gtasks_complete_task    — mark a task completed (status convenience)
    gtasks_delete_task      — delete a task from a list

The Tasks REST surface is the simple ``list / insert / patch / delete``
resource shape (no batchUpdate tagged-union), so the api layer is a
thin wrapper around ``service.tasklists()`` / ``service.tasks()`` —
closer to the Drive ``files()`` shape than the Docs / Slides
``batchUpdate`` plumbing.

NOTE — DEPLOY-TIME PREREQUISITE: the **Google Tasks API** must be
enabled in the GCP project (APIs & Services → Enable APIs → "Tasks
API") before these tools work against the live app. ``DEPLOY_ENABLED``
is off, so merging this does not deploy; the API-enable step is part of
the eventual deliberate deploy, not this PR.
"""

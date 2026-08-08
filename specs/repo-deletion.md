# Repository deletion

## Overview

Panopticon exposes repository creation and editing through the task service and dashboard, but it
has no supported deletion path. SQLite does not enforce the declared `task.repo_id` foreign key on
the service's current connections, so deleting a referenced repository directly would silently
leave corrupt task rows behind.

This feature deliberately supports only deletion of repositories that have never been referenced
by a task. It does not add a force-delete or cascade mode: terminal tasks and their history remain
valuable records, and destroying them is outside the scope of repository cleanup.

## Requirements

### 1: Service safety

1. `DELETE /repos/{repo_id}` MUST delete an existing repository only when zero persisted tasks
   reference its id.

2. A deletion request for a repository referenced by one or more persisted tasks MUST be refused
   with HTTP 409, an unambiguous message stating the exact referencing-task count, and no change to
   any repository, task, or task history.

3. With SQLite foreign-key enforcement disabled, the deletion endpoint MUST either preserve a
   referenced repository together with every referencing task or remove an unreferenced
   repository while leaving no task row whose `repo_id` equals the removed id.

4. A deletion request for an unknown repository id MUST return HTTP 404 without changing any
   repository or task.

### 2: Operator confirmation

1. The repository edit page MUST expose a delete action that is absent from the new-repository
   page.

2. Selecting the edit page's delete action MUST open an irreversible-action confirmation that
   names the repository and instructs the operator to type that exact name.

3. The confirmation MUST issue the deletion only when the entered text exactly equals the
   repository name displayed when the confirmation opened and the operator presses Enter.

4. Submitting a nonmatching name MUST leave the repository undeleted and keep the confirmation
   open with a mismatch error.

5. A service refusal MUST keep the repository edit flow open and display the service's
   referencing-task count to the operator.

6. After a successful confirmed deletion, the repositories page MUST refresh without the deleted
   repository.

## Deliberately deferred integrity recommendation

The task service should separately evaluate enabling `PRAGMA foreign_keys = ON` for every SQLite
connection. That could expose other ordering or migration defects and therefore needs its own
change and regression assessment. Repository deletion must remain safe without relying on that
pragma; this feature does not silently change the connection-wide setting.

## Non-goals

- Force-deleting a referenced repository.
- Cascading deletion into tasks, history, responsibilities, session data, artifacts, or other
  task-owned records.
- Changing SQLite connection pragmas.

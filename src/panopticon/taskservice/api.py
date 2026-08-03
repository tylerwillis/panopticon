"""The task service REST API (FastAPI).

The dashboard, the runner, and in-container skills are clients of this API. In-container
agents also reach task operations/artifacts over **MCP**: ``create_app`` mounts the MCP
streamable-HTTP app (see :mod:`panopticon.taskservice.mcp`) at ``/mcp``, so the same control
plane serves REST and MCP. ``create_app`` builds an app around an injected
:class:`~panopticon.taskservice.service.TaskService`, so tests can wire a deterministic one.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette._utils import get_route_path
from starlette.types import Message, Receive, Scope, Send

from panopticon.core.artifacts import ArtifactError
from panopticon.core.models import Actor, LifecyclePhase, Repo, Status, Task, WakeStatus
from panopticon.core.store import AlreadyExists, NotFound, StoreError
from panopticon.core.workflow import IllegalTransition, InvalidWorkflow, ResponsibilitiesNotMet
from panopticon.taskservice.service import (
    AlreadyClaimed,
    NotAuthorized,
    NotReady,
    TaskService,
    UnknownWorkflow,
)

#: How often the held ``/live`` stream emits a keepalive byte. This does **not** govern how fast
#: death is noticed — disconnect is event-driven (Starlette cancels the stream the instant the
#: client drops, so the registration is removed immediately). The keepalive only keeps idle
#: proxies from closing the connection and gives the container a tick to notice a clean stop.
LIVENESS_KEEPALIVE_SECONDS = 5.0
_log = logging.getLogger(__name__)


def _redact_stream_chunk(
    data: bytes,
    *,
    configured: tuple[bytes, ...],
    pending: bytes = b"",
    more_body: bool,
) -> tuple[bytes, bytes]:
    """Redact configured values while retaining a possible cross-chunk token prefix."""
    data = pending + data
    if not configured:
        return data, b""
    pattern = re.compile(b"|".join(re.escape(token) for token in configured))
    held = 0
    if more_body:
        held = max(
            (
                size
                for token in configured
                for size in range(1, min(len(data), len(token) - 1) + 1)
                if data.endswith(token[:size])
            ),
            default=0,
        )
    safe_end = len(data) - held
    output = bytearray()
    consumed = 0
    for matched in pattern.finditer(data):
        if matched.start() >= safe_end:
            break
        output.extend(data[consumed : matched.start()])
        output.extend(b"*" * (matched.end() - matched.start()))
        consumed = matched.end()
    if consumed < safe_end:
        output.extend(data[consumed:safe_end])
        consumed = safe_end
    return bytes(output), data[consumed:]


# -- wire schemas -------------------------------------------------------------------


# ``*Out`` models read straight off the domain objects (`model_validate`): their fields match
# the domain attribute names, so `from_attributes=True` does the conversion — incl. nested
# Task -> History -> Responsibility — with no hand-written copying.


class ResponsibilityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    description: str
    status: Status
    comment: str | None = None


class HistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    at: str
    from_state: str | None
    to_state: str
    trigger: str | None = None
    note: str | None = None
    responsibilities: list[ResponsibilityOut] = []
    wake_status: WakeStatus


class MigrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_runner: str
    destination_runner: str
    workspace_disposition: str
    workspace_method: str = "archive"
    session_history_disposition: str
    discarded_changes: list[str] = []
    discard_authorized_by: str | None = None
    session_history_changed_by: str | None = None
    session_history_was_requested: bool = False


class TaskSummaryOut(BaseModel):
    """Task fields from the tasks table only — no history. Returned by ``GET /tasks``."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    repo_id: str
    workflow: str
    state: str
    turn: Actor
    blocked: bool
    attention: bool
    memo: str | None
    initial_prompt: str | None
    slug: str | None
    url: str | None
    snoozed_until: str | None
    branch: str | None
    clone: str | None
    claimed_by: str | None
    provisioned_by: str | None = None
    workspace_verified_by: str | None = None
    migration: MigrationOut | None = None
    tokens_used: int | None
    token_estimate: int | None
    starting_model: str | None = None
    harness: str | None = None
    governor_task_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    depends_on_task_ids: list[str] = []
    provisioned: bool
    terminal: bool = False
    container_status: str = "–"
    lifecycle_detail: str | None = None
    runner_host: str | None = (
        None  # hostname the claiming runner registered with (M5: remote attach)
    )


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    repo_id: str
    workflow: str
    state: str
    turn: Actor
    blocked: bool
    attention: bool
    memo: (
        str | None
    )  # a brief one-line reminder of what the task is, collected at creation (shown in the summary)
    initial_prompt: str | None  # optional text prefilled into Claude's input box on first spawn
    slug: str | None
    url: str | None  # an optional external URL (PR, issue, …); the dashboard's `p` hotkey opens it
    snoozed_until: str | None  # operator-recorded mute deadline; only display code compares time
    branch: str | None
    clone: str | None
    claimed_by: str | None  # the runner that owns this task (the spawn gate), or None
    provisioned_by: str | None = None
    workspace_verified_by: str | None = None
    migration: MigrationOut | None = None
    tokens_used: int | None  # cost-weighted input-equivalent tokens used (None until reported)
    token_estimate: (
        int | None
    )  # the agent's forecast of total tokens (set in planning; None until then)
    starting_model: str | None = (
        None  # the model seeded at creation from the workflow's default_model
    )
    harness: str | None = (
        None  # concrete for new tasks; None only on legacy rows (resolves to the app default)
    )
    governor_task_id: str | None = (
        None  # the task that oversees this one, or None for ungoverned tasks
    )
    created_at: str | None = (
        None  # ISO-8601 timestamp when the task was created; set once, never changed
    )
    updated_at: str | None = (
        None  # ISO-8601 timestamp of the last mutation, stamped by the task service
    )
    depends_on_task_ids: list[
        str
    ] = []  # task IDs that must complete before work on this task should begin
    provisioned: bool  # computed (Task.provisioned): branch + clone recorded
    terminal: bool = False  # workflow-derived terminality for deterministic relationship consumers
    #: The composed container-lifecycle status the dashboard displays (the task service folds the
    #: session service's reported phase with registration presence + runner liveness). Not a domain
    #: field — attached on serialization (see ``_task_out``), defaulted for the bare-validate path.
    container_status: str = "–"
    lifecycle_detail: str | None = (
        None  # the reported phase's detail, e.g. the build layers / failure
    )
    runner_host: str | None = (
        None  # hostname the claiming runner registered with (M5: remote attach)
    )
    history: list[HistoryOut]


class RunnerOut(BaseModel):
    id: str  # runner_id (the runner's own identifier, e.g. "local" or a hostname alias)
    host: str | None  # hostname the runner registered with; None if not provided


class RepoIn(BaseModel):
    id: str
    name: str
    git_url: str
    default_base: str = "main"
    env_file: str | None = None
    image_layer_file: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    hook_file: str | None = None
    enabled_workflows: list[str] = Field(default_factory=list)
    disabled_workflows: list[str] = Field(default_factory=list)
    default_harness: str | None = None  # the harness this repo's tasks run by default
    default_model: str | None = None  # opaque model[:effort] for that harness
    credential_dir: str | None = None  # name of a shared credential dir under the secrets dir


class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    git_url: str
    default_base: str
    env_file: str | None = None
    image_layer_file: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    hook_file: str | None = None
    enabled_workflows: list[str] = Field(default_factory=list)
    disabled_workflows: list[str] = Field(default_factory=list)
    default_harness: str | None = None
    default_model: str | None = None
    credential_dir: str | None = None


class RepoPatchIn(BaseModel):
    # All fields optional: a PATCH carries only what changes. ``model_dump(exclude_unset=True)``
    # then tells "field omitted" from "field explicitly set to null", so a partial update can't
    # null out a field the operator didn't touch. ``id`` is the key — present here only so a
    # mismatched body is rejected (the path id is authoritative).
    id: str | None = None
    name: str | None = None
    git_url: str | None = None
    default_base: str | None = None
    env_file: str | None = None
    image_layer_file: str | None = None
    capabilities: dict[str, Any] | None = None
    hook_file: str | None = None
    enabled_workflows: list[str] | None = None
    disabled_workflows: list[str] | None = None
    default_harness: str | None = None
    default_model: str | None = None
    credential_dir: str | None = None


class WorkflowInfo(BaseModel):
    name: str
    when_to_use: str
    opt_in: bool


class WorkflowEditorInfo(BaseModel):
    name: str
    when_to_use: str
    path: str
    built_in: bool


class CreateTaskIn(BaseModel):
    repo_id: str
    workflow: str
    memo: str | None = None
    governor_task_id: str | None = None
    initial_prompt: str | None = None
    harness: str | None = None  # agent-CLI harness for the task's container (None = claude)
    starting_model: str | None = None  # harness-scoped model name (None = the harness's default)
    artifacts: dict[str, str] | None = None
    depends_on_task_ids: list[str] = []


class DependenciesIn(BaseModel):
    dep_ids: list[str]


class GovernorIn(BaseModel):
    governor_task_id: str | None


class ResponsibilityIn(BaseModel):
    key: str
    status: Status
    comment: str | None = None


class StageEntryWakeIn(BaseModel):
    status: WakeStatus


class TransitionIn(BaseModel):
    to_state: str
    trigger: str | None = None
    note: str | None = None


class SlugIn(BaseModel):
    slug: str


class UrlIn(BaseModel):
    url: str


class TokensUsedIn(BaseModel):
    tokens_used: int


class TokenEstimateIn(BaseModel):
    token_estimate: int


class StateIn(BaseModel):
    state: str


class ProvisioningIn(BaseModel):
    branch: str
    clone: str
    runner_id: str
    workspace_verified: bool


class MigrationIn(BaseModel):
    source_runner: str
    destination_runner: str
    workspace_disposition: str
    workspace_method: str = "archive"
    session_history_disposition: str
    discarded_changes: list[str] = []
    discard_authorized_by: str | None = None


class SkillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str
    instructions: str


class TurnIn(BaseModel):
    turn: Actor


class BlockedIn(BaseModel):
    blocked: bool


class AttentionIn(BaseModel):
    attention: bool


class SnoozeIn(BaseModel):
    until: str | None

    @field_validator("until")
    @classmethod
    def validate_until(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("until must be an ISO-8601 timestamp") from exc
        if len(value) <= 10:
            raise ValueError("until must include a time")
        return value


class ClaimIn(BaseModel):
    runner_id: str


class RegisterIn(BaseModel):
    container_id: str
    runner_id: str | None = None


class LifecycleIn(BaseModel):
    runner_id: str
    phase: LifecyclePhase
    detail: str | None = None


class RegistrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    container_id: str
    runner_id: str | None
    registered_at: str


# -- block-until-change feed --------------------------------------------------------

#: The header carrying the store's change-feed version on every ``GET /tasks`` response — the
#: cursor a client echoes back as ``?since=`` to long-poll for the next change.
TASKS_VERSION_HEADER = "X-Tasks-Version"

#: Ceiling on a single long-poll's hold time (seconds). A client asking to wait longer just gets
#: a snapshot at the cap and re-requests — keeps a connection from parking indefinitely.
MAX_WAIT_SECONDS = 60.0


class ChangeFeed:
    """An async broadcast over the store's change counter — the HTTP side of block-until-change.

    The store bumps its synchronous :meth:`~panopticon.core.store.Store.version` on every task
    mutation and calls :meth:`notify` (a subscribed listener). :meth:`wait` parks a request on an
    :class:`asyncio.Event` until the next ``notify`` (or a timeout), then returns the current
    version. The asyncio lives here, not in ``core`` — the store stays clock-free and push-free.

    All mutations arrive over HTTP/MCP and run on the event loop, so ``notify`` (which sets the
    event) and the waiters share one thread; no locking is needed.
    """

    def __init__(self, version: Callable[[], int]) -> None:
        self._version = version
        self._changed = asyncio.Event()

    def notify(self) -> None:
        """Wake every parked waiter, then arm a fresh event for the next round (broadcast)."""
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait(self, since: int, timeout: float) -> int:
        """Return the current version once it differs from ``since``, or after ``timeout`` seconds.

        Returns immediately when the version already moved (any difference — including a service
        restart that reset the counter — counts, so a stale cursor never blocks forever).
        """
        if self._version() != since:
            return self._version()
        changed = self._changed  # capture before awaiting: notify() swaps in a fresh event
        with suppress(TimeoutError):
            await asyncio.wait_for(changed.wait(), timeout)
        return self._version()


def create_app(
    service: TaskService,
    *,
    auth_file: str | None = None,
    auth_mode: str | None = None,
    secrets_dir: str | Path | None = None,
) -> FastAPI:
    operator_token = os.environ.get("PANOPTICON_OPERATOR_TOKEN")
    # MCP over streamable HTTP, mounted at /mcp on the same control plane (operations=tools,
    # artifacts=resources). Its path is set to "/" so the mount point *is* the endpoint (/mcp).
    # The session manager must run for the app's lifetime, so its context is driven by the
    # parent FastAPI lifespan (a mounted sub-app's own lifespan isn't run by the parent).
    # Imported here, not at module scope: mcp.py imports our ``*Out`` schemas (would cycle).
    from panopticon.taskservice.auth import load_tokens
    from panopticon.taskservice.mcp import build_mcp_server

    mode = auth_mode or ("enforced" if auth_file else "disabled")
    if mode not in {"disabled", "permissive", "enforced"}:
        raise ValueError("authentication mode must be disabled, permissive, or enforced")
    if mode in {"permissive", "enforced"} and auth_file is None:
        raise ValueError(f"authentication credential file is required in {mode} mode")
    tokens = load_tokens(auth_file, secrets_dir=secrets_dir) if auth_file is not None else None

    mcp = build_mcp_server(service)
    mcp.settings.streamable_http_path = "/"
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await service.init()
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="panopticon task service", version="0.0.3", lifespan=lifespan)

    generic_auth_failure = {"detail": "authentication required"}
    permissive_unauthenticated_total = 0

    def redact_configured_tokens(value: Any) -> Any:
        if tokens is None:
            return value
        configured = sorted((*tokens.read, *tokens.write), key=len, reverse=True)
        if isinstance(value, str):
            for token in configured:
                value = value.replace(token, "[redacted]")
            return value
        if isinstance(value, list):
            return [redact_configured_tokens(item) for item in value]
        if isinstance(value, dict):
            return {key: redact_configured_tokens(item) for key, item in value.items()}
        return value

    async def redacted_mcp_app(scope: Scope, receive: Receive, send: Send) -> None:
        """Redact mounted MCP responses, which bypass the parent exception handlers."""
        configured = (
            tuple(
                sorted(
                    (token.encode() for token in (*tokens.read, *tokens.write)),
                    key=len,
                    reverse=True,
                )
            )
            if tokens
            else ()
        )
        pending = b""

        async def send_redacted(message: Message) -> None:
            nonlocal pending
            if message["type"] == "http.response.body":
                more_body = bool(message.get("more_body", False))
                output, pending = _redact_stream_chunk(
                    message.get("body", b""),
                    configured=configured,
                    pending=pending,
                    more_body=more_body,
                )
                message = {
                    **message,
                    "body": output,
                }
            await send(message)

        await mcp_app(scope, receive, send_redacted)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": redact_configured_tokens(jsonable_encoder(exc.errors()))},
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": redact_configured_tokens(exc.detail)},
            headers=exc.headers,
        )

    @app.middleware("http")
    async def authenticate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        nonlocal permissive_unauthenticated_total
        route_path = get_route_path(request.scope)
        if not route_path.startswith("/"):
            route_path = f"/{route_path}"
        if mode == "disabled" or (request.method == "GET" and route_path == "/healthz"):
            return await call_next(request)
        authorization = request.headers.get("authorization")
        if mode == "permissive" and authorization is None:
            client = request.client.host if request.client is not None else "unknown"
            permissive_unauthenticated_total += 1
            if permissive_unauthenticated_total & (permissive_unauthenticated_total - 1) == 0:
                _log.warning(
                    "permissive authentication accepted headerless request: "
                    "method=%s route=%s client=%s count=%d",
                    redact_configured_tokens(request.method),
                    redact_configured_tokens(route_path),
                    redact_configured_tokens(client),
                    permissive_unauthenticated_total,
                )
            return await call_next(request)
        presented = ""
        if authorization is not None and authorization.startswith("Bearer "):
            candidate = authorization[7:]
            if candidate and " " not in candidate:
                presented = candidate
        assert tokens is not None
        presented_bytes = presented.encode()
        write = any(hmac.compare_digest(presented_bytes, token.encode()) for token in tokens.write)
        read = any(hmac.compare_digest(presented_bytes, token.encode()) for token in tokens.read)
        path_parts = route_path.strip("/").split("/")
        liveness = (
            len(path_parts) == 3
            and path_parts[0] in {"tasks", "runners"}
            and path_parts[2] == "live"
        )
        mutating = (
            request.method not in {"GET", "HEAD"} or route_path.startswith("/mcp") or liveness
        )
        if not write and (mutating or not read):
            return JSONResponse(
                status_code=401,
                content=generic_auth_failure,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    # The block-until-change feed: a store mutation bumps the version + wakes parked GET /tasks
    # long-polls (the seam the daemons/dashboard migrate onto, replacing their interval re-polls).
    feed = ChangeFeed(service.tasks_version)
    service.subscribe_to_changes(feed.notify)

    async def _task_out(task: Task) -> TaskOut:
        """Serialize a task **with** its computed container-lifecycle fields. These aren't domain
        attributes (the status is composed from ephemeral runner-reported phase + registrations +
        runner liveness), so they're attached here rather than read off the Task by ``model_validate``.
        Every task-returning handler routes through this so the dashboard always sees them."""
        out = TaskOut.model_validate(task)
        out.terminal = service.task_is_terminal(task)
        out.container_status = service.container_status(
            task, dependencies_blocking=await service.dependencies_blocking(task)
        ).value
        lifecycle = service.lifecycle(task.id)
        out.lifecycle_detail = lifecycle.detail if lifecycle is not None else None
        if task.claimed_by is not None:
            out.runner_host = service.runner_host(task.claimed_by)
        return out

    def _task_summary_out(task: Task, tasks_by_id: dict[str, Task]) -> TaskSummaryOut:
        """Serialize a task to the cheap summary shape (no history), with computed status fields."""
        out = TaskSummaryOut.model_validate(task)
        out.terminal = service.task_is_terminal(task)
        out.container_status = service.container_status(
            task,
            dependencies_blocking=service.dependencies_blocking_in_snapshot(task, tasks_by_id),
        ).value
        lifecycle = service.lifecycle(task.id)
        out.lifecycle_detail = lifecycle.detail if lifecycle is not None else None
        if task.claimed_by is not None:
            out.runner_host = service.runner_host(task.claimed_by)
        return out

    # -- error mapping: domain exceptions -> HTTP status --------------------------

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": redact_configured_tokens(str(exc))})

    @app.exception_handler(AlreadyExists)
    async def _conflict(_: Request, exc: AlreadyExists) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": redact_configured_tokens(str(exc))})

    @app.exception_handler(IllegalTransition)
    async def _illegal(_: Request, exc: IllegalTransition) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": redact_configured_tokens(str(exc))})

    @app.exception_handler(ResponsibilitiesNotMet)
    async def _responsibilities(_: Request, exc: ResponsibilitiesNotMet) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": redact_configured_tokens(str(exc))})

    @app.exception_handler(NotAuthorized)
    async def _not_authorized(_: Request, exc: NotAuthorized) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": redact_configured_tokens(str(exc))})

    @app.exception_handler(UnknownWorkflow)
    async def _unknown_wf(_: Request, exc: UnknownWorkflow) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": redact_configured_tokens(str(exc))})

    @app.exception_handler(InvalidWorkflow)
    async def _invalid_wf(_: Request, exc: InvalidWorkflow) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": redact_configured_tokens(str(exc))})

    @app.exception_handler(ArtifactError)
    async def _artifact(_: Request, exc: ArtifactError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": redact_configured_tokens(str(exc))})

    @app.exception_handler(StoreError)
    async def _store_error(_: Request, exc: StoreError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": redact_configured_tokens(str(exc))})

    # -- health & discovery -------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/workflows")
    async def list_workflows() -> list[WorkflowInfo]:
        return await service.list_workflow_infos()  # type: ignore[return-value]

    @app.get("/workflow-files")
    async def list_workflow_files() -> list[WorkflowEditorInfo]:
        """Registered workflow sources for the dashboard's editor-first workflow UI."""
        return await service.list_workflow_editor_infos()  # type: ignore[return-value]

    @app.get("/workflows/{name}/image-layer")
    async def workflow_image_layer(name: str) -> dict[str, str]:
        """The workflow's Dockerfile layer (ADR 0005); the runner composes it onto the base."""
        return {"layer": await service.workflow_image_layer(name)}

    @app.get("/workflows/{name}/execution")
    async def workflow_execution(name: str) -> dict[str, Any]:
        """How the runner executes this workflow's tasks: ``runner_type`` (``"docker"``/``"shell"``),
        the shell ``script``, ``clone_repo``, and a shell ``workdir`` override."""
        return await service.workflow_execution(name)

    # -- repos --------------------------------------------------------------------

    @app.post("/repos", status_code=201)
    async def create_repo(body: RepoIn) -> RepoOut:
        try:
            repo = await service.create_repo(Repo(**body.model_dump()))
        except ValueError as exc:  # e.g. env_file does not exist
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RepoOut.model_validate(repo)

    @app.get("/repos")
    async def list_repos() -> list[RepoOut]:
        return [RepoOut.model_validate(r) for r in await service.list_repos()]

    @app.get("/repos/{repo_id}")
    async def get_repo(repo_id: str) -> RepoOut:
        return RepoOut.model_validate(await service.get_repo(repo_id))

    @app.get("/repos/{repo_id}/workflows")
    async def list_repo_workflows(repo_id: str) -> list[dict[str, str | bool]]:
        """Workflows available for this repo, filtered by its ``enabled_workflows`` /
        ``disabled_workflows`` preferences and each workflow's ``opt_in`` flag."""
        return await service.list_workflow_infos_for_repo(repo_id)

    @app.get("/repos/{repo_id}/image-layer")
    async def repo_image_layer(repo_id: str) -> dict[str, str]:
        """The repo's Dockerfile layer (ADR 0005), read from its ``image_layer_file`` reference;
        the runner composes it onto base → workflow. Empty when the repo declares none."""
        return {"layer": await service.repo_image_layer(repo_id)}

    @app.patch("/repos/{repo_id}")
    async def update_repo(repo_id: str, body: RepoPatchIn) -> RepoOut:
        # exclude_unset → only the fields the caller actually sent; the service merges them
        # onto the stored repo (untouched fields, e.g. image_layer_file/capabilities, are preserved).
        changes = body.model_dump(exclude_unset=True)
        try:
            repo = await service.update_repo(repo_id, changes)
        except ValueError as exc:  # e.g. attempting to change the id
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RepoOut.model_validate(repo)

    # -- tasks --------------------------------------------------------------------

    @app.post("/tasks", status_code=201)
    async def create_task(body: CreateTaskIn) -> TaskOut:
        try:
            return await _task_out(
                await service.create_task(
                    body.repo_id,
                    body.workflow,
                    memo=body.memo,
                    governor_task_id=body.governor_task_id,
                    initial_prompt=body.initial_prompt,
                    harness=body.harness,
                    starting_model=body.starting_model,
                    artifacts=body.artifacts,
                    depends_on_task_ids=body.depends_on_task_ids or None,
                )
            )
        except ValueError as exc:  # e.g. an unknown harness
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/tasks")
    async def list_tasks(
        response: Response,
        wait: float | None = Query(
            default=None,
            ge=0,
            description="Block up to this many seconds for a change past ?since before returning "
            f"(capped at {MAX_WAIT_SECONDS:g}s). Omit for an immediate snapshot.",
        ),
        since: int = Query(
            default=0,
            description="The X-Tasks-Version a client last saw; with ?wait, return once the "
            "version differs from it (block-until-change).",
        ),
        terminal: bool | None = Query(
            default=None,
            description="Filter to terminal tasks only (true) or active tasks only (false). "
            "Omit to return all tasks.",
        ),
    ) -> list[TaskSummaryOut]:
        # Every response carries the current version in X-Tasks-Version so a client can echo it
        # back as ?since=. With ?wait the request parks until the version moves past ?since (or
        # the cap elapses); without it, it's an immediate snapshot — today's behaviour.
        if wait is not None:
            version = await feed.wait(since=since, timeout=min(wait, MAX_WAIT_SECONDS))
            all_tasks = await service.list_tasks_summary()
        else:
            # Read version and snapshot in a single thread call so no event-loop yield can
            # interleave a mutation between them — preserving the original atomicity invariant.
            version, all_tasks = await service._tasks_snapshot()
        tasks_by_id = {task.id: task for task in all_tasks}
        tasks_raw = (
            all_tasks
            if terminal is None
            else [task for task in all_tasks if service.task_is_terminal(task) == terminal]
        )
        tasks = [_task_summary_out(task, tasks_by_id) for task in tasks_raw]
        response.headers[TASKS_VERSION_HEADER] = str(version)
        return tasks

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> TaskOut:
        return await _task_out(await service.get_task(task_id))

    @app.get("/tasks/{task_id}/transitions")
    async def list_transitions(task_id: str) -> list[str]:
        return await service.legal_transitions(task_id)

    @app.get("/tasks/{task_id}/operations")
    async def list_operations(task_id: str) -> dict[str, str]:
        return await service.operations(task_id)

    @app.post("/tasks/{task_id}/operations/{operation}")
    async def apply_operation(task_id: str, operation: str) -> TaskOut:
        return await _task_out(await service.apply_operation(task_id, operation))

    @app.get("/tasks/{task_id}/states")
    async def list_states(task_id: str) -> list[str]:
        return await service.workflow_states(task_id)

    @app.get("/tasks/{task_id}/skills")
    async def list_skills(task_id: str) -> list[SkillOut]:
        return [SkillOut.model_validate(s) for s in await service.skills(task_id)]

    @app.get("/tasks/{task_id}/briefing")
    async def get_briefing(task_id: str) -> dict[str, str]:
        """The agent's current-phase briefing (the container's user-prompt hook emits it)."""
        return {"briefing": await service.briefing(task_id)}

    @app.get("/tasks/{task_id}/history/{entry_index}/wake")
    async def get_stage_entry_wake(task_id: str, entry_index: int) -> dict[str, str]:
        try:
            briefing = await service.stage_entry_briefing(task_id, entry_index)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"briefing": briefing}

    @app.put("/tasks/{task_id}/history/{entry_index}/wake")
    async def record_stage_entry_wake(
        task_id: str, entry_index: int, body: StageEntryWakeIn
    ) -> TaskOut:
        try:
            task = await service.record_stage_entry_wake(task_id, entry_index, body.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await _task_out(task)

    @app.get("/tasks/{task_id}/workflow-overview")
    async def get_workflow_overview(task_id: str) -> dict[str, str]:
        """The whole-workflow map (the agent launcher puts it in claude's system prompt)."""
        return {"overview": await service.workflow_overview(task_id)}

    @app.put("/tasks/{task_id}/state")
    async def set_state(task_id: str, body: StateIn) -> TaskOut:
        return await _task_out(await service.set_state(task_id, body.state))

    @app.post("/tasks/{task_id}/transition")
    async def transition(task_id: str, body: TransitionIn) -> TaskOut:
        return await _task_out(
            await service.request_transition(
                task_id, body.to_state, trigger=body.trigger, note=body.note
            )
        )

    @app.post("/tasks/{task_id}/responsibilities")
    async def resolve_responsibility(task_id: str, body: ResponsibilityIn) -> TaskOut:
        try:
            task = await service.resolve_responsibility(
                task_id, body.key, status=body.status, comment=body.comment
            )
        except ValueError as exc:  # unknown key / PENDING / FAILED without a comment
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await _task_out(task)

    @app.put("/tasks/{task_id}/slug")
    async def set_slug(task_id: str, body: SlugIn) -> TaskOut:
        return await _task_out(await service.set_slug(task_id, body.slug))

    @app.put("/tasks/{task_id}/url")
    async def set_url(task_id: str, body: UrlIn) -> TaskOut:
        return await _task_out(await service.set_url(task_id, body.url))

    @app.put("/tasks/{task_id}/tokens-used")
    async def set_tokens_used(task_id: str, body: TokensUsedIn) -> TaskOut:
        return await _task_out(await service.set_tokens_used(task_id, body.tokens_used))

    @app.put("/tasks/{task_id}/token-estimate")
    async def set_token_estimate(task_id: str, body: TokenEstimateIn) -> TaskOut:
        return await _task_out(await service.set_token_estimate(task_id, body.token_estimate))

    @app.put("/tasks/{task_id}/turn")
    async def set_turn(task_id: str, body: TurnIn) -> TaskOut:
        return await _task_out(await service.set_turn(task_id, body.turn))

    @app.put("/tasks/{task_id}/blocked")
    async def set_blocked(task_id: str, body: BlockedIn) -> TaskOut:
        return await _task_out(await service.set_blocked(task_id, body.blocked))

    @app.put("/tasks/{task_id}/attention")
    async def set_attention(task_id: str, body: AttentionIn) -> TaskOut:
        return await _task_out(await service.set_attention(task_id, body.attention))

    @app.put("/tasks/{task_id}/snooze")
    async def set_snooze(task_id: str, body: SnoozeIn) -> TaskOut:
        return await _task_out(await service.set_snooze(task_id, body.until))

    @app.put("/tasks/{task_id}/governor")
    async def set_governor(task_id: str, body: GovernorIn) -> TaskOut:
        return await _task_out(await service.set_governor(task_id, body.governor_task_id))

    @app.put("/tasks/{task_id}/dependencies")
    async def set_dependencies(task_id: str, body: DependenciesIn) -> TaskOut:
        try:
            task = await service.set_dependencies(task_id, body.dep_ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return await _task_out(task)

    @app.put("/tasks/{task_id}/claim")
    async def claim(task_id: str, body: ClaimIn) -> TaskOut:
        try:  # a runner claims an unclaimed task before spawning its container (ADR 0008)
            task = await service.claim(task_id, body.runner_id)
        except (AlreadyClaimed, NotReady) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await _task_out(task)

    @app.delete("/tasks/{task_id}/claim")
    async def release(task_id: str) -> TaskOut:
        return await _task_out(await service.release(task_id))

    @app.put("/tasks/{task_id}/provisioning")
    async def record_provisioning(task_id: str, body: ProvisioningIn) -> TaskOut:
        try:  # the session service reports the host branch + per-task clone it created (ADR 0011)
            task = await service.record_provisioning(
                task_id,
                body.branch,
                body.clone,
                body.runner_id,
                body.workspace_verified,
            )
        except ValueError as exc:  # slug not set yet
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except NotReady as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await _task_out(task)

    @app.put("/tasks/{task_id}/migration")
    async def record_migration(
        task_id: str,
        body: MigrationIn,
        supplied_token: str | None = Header(None, alias="X-Panopticon-Operator-Token"),
    ) -> TaskOut:
        if operator_token is None:
            raise HTTPException(
                status_code=503, detail="operator migration token is not configured"
            )
        if supplied_token is None or not secrets.compare_digest(
            supplied_token.encode(), operator_token.encode()
        ):
            raise HTTPException(status_code=403, detail="operator authorization required")
        try:
            task = await service.record_migration(
                task_id,
                body.source_runner,
                body.destination_runner,
                body.workspace_disposition,
                body.session_history_disposition,
                body.discarded_changes,
                body.discard_authorized_by,
                body.workspace_method,
            )
        except (ValueError, NotReady) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return await _task_out(task)

    # -- artifacts ----------------------------------------------------------------

    @app.put("/tasks/{task_id}/artifacts/{name}", status_code=204)
    async def put_artifact(task_id: str, name: str, request: Request) -> Response:
        await service.put_artifact(task_id, name, await request.body())
        return Response(status_code=204)

    @app.get("/tasks/{task_id}/artifacts")
    async def list_artifacts(task_id: str) -> list[str]:
        return await service.list_artifacts(task_id)

    @app.get("/tasks/{task_id}/artifacts/{name}")
    async def get_artifact(task_id: str, name: str) -> Response:
        content = await service.get_artifact(task_id, name)
        if content is None:
            raise HTTPException(status_code=404, detail=f"artifact {name!r} not found")
        return Response(content=content, media_type="application/octet-stream")

    # -- liveness -----------------------------------------------------------------

    @app.get("/tasks/{task_id}/live")
    async def live(
        task_id: str, request: Request, container_id: str, runner_id: str | None = None
    ) -> StreamingResponse:
        """The liveness connection: a container holds this stream open for its whole lifetime.

        Registering happens on connect and is removed on disconnect — the open connection *is* the
        signal that the container is alive. When the container dies (clean exit, ``docker stop``,
        ``SIGKILL``/``docker rm --force``, crash) the stream drops and Starlette cancels the body
        generator, so the ``finally`` deregisters **immediately** — no heartbeat, no TTL. A flaky
        network drop reaps the registration too, but the container re-opens this connection (its
        reconnect loop), so a transient blip self-heals into a brief ``down`` flicker.
        """
        await service.get_task(task_id)  # 404 if the task is unknown
        reg = await service.register(task_id, container_id, runner_id)

        async def hold() -> AsyncIterator[bytes]:
            try:
                yield b":ok\n"  # flush headers + confirm liveness is established
                while True:
                    await asyncio.sleep(LIVENESS_KEEPALIVE_SECONDS)
                    yield b":keepalive\n"
            finally:  # client disconnected (Starlette cancels us) or the loop ended — reap now
                await service.deregister(reg.id)

        return StreamingResponse(hold(), media_type="text/event-stream")

    @app.post("/tasks/{task_id}/registrations", status_code=201)
    async def register(task_id: str, body: RegisterIn) -> RegistrationOut:
        return RegistrationOut.model_validate(
            await service.register(task_id, body.container_id, body.runner_id)
        )

    @app.get("/tasks/{task_id}/registrations")
    async def list_registrations(task_id: str) -> list[RegistrationOut]:
        await service.get_task(task_id)  # 404 if the task is unknown
        return [RegistrationOut.model_validate(r) for r in service.registrations(task_id)]

    @app.delete("/registrations/{registration_id}", status_code=204)
    async def deregister(registration_id: str) -> Response:
        await service.deregister(registration_id)
        return Response(status_code=204)

    # -- container lifecycle (the session service reports its spawn progress) ----------
    #
    # The runner pushes its spawn phase here as it claims → prepares → builds → starts a container,
    # so the dashboard can surface the steps to becoming live (and a failure) instead of guessing.
    # Folded into TaskOut.container_status; cleared on claim release/reclaim (see the service).

    @app.put("/tasks/{task_id}/lifecycle")
    async def report_lifecycle(task_id: str, body: LifecycleIn) -> TaskOut:
        await service.report_lifecycle(task_id, body.runner_id, body.phase, body.detail)
        return await _task_out(await service.get_task(task_id))

    @app.delete("/tasks/{task_id}/lifecycle")
    async def clear_lifecycle(task_id: str) -> TaskOut:
        await service.get_task(task_id)  # 404 if the task is unknown
        service.clear_lifecycle(task_id)
        return await _task_out(await service.get_task(task_id))

    # -- host (runner) liveness + reclaim ----------------------------------------------
    #
    # Container liveness one layer up: the session-service daemon holds ``/runners/{id}/live`` open
    # for its whole life, so the control plane knows which hosts are alive. ``GET /runners`` surfaces
    # the live set; ``POST /runners/{id}/reclaim`` is the operator-gated release of a dead host's
    # claims (so a healthy host respawns them) — see :meth:`TaskService.reclaim`.

    @app.get("/runners/{runner_id}/live")
    async def runner_live(
        runner_id: str,
        request: Request,
        host: str | None = Query(default=None),
    ) -> StreamingResponse:
        """The host-liveness connection: a runner holds this stream open for its whole lifetime.

        Mirrors ``/tasks/{id}/live`` one layer up. Registering happens on connect and is removed on
        disconnect — the open connection *is* the signal the runner is alive. When the daemon dies
        (clean stop or crash) the stream drops and Starlette cancels the body generator, so the
        ``finally`` removes it from ``live_runners`` **immediately** — no heartbeat, no TTL. A flaky
        drop removes it too, but the daemon re-opens this connection (its reconnect loop), so a
        transient blip self-heals. The optional ``host`` query param records the runner's hostname
        so the terminal supervisor can ssh-attach to its tasks.
        """
        reg = await service.register_runner(runner_id, host=host)

        async def hold() -> AsyncIterator[bytes]:
            try:
                yield b":ok\n"  # flush headers + confirm host liveness is established
                while True:
                    await asyncio.sleep(LIVENESS_KEEPALIVE_SECONDS)
                    yield b":keepalive\n"
            finally:  # daemon disconnected (Starlette cancels us) or the loop ended — drop it now
                await service.deregister_runner(reg.id)

        return StreamingResponse(hold(), media_type="text/event-stream")

    @app.get("/runners")
    async def list_runners() -> list[RunnerOut]:
        """The runners currently holding a host-liveness connection (sorted by id, for stable reads)."""
        return [RunnerOut(id=r.runner_id, host=r.host) for r in service.live_runner_registrations()]

    @app.get("/runners/{runner_id}")
    async def get_runner(runner_id: str) -> RunnerOut:
        """The registration details for a single live runner, or 404 if not connected."""
        if runner_id not in service.live_runners():
            raise HTTPException(status_code=404, detail=f"runner {runner_id!r} is not connected")
        return RunnerOut(id=runner_id, host=service.runner_host(runner_id))

    @app.post("/runners/{runner_id}/reclaim")
    async def reclaim(runner_id: str) -> list[TaskOut]:
        """Release a (dead) runner's non-terminal claims so a healthy host respawns them."""
        return [await _task_out(t) for t in await service.reclaim(runner_id)]

    # In-container agents connect here for task operations + artifacts. The mounted app does not
    # inherit the parent's exception handlers, so keep its response redactor at this boundary.
    app.mount("/mcp", redacted_mcp_app)
    return app

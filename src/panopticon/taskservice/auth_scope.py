"""Deterministic task-capability authorization for REST and MCP surfaces."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from mcp.types import ReadResourceRequestParams
from pydantic import ValidationError

from panopticon.taskservice.auth import decode_task_capability
from panopticon.taskservice.service import TaskService

SCOPE_FAILURE = {"detail": "credential scope forbids operation"}

#: Matches a canonical artifact resource URI *after* it has been normalized through the same
#: Pydantic ``AnyUrl`` parsing FastMCP applies before dispatch (``ReadResourceRequestParams``).
#: Each capture is constrained to one nonempty path segment, mirroring FastMCP's own
#: single-segment resource-template matching (``{task_id}``/``{name}``) and the REST route
#: matcher's ``[^/]+`` template substitution — deriving a target from a more permissive pattern
#: than what actually gets dispatched is the class of bug this guards against.
_ARTIFACT_URI_PATTERN = re.compile(r"(?:panopticon://tasks/|task://)([^/]+)/artifacts/([^/]+)")


def _normalized_artifact_target(raw_uri: Any) -> tuple[str, str]:
    """Derive ``(task_id, name)`` from an MCP artifact URI via the SDK's own normalization.

    Returns ``("", "")`` when the URI fails to parse or does not match the single-segment
    artifact template — never falling back to any part of the unnormalized input.
    """
    try:
        normalized = str(ReadResourceRequestParams(uri=raw_uri).uri)
    except ValidationError:
        return "", ""
    match = _ARTIFACT_URI_PATTERN.fullmatch(normalized)
    if match is None:
        return "", ""
    task_id, name = match.group(1), match.group(2)
    # REQ 2.1 parity, fail-closed: the ASGI layer unconditionally decodes a REST path's "%2F"
    # to a literal "/" before routing, so an equivalent REST request carrying an encoded
    # separator anywhere in {task_id}/{name} already presents as more segments than the
    # single-segment route accepts and is denied there. AnyUrl correctly leaves "%2F" encoded
    # per RFC 3986 rather than decoding it, so without this check such a URI would authorize
    # here as a single legal segment while REST denies its decoded equivalent.
    #
    # A narrower rule that only denies when decoding "%2F" and re-resolving reveals a *clean*
    # redirect to a different task was tried and rejected: a nested payload like
    # ``junk%2f..%2f..%2f{victim}%2fartifacts%2fplan.md`` decodes to a real ``..``-bearing path
    # that, after RFC-3986 dot-segment removal, no longer fits this module's two-segment
    # template at all — REST would still deny that decoded form (too many segments), but the
    # narrower rule read "doesn't cleanly resolve" as "inert content" and let it through. There
    # is no reliable way to tell that case apart from genuinely inert content (e.g. an artifact
    # deliberately named with a literal "%2f") using only the decoded string's segment shape, so
    # this denies "%2F" outright rather than risk under-denying a traversal payload. The accepted
    # cost: an own-task artifact whose real name contains "%2f" becomes unreachable over MCP
    # (REST can still reach it, via double percent-encoding) — a narrow availability loss judged
    # preferable to any residual authorization gap in a security boundary.
    if "%2f" in task_id.lower() or "%2f" in name.lower():
        return "", ""
    return task_id, name


# Streamable HTTP protocol/session messages carry no task authority themselves. Task-capability
# authorization is applied only when a request names a Panopticon tool or artifact resource.
MCP_PROTOCOL_METHODS = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "notifications/cancelled",
        "ping",
        "tools/list",
        "resources/list",
        "resources/templates/list",
        "prompts/list",
        "logging/setLevel",
    }
)


class AuthorizationClass(str, Enum):
    PUBLIC = "public"
    FLEET_READ = "fleet-read"
    FLEET_WRITE = "fleet-write"
    TASK_SCOPED = "task-scoped"
    OPERATOR_MIGRATION = "operator-migration"


class Relation(str, Enum):
    SELF = "self"
    GOVERNED = "governed-descendant"
    UNRELATED = "unrelated"
    MISSING = "missing"


class Action(str, Enum):
    READ_TASK = "read_task"
    READ_TASK_METADATA = "read_task_metadata"
    INVOKE_OPERATION = "invoke_operation"
    REQUEST_TRANSITION = "request_transition"
    SET_STATE = "set_state"
    RESOLVE_RESPONSIBILITY = "resolve_responsibility"
    SET_SLUG = "set_slug"
    SET_URL = "set_url"
    SET_TOKENS_USED = "set_tokens_used"
    SET_TOKEN_ESTIMATE = "set_token_estimate"
    SET_TURN = "set_turn"
    SET_BLOCKED = "set_blocked"
    SET_ATTENTION = "set_attention"
    SET_DEPENDENCIES = "set_dependencies"
    RECORD_STAGE_ENTRY_WAKE = "record_stage_entry_wake"
    PUT_ARTIFACT = "put_artifact"
    LIST_ARTIFACTS = "list_artifacts"
    READ_ARTIFACT = "read_artifact"
    CREATE_SESSION_INPUT = "create_session_input"
    READ_SESSION_INPUT_STATUS = "read_session_input_status"
    READ_SESSION_TRANSCRIPT = "read_session_transcript"
    REGISTER_CONTAINER = "register_container"
    DEREGISTER_CONTAINER = "deregister_container"
    TASK_LIVENESS = "task_liveness"
    CLAIM_TASK = "claim_task"
    PROVISION_TASK = "provision_task"
    MIGRATE_TASK = "migrate_task"
    REPORT_LIFECYCLE = "report_lifecycle"
    SET_GOVERNOR = "set_governor"
    SNOOZE_TASK = "snooze_task"
    PREPLAN_CHILD = "preplan_child"
    CREATE_CHILD = "create_child"
    LIST_WORKFLOWS = "list_workflows"


@dataclass(frozen=True)
class Principal:
    task_id: str

    @classmethod
    def task(cls, task_id: str) -> Principal:
        return cls(task_id)


@dataclass(frozen=True)
class Target:
    task_id: str
    relation: Relation
    orchestrates: bool


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    subject_task_id: str
    target_task_id: str
    status: int = 403
    body: dict[str, str] = field(default_factory=lambda: dict(SCOPE_FAILURE))


@dataclass(frozen=True)
class ClassifiedSurfaces:
    rest: set[tuple[str, str]]
    rest_classes: dict[tuple[str, str], AuthorizationClass]
    mcp_tools: set[str]
    mcp_resources: set[str]
    mcp_tool_classes: dict[str, AuthorizationClass]
    mcp_resource_classes: dict[str, AuthorizationClass]
    task_targeted_rest: set[tuple[str, str]]
    mcp_surfaces_with_rest_equivalents: set[tuple[str, str]]


_SELF_ACTIONS = frozenset(
    {
        Action.READ_TASK,
        Action.READ_TASK_METADATA,
        Action.INVOKE_OPERATION,
        Action.REQUEST_TRANSITION,
        Action.SET_STATE,
        Action.RESOLVE_RESPONSIBILITY,
        Action.SET_SLUG,
        Action.SET_URL,
        Action.SET_TOKENS_USED,
        Action.SET_TOKEN_ESTIMATE,
        Action.SET_TURN,
        Action.SET_BLOCKED,
        Action.SET_ATTENTION,
        Action.SET_DEPENDENCIES,
        Action.PUT_ARTIFACT,
        Action.LIST_ARTIFACTS,
        Action.READ_ARTIFACT,
        Action.CREATE_SESSION_INPUT,
        Action.READ_SESSION_INPUT_STATUS,
        Action.READ_SESSION_TRANSCRIPT,
        Action.REGISTER_CONTAINER,
        Action.DEREGISTER_CONTAINER,
        Action.TASK_LIVENESS,
    }
)
_CHILD_ACTIONS = frozenset(
    {
        Action.READ_TASK,
        Action.READ_TASK_METADATA,
        Action.PUT_ARTIFACT,
        Action.LIST_ARTIFACTS,
        Action.READ_ARTIFACT,
        Action.SET_SLUG,
        Action.SET_TOKEN_ESTIMATE,
        Action.SET_TURN,
        Action.SET_DEPENDENCIES,
    }
)
_TASK_TARGETED = frozenset(
    action
    for action in Action
    if action not in {Action.CREATE_CHILD, Action.LIST_WORKFLOWS, Action.PREPLAN_CHILD}
)


def task_targeted_actions() -> frozenset[Action]:
    return _TASK_TARGETED


def authorize(principal: Principal, action: Action, target: Target) -> ScopeDecision:
    if (
        not isinstance(principal, Principal)
        or not isinstance(action, Action)
        or not isinstance(target, Target)
    ):
        raise TypeError("principal, action, and target must use authorization domain types")
    allowed = False
    if target.relation is Relation.SELF and target.task_id == principal.task_id:
        allowed = action in _SELF_ACTIONS
    elif target.relation is Relation.GOVERNED and target.orchestrates:
        allowed = action in _CHILD_ACTIONS or action is Action.PREPLAN_CHILD
    return ScopeDecision(allowed, principal.task_id, target.task_id)


_REST_ACTIONS: dict[tuple[str, str], Action] = {
    ("GET", "/tasks/{task_id}"): Action.READ_TASK,
    ("POST", "/tasks/{task_id}/operations/{operation}"): Action.INVOKE_OPERATION,
    ("PUT", "/tasks/{task_id}/state"): Action.SET_STATE,
    ("POST", "/tasks/{task_id}/transition"): Action.REQUEST_TRANSITION,
    ("POST", "/tasks/{task_id}/responsibilities"): Action.RESOLVE_RESPONSIBILITY,
    ("PUT", "/tasks/{task_id}/slug"): Action.SET_SLUG,
    ("PUT", "/tasks/{task_id}/url"): Action.SET_URL,
    ("PUT", "/tasks/{task_id}/tokens-used"): Action.SET_TOKENS_USED,
    ("PUT", "/tasks/{task_id}/token-estimate"): Action.SET_TOKEN_ESTIMATE,
    ("PUT", "/tasks/{task_id}/turn"): Action.SET_TURN,
    ("PUT", "/tasks/{task_id}/blocked"): Action.SET_BLOCKED,
    ("PUT", "/tasks/{task_id}/attention"): Action.SET_ATTENTION,
    ("PUT", "/tasks/{task_id}/dependencies"): Action.SET_DEPENDENCIES,
    ("PUT", "/tasks/{task_id}/history/{entry_index}/wake"): Action.RECORD_STAGE_ENTRY_WAKE,
    ("PUT", "/tasks/{task_id}/artifacts/{name}"): Action.PUT_ARTIFACT,
    ("GET", "/tasks/{task_id}/artifacts"): Action.LIST_ARTIFACTS,
    ("GET", "/tasks/{task_id}/artifacts/{name}"): Action.READ_ARTIFACT,
    ("POST", "/tasks/{task_id}/session/input"): Action.CREATE_SESSION_INPUT,
    ("GET", "/tasks/{task_id}/session/input/{delivery_id}"): Action.READ_SESSION_INPUT_STATUS,
    ("GET", "/tasks/{task_id}/session/transcript"): Action.READ_SESSION_TRANSCRIPT,
    ("POST", "/tasks/{task_id}/registrations"): Action.REGISTER_CONTAINER,
    ("GET", "/tasks/{task_id}/registrations"): Action.READ_TASK_METADATA,
    ("GET", "/tasks/{task_id}/live"): Action.TASK_LIVENESS,
    ("PUT", "/tasks/{task_id}/claim"): Action.CLAIM_TASK,
    ("DELETE", "/tasks/{task_id}/claim"): Action.CLAIM_TASK,
    ("PUT", "/tasks/{task_id}/provisioning"): Action.PROVISION_TASK,
    ("PUT", "/tasks/{task_id}/migration"): Action.MIGRATE_TASK,
    ("PUT", "/tasks/{task_id}/lifecycle"): Action.REPORT_LIFECYCLE,
    ("DELETE", "/tasks/{task_id}/lifecycle"): Action.REPORT_LIFECYCLE,
    ("PUT", "/tasks/{task_id}/governor"): Action.SET_GOVERNOR,
    ("PUT", "/tasks/{task_id}/snooze"): Action.SNOOZE_TASK,
}
for suffix in ("transitions", "operations", "states", "skills", "briefing", "workflow-overview"):
    _REST_ACTIONS[("GET", f"/tasks/{{task_id}}/{suffix}")] = Action.READ_TASK_METADATA
_REST_ACTIONS[("GET", "/tasks/{task_id}/history/{entry_index}/wake")] = Action.READ_TASK_METADATA

_MCP_ACTIONS = {
    "get_task": Action.READ_TASK,
    "set_slug": Action.SET_SLUG,
    "set_url": Action.SET_URL,
    "set_tokens_used": Action.SET_TOKENS_USED,
    "set_token_estimate": Action.SET_TOKEN_ESTIMATE,
    "apply_operation": Action.INVOKE_OPERATION,
    "set_state": Action.SET_STATE,
    "resolve_responsibility": Action.RESOLVE_RESPONSIBILITY,
    "set_turn": Action.SET_TURN,
    "set_blocked": Action.SET_BLOCKED,
    "set_attention": Action.SET_ATTENTION,
    "set_dependencies": Action.SET_DEPENDENCIES,
    "put_artifact": Action.PUT_ARTIFACT,
    "list_artifacts": Action.LIST_ARTIFACTS,
}


class CredentialScopePolicy:
    def __init__(
        self, service: TaskService, write_tokens: tuple[str, ...], app: Any, mcp: Any
    ) -> None:
        self._service = service
        self._write_tokens = write_tokens
        self._app = app
        self._mcp = mcp

    def _tasks(self) -> list[Any]:
        return asyncio.run(self._service.list_tasks())

    def _target(self, subject: str, target_id: str, tasks: list[Any]) -> Target:
        by_id = {task.id: task for task in tasks}
        target = by_id.get(target_id)
        subject_task = by_id.get(subject)
        orchestrates = self._service.task_orchestrates(subject_task)
        if target is None:
            relation = Relation.MISSING
        elif target.id == subject and subject_task is not None:
            relation = Relation.SELF
        else:
            relation = Relation.UNRELATED
            current = target
            seen: set[str] = set()
            while current.governor_task_id and current.id not in seen:
                seen.add(current.id)
                if current.governor_task_id == subject:
                    relation = Relation.GOVERNED
                    break
                current = by_id.get(current.governor_task_id)
                if current is None:
                    break
        return Target(target_id, relation, orchestrates)

    async def target(self, subject: str, target_id: str) -> Target:
        return self._target(subject, target_id, await self._service.list_tasks())

    async def decide(self, subject: str, action: Action, target_id: str) -> ScopeDecision:
        return authorize(Principal.task(subject), action, await self.target(subject, target_id))

    async def decide_responsibility(
        self, subject: str, target_id: str, responsibility_key: str
    ) -> ScopeDecision:
        """Authorize self resolution or a governed child's current planning responsibility."""

        tasks = await self._service.list_tasks()
        target = self._target(subject, target_id, tasks)
        if target.relation is Relation.SELF:
            return authorize(Principal.task(subject), Action.RESOLVE_RESPONSIBILITY, target)
        target_task = next((task for task in tasks if task.id == target_id), None)
        planning_key = bool(
            target_task is not None
            and target_task.state == "PLANNING"
            and any(
                item.key == responsibility_key
                for item in target_task.current_entry.responsibilities
            )
        )
        allowed = bool(
            target.relation is Relation.GOVERNED and target.orchestrates and planning_key
        )
        return ScopeDecision(allowed, subject, target_id)

    async def decide_dependencies(
        self, subject: str, target_id: str, dep_ids: list[str]
    ) -> ScopeDecision:
        """Authorize a dependency-list replacement, treating each proposed id as a target too.

        Every nonempty ``dep_ids`` entry must pass the same self-or-governed-descendant scope
        check as the primary ``target_id``, evaluated before the service layer's existence or
        cycle validation runs — otherwise an out-of-scope id (existing or not) could be
        distinguished by the differing error it produces downstream.
        """
        tasks = await self._service.list_tasks()
        primary = authorize(
            Principal.task(subject),
            Action.SET_DEPENDENCIES,
            self._target(subject, target_id, tasks),
        )
        if not primary.allowed:
            return primary
        for dep_id in dep_ids:
            if not dep_id:
                continue
            secondary = authorize(
                Principal.task(subject),
                Action.SET_DEPENDENCIES,
                self._target(subject, dep_id, tasks),
            )
            if not secondary.allowed:
                return ScopeDecision(False, subject, target_id)
        return primary

    async def authorize_mcp_async(self, token: str, request: dict[str, Any]) -> ScopeDecision:
        subject = decode_task_capability(token, self._write_tokens) or ""
        action: Action | None
        params = request.get("params") if isinstance(request, dict) else None
        params = params if isinstance(params, dict) else {}
        raw_arguments = params.get("arguments")
        arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
        if request.get("method") == "resources/read":
            target_id, _name = _normalized_artifact_target(params.get("uri", ""))
            action = Action.READ_ARTIFACT
        else:
            name = str(params.get("name", ""))
            if name in {"create_task", "list_workflows"}:
                target_id = str(arguments.get("orchestrator_task_id", ""))
                action = Action.CREATE_CHILD if name == "create_task" else Action.LIST_WORKFLOWS
            else:
                target_id = str(arguments.get("task_id", ""))
                action = _MCP_ACTIONS.get(name)
        if not subject or action is None:
            return ScopeDecision(False, subject, target_id)
        if action in {Action.CREATE_CHILD, Action.LIST_WORKFLOWS}:
            target = await self.target(subject, subject)
            allowed = (
                target.relation is Relation.SELF and target.orchestrates and target_id == subject
            )
            return ScopeDecision(allowed, subject, target_id)
        if action is Action.RESOLVE_RESPONSIBILITY:
            return await self.decide_responsibility(
                subject, target_id, str(arguments.get("key", ""))
            )
        if action is Action.SET_DEPENDENCIES:
            raw_dep_ids = arguments.get("dep_ids")
            dep_ids = [str(item) for item in raw_dep_ids] if isinstance(raw_dep_ids, list) else []
            return await self.decide_dependencies(subject, target_id, dep_ids)
        return await self.decide(subject, action, target_id)

    @staticmethod
    def is_mcp_protocol_request(request: dict[str, Any]) -> bool:
        """Whether a JSON-RPC message is transport/discovery rather than a scoped invocation."""

        return request.get("method") in MCP_PROTOCOL_METHODS

    def action_for_rest(self, method: str, template: str) -> Action | None:
        return _REST_ACTIONS.get((method.upper(), template))

    def action_for_mcp(
        self, surface: tuple[str, str] | str, name: str | None = None
    ) -> Action | None:
        if isinstance(surface, str):
            surface = (surface, name or "")
        if surface == ("resource", "artifact"):
            return Action.READ_ARTIFACT
        return _MCP_ACTIONS.get(surface[1]) if surface[0] == "tool" else None

    def _match_rest(self, method: str, path: str) -> tuple[Action | None, str | None]:
        for (candidate_method, template), action in _REST_ACTIONS.items():
            if candidate_method != method.upper():
                continue
            pattern = re.sub(r"\{[^}]+\}", r"([^/]+)", template)
            match = re.fullmatch(pattern, path)
            if match:
                names = re.findall(r"\{([^}]+)\}", template)
                values = dict(zip(names, match.groups(), strict=True))
                return action, values.get("task_id")
        return None, None

    def authorize_rest_request(self, token: str, method: str, path: str) -> ScopeDecision:
        """Authorize a REST request by its primary target only.

        Test/parity-checking helper, not part of live enforcement (the ASGI middleware and
        ``authorize_mcp_async`` own that). For ``SET_DEPENDENCIES`` this never sees
        ``dep_ids`` and so never applies ``decide_dependencies``'s secondary-target check —
        wiring this into enforcement for that action would need the same treatment.
        """
        subject = decode_task_capability(token, self._write_tokens) or ""
        action, target_id = self._match_rest(method, path)
        if not subject or action is None or target_id is None:
            return ScopeDecision(False, subject, target_id or "")
        return authorize(
            Principal.task(subject), action, self._target(subject, target_id, self._tasks())
        )

    def authorize_rest(
        self, token: str, method: str, template: str, path_params: dict[str, Any]
    ) -> ScopeDecision:
        """Authorize a REST request by its primary target only — see ``authorize_rest_request``'s
        note on ``SET_DEPENDENCIES`` and secondary targets; the same caveat applies here."""
        subject = decode_task_capability(token, self._write_tokens) or ""
        target_id = str(path_params.get("task_id", ""))
        action = self.action_for_rest(method, template)
        if not subject or action is None:
            return ScopeDecision(False, subject, target_id)
        return authorize(
            Principal.task(subject), action, self._target(subject, target_id, self._tasks())
        )

    def authorize_mcp_surface(
        self, token: str, surface: tuple[str, str], arguments: dict[str, Any]
    ) -> ScopeDecision:
        """Authorize an MCP surface by its primary target only — see ``authorize_rest_request``'s
        note on ``SET_DEPENDENCIES`` and secondary targets; the same caveat applies here."""
        subject = decode_task_capability(token, self._write_tokens) or ""
        target_id = str(arguments.get("task_id", ""))
        action = self.action_for_mcp(surface)
        if not subject or action is None:
            return ScopeDecision(False, subject, target_id)
        return authorize(
            Principal.task(subject), action, self._target(subject, target_id, self._tasks())
        )

    def authorize_mcp(self, token: str, request: dict[str, Any]) -> ScopeDecision:
        return asyncio.run(self.authorize_mcp_async(token, request))

    def classified_surfaces(self) -> ClassifiedSurfaces:
        rest_entries = {
            (method, route.path)
            for route in self._app.routes
            if hasattr(route, "methods")
            and route.path not in {"/healthz", "/docs", "/docs/oauth2-redirect", "/openapi.json"}
            for method in route.methods
        }
        task_targeted = {entry for entry in rest_entries if entry in _REST_ACTIONS} | {
            ("DELETE", "/registrations/{registration_id}")
        }
        admin = self.fleet_administration_rest_surfaces()
        rest_classes = {
            entry: (
                AuthorizationClass.TASK_SCOPED
                if entry in task_targeted
                or entry == ("POST", "/tasks")
                or entry == ("GET", "/tasks")
                else AuthorizationClass.OPERATOR_MIGRATION
                if entry == ("PUT", "/tasks/{task_id}/migration")
                else AuthorizationClass.FLEET_WRITE
                if entry in admin or entry[0] not in {"GET", "HEAD"}
                else AuthorizationClass.FLEET_READ
            )
            for entry in rest_entries
        }
        tools = set(self._mcp._tool_manager._tools)
        resources = {
            *map(str, self._mcp._resource_manager._resources),
            *map(str, self._mcp._resource_manager._templates),
        }
        mcp_tool_classes = dict.fromkeys(tools, AuthorizationClass.TASK_SCOPED)
        mcp_resource_classes = dict.fromkeys(resources, AuthorizationClass.TASK_SCOPED)
        equivalents = {("tool", name) for name in _MCP_ACTIONS} | {("resource", "artifact")}
        return ClassifiedSurfaces(
            rest_entries,
            rest_classes,
            tools,
            resources,
            mcp_tool_classes,
            mcp_resource_classes,
            task_targeted,
            equivalents,
        )

    def classification_for_rest(self, method: str, path: str) -> AuthorizationClass | None:
        return self.classified_surfaces().rest_classes.get((method, path))

    def classification_for_mcp_tool(self, name: str) -> AuthorizationClass | None:
        return self.classified_surfaces().mcp_tool_classes.get(name)

    def classification_for_mcp_resource(self, uri: str) -> AuthorizationClass | None:
        return self.classified_surfaces().mcp_resource_classes.get(uri)

    @staticmethod
    def fleet_administration_rest_surfaces() -> set[tuple[str, str]]:
        return {
            ("POST", "/repos"),
            ("PATCH", "/repos/{repo_id}"),
            ("GET", "/workflow-files"),
            ("PUT", "/tasks/{task_id}/claim"),
            ("DELETE", "/tasks/{task_id}/claim"),
            ("PUT", "/tasks/{task_id}/provisioning"),
            ("PUT", "/tasks/{task_id}/migration"),
            ("PUT", "/tasks/{task_id}/lifecycle"),
            ("DELETE", "/tasks/{task_id}/lifecycle"),
            ("PUT", "/tasks/{task_id}/governor"),
            ("PUT", "/tasks/{task_id}/snooze"),
            ("GET", "/tasks/{task_id}/session/input"),
            ("PUT", "/tasks/{task_id}/session/input/{delivery_id}"),
            ("PUT", "/tasks/{task_id}/session/transcript"),
            ("GET", "/runners"),
            ("GET", "/runners/{runner_id}"),
            ("GET", "/runners/{runner_id}/live"),
            ("POST", "/runners/{runner_id}/reclaim"),
        }

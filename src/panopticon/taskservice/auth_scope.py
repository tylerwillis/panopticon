"""Deterministic task-capability authorization for REST and MCP surfaces."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from panopticon.taskservice.auth import decode_task_capability
from panopticon.taskservice.service import TaskService

SCOPE_FAILURE = {"detail": "credential scope forbids operation"}


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
    body: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.body is None:
            object.__setattr__(self, "body", SCOPE_FAILURE)


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
        Action.RESOLVE_RESPONSIBILITY,
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
        orchestrates = bool(
            subject_task is not None and self._service._workflow(subject_task.workflow).orchestrates
        )
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

    async def authorize_mcp_async(self, token: str, request: dict[str, Any]) -> ScopeDecision:
        subject = decode_task_capability(token, self._write_tokens) or ""
        action: Action | None
        params = request.get("params") if isinstance(request, dict) else None
        params = params if isinstance(params, dict) else {}
        if request.get("method") == "resources/read":
            match = re.fullmatch(
                r"(?:panopticon://tasks/|task://)([^/]+)/artifacts/(.+)",
                str(params.get("uri", "")),
            )
            target_id = match.group(1) if match else ""
            action = Action.READ_ARTIFACT
        else:
            name = str(params.get("name", ""))
            arguments = params.get("arguments")
            arguments = arguments if isinstance(arguments, dict) else {}
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
        return await self.decide(subject, action, target_id)

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
        subject = decode_task_capability(token, self._write_tokens) or ""
        target_id = str(arguments.get("task_id", ""))
        action = self.action_for_mcp(surface)
        if not subject or action is None:
            return ScopeDecision(False, subject, target_id)
        return authorize(
            Principal.task(subject), action, self._target(subject, target_id, self._tasks())
        )

    def authorize_mcp(self, token: str, request: dict[str, Any]) -> ScopeDecision:
        params = request.get("params") if isinstance(request, dict) else None
        params = params if isinstance(params, dict) else {}
        if request.get("method") == "resources/read":
            uri = params.get("uri", "")
            match = re.fullmatch(r"(?:panopticon://tasks/|task://)([^/]+)/artifacts/(.+)", str(uri))
            target = match.group(1) if match else ""
            return self.authorize_mcp_surface(token, ("resource", "artifact"), {"task_id": target})
        name = str(params.get("name", ""))
        arguments = params.get("arguments")
        return self.authorize_mcp_surface(
            token, ("tool", name), arguments if isinstance(arguments, dict) else {}
        )

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
            ("GET", "/runners"),
            ("GET", "/runners/{runner_id}"),
            ("GET", "/runners/{runner_id}/live"),
            ("POST", "/runners/{runner_id}/reclaim"),
        }

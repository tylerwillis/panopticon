"""Discovery and recording helpers used only by the standing credential guard tests."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from starlette.routing import BaseRoute, Mount

from panopticon.core.artifact_skills import ARTIFACT_SKILL
from panopticon.harnesses import HARNESSES, BootstrapContext, LaunchContext

PUBLIC_UNAUTHENTICATED_ROUTES = frozenset({("GET", "/healthz"), ("HEAD", "/healthz")})


@dataclass(frozen=True)
class ComposedRoute:
    method: str
    path: str
    probe_path: str


@dataclass(frozen=True)
class CurlViolation:
    path: Path
    line_number: int


@dataclass(frozen=True)
class ShellCaller:
    path: Path
    bare_curl_violations: tuple[CurlViolation, ...]


@dataclass(frozen=True)
class RenderedFile:
    harness: str
    path: Path
    content: str


@dataclass(frozen=True)
class HarnessSurface:
    files: tuple[RenderedFile, ...]
    argv: tuple[str, ...]


@dataclass(frozen=True)
class CurlObservation:
    harness: str
    path: Path
    argv: str


def _line_invokes_service_tool(line: str) -> bool:
    if re.match(r"^(['\"])curl\1(?:\s|$)", line.lstrip()) and not re.search(r";|&&|\|\|", line):
        return False
    parse_line = line.rstrip()
    if parse_line.endswith("\\"):
        parse_line = parse_line[:-1]
    parse_line = re.sub(
        r"(^|(?:;|&&|\|\|)\s*)(['\"])(?:curl|wget|http)\2",
        r"\1quoted-service-tool",
        parse_line,
    )
    lexer = shlex.shlex(parse_line, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    command_position = True
    service_command = False
    service_has_url = False
    for token in tokens:
        if token in {";", "&&", "||"}:
            if service_command and service_has_url:
                return True
            command_position = True
            service_command = False
            service_has_url = False
            continue
        if command_position:
            service_command = token.rsplit("/", 1)[-1] in {"curl", "wget", "http"}
            command_position = False
        elif service_command and "PANOPTICON_SERVICE_URL" in token:
            service_has_url = True
    return service_command and service_has_url


def _structural_brace_delta(line: str) -> int:
    structural = re.sub(r"'(?:[^']*)'|\"(?:[^\"]*)\"", "", line).split("#", 1)[0]
    return structural.count("{") - structural.count("}")


def assert_minimum_subjects(subjects: list[object], minimum: int, kind: str) -> None:
    assert len(subjects) >= minimum, (
        f"{kind} sweep found only {len(subjects)} subjects: {subjects!r}; repair discovery "
        "before weakening the required floor"
    )


def _probe_path(path: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-") or "root"
    return re.sub(r"\{[^}]+\}", f"credential-guard-{slug}", path) or "/"


def _walk_routes(routes: list[BaseRoute], prefix: str = "") -> list[ComposedRoute]:
    found: list[ComposedRoute] = []
    for route in routes:
        path = prefix + getattr(route, "path", "")
        if isinstance(route, Mount):
            child_routes = list(getattr(route.app, "routes", ()))
            if child_routes:
                found.extend(_walk_routes(child_routes, path.rstrip("/")))
            else:
                for method in ("DELETE", "GET", "POST"):
                    found.append(ComposedRoute(method, path, _probe_path(path)))
            continue
        methods = getattr(route, "methods", None) or {"GET"}
        for method in sorted(methods):
            found.append(ComposedRoute(method, path, _probe_path(path)))
    return found


def discover_composed_routes(app: FastAPI) -> list[ComposedRoute]:
    return _walk_routes(list(app.routes))


def discover_shell_service_callers(root: Path) -> list[ShellCaller]:
    callers: list[ShellCaller] = []
    for path in sorted(root.rglob("*.sh")):
        text = path.read_text()
        if "PANOPTICON_SERVICE_URL" not in text:
            continue
        violations: list[CurlViolation] = []
        inside_helper = False
        helper_depth = 0
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if re.match(r"_panopticon_curl\s*\(\)\s*\{", stripped):
                if re.search(r"\b(?:wget|http)\s+.*PANOPTICON_SERVICE_URL", stripped):
                    violations.append(CurlViolation(path, number))
                inside_helper = True
                helper_depth = _structural_brace_delta(stripped)
                if helper_depth <= 0:
                    inside_helper = False
                    tail = stripped.rsplit("}", 1)[-1]
                    if "PANOPTICON_SERVICE_URL" in tail and _line_invokes_service_tool(tail):
                        violations.append(CurlViolation(path, number))
                continue
            if inside_helper:
                if re.search(r"(^|[;&|()]|\s)(?:wget|http)(?:\s|$)", stripped):
                    violations.append(CurlViolation(path, number))
                helper_depth += _structural_brace_delta(stripped)
                if helper_depth <= 0:
                    inside_helper = False
                    tail = stripped.rsplit("}", 1)[-1]
                    if "PANOPTICON_SERVICE_URL" in tail and _line_invokes_service_tool(tail):
                        violations.append(CurlViolation(path, number))
                continue
            if "PANOPTICON_SERVICE_URL" in stripped and _line_invokes_service_tool(stripped):
                violations.append(CurlViolation(path, number))
        callers.append(ShellCaller(path, tuple(violations)))
    return callers


def shell_violation_message(violations: list[CurlViolation]) -> str:
    return "\n".join(
        f"{item.path}:{item.line_number}: task-service call bypasses _panopticon_curl; "
        "route it through the authenticated helper"
        for item in violations
    )


def render_authenticated_harness_surfaces(
    tmp_path: Path, credential: str
) -> dict[str, HarnessSurface]:
    surfaces: dict[str, HarnessSurface] = {}
    for name, harness in HARNESSES.items():
        root = tmp_path / name
        home = root / "home"
        cwd = root / "workspace"
        home.mkdir(parents=True)
        cwd.mkdir()
        context = BootstrapContext(
            home=home,
            cwd=cwd,
            service_url="http://service",
            task_id="credential-guard-task",
            skills=(ARTIFACT_SKILL,),
            operations={"advance": "COMPLETE", "drop": "DROPPED"},
            overview="credential guard overview",
            environ={"PANOPTICON_SERVICE_AUTH_TOKEN": credential},
        )
        harness.bootstrap(context)
        files = tuple(
            RenderedFile(name, path.relative_to(root), path.read_text(errors="replace"))
            for path in sorted(root.rglob("*"))
            if path.is_file()
        )
        argv = tuple(harness.argv(LaunchContext(home=home, cwd=cwd, initial_prompt="guard")))
        surfaces[name] = HarnessSurface(files, argv)
    return surfaces


def _authenticated_shell_commands(content: str) -> list[str]:
    commands: list[str] = []
    for command in re.findall(r"(?<!`)`([^`]*)`(?!`)", content, flags=re.DOTALL):
        if re.search(r"(?:^|(?:;|&&|\|\||\|))\s*curl(?:\s|>|$)", command):
            commands.append(command)
    return commands


def assert_harness_credentials_stay_out_of_argv(
    surfaces: dict[str, HarnessSurface], credential: str, tmp_path: Path
) -> list[CurlObservation]:
    observations: list[CurlObservation] = []
    artifact = tmp_path / "artifact.md"
    artifact.write_text("guard")
    for harness, surface in surfaces.items():
        for rendered in surface.files:
            for index, command in enumerate(_authenticated_shell_commands(rendered.content)):
                command = command.replace("<artifact-file>", str(artifact)).replace(
                    "<name>", "guard.md"
                )
                argv_path = tmp_path / f"{harness}-{index}-argv"
                stdin_path = tmp_path / f"{harness}-{index}-stdin"
                script = (
                    f"curl() {{ cat > {stdin_path}; printf '%s\\n' \"$@\" > {argv_path}; }}\n"
                    f"set -x\n{command}\n"
                )
                completed = subprocess.run(
                    ["sh", "-c", script],
                    env={
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                        "PANOPTICON_SERVICE_AUTH_TOKEN": credential,
                        "PANOPTICON_SERVICE_URL": "http://service",
                        "PANOPTICON_TASK_ID": "credential-guard-task",
                    },
                    check=True,
                    text=True,
                    capture_output=True,
                )
                argv = argv_path.read_text()
                assert credential not in argv, (
                    f"{harness}: {rendered.path} placed the credential in curl argv; pass it "
                    "through curl --config - on stdin"
                )
                assert credential not in completed.stderr, (
                    f"{harness}: {rendered.path} exposed the credential under shell xtrace; "
                    "disable tracing around credential expansion and use stdin"
                )
                observations.append(CurlObservation(harness, rendered.path, argv))
    return observations

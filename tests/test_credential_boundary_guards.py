"""Standing, discovery-driven guards for the task-service credential boundary."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest
from credential_guard_helpers import (
    PUBLIC_UNAUTHENTICATED_ROUTES,
    HarnessSurface,
    RenderedFile,
    _authenticated_shell_commands,
    assert_harness_credentials_stay_out_of_argv,
    assert_minimum_subjects,
    discover_composed_routes,
    discover_shell_service_callers,
    render_authenticated_harness_surfaces,
    shell_violation_message,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

from panopticon.harnesses import HARNESSES
from panopticon.taskservice.__main__ import build_app

READ_TOKEN = "guard-read-token-9f830a"
WRITE_TOKEN = "guard-write-token-4c721b"


def _independent_declared_routes(routes: list[object], prefix: str = "") -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    for route in routes:
        path = prefix + getattr(route, "path", "")
        if isinstance(route, Mount) and getattr(route.app, "routes", None):
            identities |= _independent_declared_routes(list(route.app.routes), path.rstrip("/"))
        else:
            identities |= {(method, path) for method in (getattr(route, "methods", None) or ())}
    return identities


def _production_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "config"
    secrets = config / "secrets"
    secrets.mkdir(parents=True)
    credential_file = secrets / "task-service-auth.json"
    credential_file.write_text(json.dumps({"read": [READ_TOKEN], "write": [WRITE_TOKEN]}))
    credential_file.chmod(0o600)
    monkeypatch.setenv("PANOPTICON_CONFIG", str(config))
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", credential_file.name)
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_MODE", "enforced")
    return build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        layers_root=str(tmp_path / "layers"),
        _home_workflows=tmp_path / "home-workflows",
    )


def test_every_composed_route_rejects_unauthenticated_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-044.1.1
    # 2119: REQ-044.7.1
    app = _production_app(tmp_path, monkeypatch)
    routes = discover_composed_routes(app)
    assert_minimum_subjects(list(routes), 50, "composed route")
    directly_declared = _independent_declared_routes(list(app.routes))
    assert directly_declared <= {(route.method, route.path) for route in routes}
    app.add_api_route("/future-guard-route", lambda: {"ok": True}, methods=["PUT"])
    child = FastAPI()
    child.add_api_route("/child", lambda: {"ok": True}, methods=["PATCH"])
    app.mount("/guard-mount", child)
    routes = discover_composed_routes(app)
    identities = {(route.method, route.path) for route in routes}
    assert ("PUT", "/future-guard-route") in identities
    assert ("PATCH", "/guard-mount/child") in identities
    for required in {
        ("GET", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("GET", "/openapi.json"),
        ("GET", "/redoc"),
        ("GET", "/mcp"),
        ("POST", "/mcp"),
        ("DELETE", "/mcp"),
    }:
        assert required in identities, (
            f"composed route discovery omitted generated or mounted route {required!r}; recurse "
            "through app.routes and mounted child route tables before testing authentication"
        )

    with TestClient(app, raise_server_exceptions=False) as client:
        for route in routes:
            response = client.request(route.method, route.probe_path)
            if (route.method, route.path) in PUBLIC_UNAUTHENTICATED_ROUTES:
                assert response.status_code == 200, (
                    f"public allowlist entry {route.method} {route.path} is no longer public; "
                    "remove it from PUBLIC_UNAUTHENTICATED_ROUTES or restore the public route"
                )
            else:
                assert response.status_code == 401, (
                    f"unauthenticated composed route {route.method} {route.path} returned "
                    f"{response.status_code}; protect it with task-service authentication or "
                    "add an explicitly reviewed PUBLIC_UNAUTHENTICATED_ROUTES entry"
                )
                if route.method != "HEAD":
                    assert response.json() == {"detail": "authentication required"}, (
                        f"{route.method} {route.path} returned a non-generic authentication body; "
                        "use the shared generic authentication rejection"
                    )
                assert response.headers["www-authenticate"] == "Bearer", (
                    f"{route.method} {route.path} omitted the Bearer challenge; return the shared "
                    "generic authentication rejection"
                )


def test_every_shell_service_caller_uses_authenticated_helper() -> None:
    # 2119: REQ-044.2.1
    # 2119: REQ-044.7.1
    callers = discover_shell_service_callers(Path("src"))
    independently_discovered = {
        path for path in Path("src").rglob("*.sh") if "PANOPTICON_SERVICE_URL" in path.read_text()
    }
    assert {caller.path for caller in callers} == independently_discovered
    assert_minimum_subjects(list(callers), 3, "shell service-caller")
    assert all(caller.path.is_relative_to(Path("src")) for caller in callers), (
        f"shell sweep subjects escaped the real src tree: {[str(c.path) for c in callers]!r}"
    )
    violations = [violation for caller in callers for violation in caller.bare_curl_violations]
    assert not violations, shell_violation_message(violations)


@pytest.mark.parametrize("command", ["curl", "wget", "http"])
def test_shell_sweep_rejects_the_next_bare_caller(tmp_path: Path, command: str) -> None:
    # 2119: REQ-044.2.1
    for index in range(3):
        (tmp_path / f"safe-{index}.sh").write_text(
            'echo $PANOPTICON_SERVICE_URL\n_panopticon_curl "$PANOPTICON_SERVICE_URL/tasks"\n'
        )
    offender = tmp_path / "nested" / "future-caller.sh"
    offender.parent.mkdir()
    offender.write_text(f'{command} "$PANOPTICON_SERVICE_URL/tasks"\n')
    callers = discover_shell_service_callers(tmp_path)
    violations = [item for caller in callers for item in caller.bare_curl_violations]
    assert [(item.path, item.line_number) for item in violations] == [(offender, 1)]


def test_shell_sweep_exempts_only_curl_inside_authenticated_helper(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    helper = tmp_path / "helper.sh"
    helper.write_text(
        "_panopticon_curl() {\n"
        '  curl --config - "$PANOPTICON_SERVICE_URL/tasks"\n'
        '  wget "$PANOPTICON_SERVICE_URL/tasks"\n'
        '  http "$PANOPTICON_SERVICE_URL/tasks"\n'
        "}\n"
    )
    [caller] = discover_shell_service_callers(tmp_path)
    assert [(item.path, item.line_number) for item in caller.bare_curl_violations] == [
        (helper, 3),
        (helper, 4),
    ]


def test_shell_sweep_closes_one_line_helper_definition(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    helper = tmp_path / "one-line.sh"
    helper.write_text(
        '_panopticon_curl() { curl "$PANOPTICON_SERVICE_URL/tasks"; }\n'
        'curl "$PANOPTICON_SERVICE_URL/tasks"\n'
    )
    [caller] = discover_shell_service_callers(tmp_path)
    assert [(item.path, item.line_number) for item in caller.bare_curl_violations] == [(helper, 2)]


def test_shell_sweep_ignores_braces_in_helper_strings_and_comments(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    helper = tmp_path / "braces.sh"
    helper.write_text(
        '_panopticon_curl() { echo "{"; # }\n'
        '  curl "$PANOPTICON_SERVICE_URL/inside"\n'
        "}\n"
        'curl "$PANOPTICON_SERVICE_URL/outside"\n'
    )
    [caller] = discover_shell_service_callers(tmp_path)
    assert [(item.path, item.line_number) for item in caller.bare_curl_violations] == [(helper, 4)]


@pytest.mark.parametrize(
    ("minimum", "subjects", "kind"),
    [(50, list(range(49)), "route"), (3, ["a", "b"], "shell"), (1, [], "harness")],
)
def test_sweep_count_boundaries_report_discovered_identities(
    minimum: int, subjects: list[object], kind: str
) -> None:
    # 2119: REQ-044.7.1
    with pytest.raises(AssertionError) as exc:
        assert_minimum_subjects(subjects, minimum, kind)
    assert kind in str(exc.value)
    assert repr(subjects) in str(exc.value)


def test_shell_sweep_honors_command_and_physical_line_boundaries(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    safe = tmp_path / "boundaries.sh"
    safe.write_text(
        '"curl" "$PANOPTICON_SERVICE_URL/tasks"\n'
        '# curl "$PANOPTICON_SERVICE_URL/tasks"\n'
        'echo curl "$PANOPTICON_SERVICE_URL/tasks"\n'
        'echo "; curl $PANOPTICON_SERVICE_URL/tasks"\n'
        'true; # curl "$PANOPTICON_SERVICE_URL/tasks"\n'
        'curl \\\n "$PANOPTICON_SERVICE_URL/tasks"\n'
    )
    [caller] = discover_shell_service_callers(tmp_path)
    assert caller.bare_curl_violations == ()


def test_shell_sweep_rejects_command_after_separator(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    offender = tmp_path / "compound.sh"
    offender.write_text('true; curl "$PANOPTICON_SERVICE_URL/tasks"\n')
    [caller] = discover_shell_service_callers(tmp_path)
    assert [(item.path, item.line_number) for item in caller.bare_curl_violations] == [
        (offender, 1)
    ]


@pytest.mark.parametrize("separator", [";", "&&", "||"])
@pytest.mark.parametrize("command", ["curl", "wget", "http"])
def test_shell_sweep_rejects_each_declared_separator(
    tmp_path: Path, separator: str, command: str
) -> None:
    # 2119: REQ-044.2.1
    offender = tmp_path / "separator.sh"
    offender.write_text(f'true {separator} {command} "$PANOPTICON_SERVICE_URL/tasks"\n')
    [caller] = discover_shell_service_callers(tmp_path)
    assert [(item.path, item.line_number) for item in caller.bare_curl_violations] == [
        (offender, 1)
    ]


def test_shell_sweep_diagnostic_names_file_line_and_fix(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    offender = tmp_path / "future.sh"
    offender.write_text('curl "$PANOPTICON_SERVICE_URL/tasks"\n')
    [caller] = discover_shell_service_callers(tmp_path)
    message = shell_violation_message(list(caller.bare_curl_violations))
    assert message == (
        f"{offender}:1: task-service call bypasses _panopticon_curl; "
        "route it through the authenticated helper"
    )


def test_registered_harness_commands_keep_credentials_out_of_runtime_argv(
    tmp_path: Path,
) -> None:
    # 2119: REQ-044.3.1
    # 2119: REQ-044.7.1
    surfaces = render_authenticated_harness_surfaces(tmp_path, WRITE_TOKEN)
    assert set(surfaces) == set(HARNESSES), (
        f"harness credential sweep rendered {sorted(surfaces)!r}, registry contains "
        f"{sorted(HARNESSES)!r}; derive the sweep from HARNESSES"
    )
    observations = assert_harness_credentials_stay_out_of_argv(surfaces, WRITE_TOKEN, tmp_path)
    assert_minimum_subjects(list(observations), 1, f"registered harness {sorted(surfaces)!r}")
    assert {observation.harness for observation in observations} == set(HARNESSES), (
        "authenticated runtime command discovery did not observe every registered harness; "
        "render and execute at least one authenticated command per harness"
    )
    discovered_commands = sum(
        len(_authenticated_shell_commands(rendered.content))
        for surface in surfaces.values()
        for rendered in surface.files
        if "`" in rendered.content
    )
    assert len(observations) == discovered_commands, (
        f"executed {len(observations)} of {discovered_commands} rendered curl commands; execute "
        "every discovered single-backtick command against the recording stub"
    )


def test_harness_runtime_sweep_rejects_multiline_argv_and_trace_leak(tmp_path: Path) -> None:
    # 2119: REQ-044.3.1
    rendered = RenderedFile(
        "future-harness",
        Path("skills/leaky.md"),
        'run `curl --header "Authorization: Bearer '
        '$PANOPTICON_SERVICE_AUTH_TOKEN" \\\nhttp://service/tasks` now',
    )
    surface = HarnessSurface((rendered,), ("future-agent",))
    with pytest.raises(AssertionError, match=r"future-harness.*skills/leaky\.md.*stdin"):
        assert_harness_credentials_stay_out_of_argv(
            {"future-harness": surface}, WRITE_TOKEN, tmp_path
        )


def test_harness_runtime_sweep_rejects_trace_only_leak(tmp_path: Path) -> None:
    # 2119: REQ-044.3.1
    rendered = RenderedFile(
        "future-harness",
        Path("skills/trace-leak.md"),
        "run `secret=$PANOPTICON_SERVICE_AUTH_TOKEN; curl --config - http://service`",
    )
    with pytest.raises(AssertionError, match=r"future-harness.*trace-leak.*xtrace") as exc:
        assert_harness_credentials_stay_out_of_argv(
            {"future-harness": HarnessSurface((rendered,), ("agent",))},
            WRITE_TOKEN,
            tmp_path,
        )
    assert "disable tracing around credential expansion and use stdin" in str(exc.value)


def test_harness_runtime_sweep_executes_each_discovered_span(tmp_path: Path) -> None:
    # 2119: REQ-044.3.1
    content = (
        "first `curl --config - http://service/one` and second `curl --config - http://service/two`"
    )
    rendered = RenderedFile("future-harness", Path("skills/two.md"), content)
    observations = assert_harness_credentials_stay_out_of_argv(
        {"future-harness": HarnessSurface((rendered,), ("agent",))}, WRITE_TOKEN, tmp_path
    )
    assert len(observations) == 2


def test_harness_command_discovery_honors_shell_token_boundaries() -> None:
    # 2119: REQ-044.3.1
    content = "`echo curl` `my-curl http://service` `true | curl http://service` ` curl /leading`"
    assert _authenticated_shell_commands(content) == [
        "true | curl http://service",
        " curl /leading",
    ]


@pytest.mark.parametrize("separator", [";", "&&", "||", "|"])
def test_harness_command_discovery_accepts_each_declared_separator(separator: str) -> None:
    # 2119: REQ-044.3.1
    command = f"true {separator} curl>out"
    assert _authenticated_shell_commands(f"`{command}`") == [command]


def test_registered_harness_files_use_credential_indirection(tmp_path: Path) -> None:
    # 2119: REQ-044.4.1
    # 2119: REQ-044.7.1
    surfaces = render_authenticated_harness_surfaces(tmp_path, WRITE_TOKEN)
    files = [rendered for surface in surfaces.values() for rendered in surface.files]
    argv = [argument for surface in surfaces.values() for argument in surface.argv]
    assert files, (
        f"harness sweep rendered no files for discovered registry subjects {sorted(HARNESSES)!r}"
    )
    for rendered in files:
        assert WRITE_TOKEN not in rendered.content, (
            f"{rendered.harness}: rendered credential value into {rendered.path}; use an "
            "environment-variable reference, stdin configuration, or credential-file path"
        )
    assert all(WRITE_TOKEN not in argument for argument in argv), (
        "a registered harness placed the credential in launch argv; pass only an environment "
        "reference or credential-file path"
    )
    for harness, surface in surfaces.items():
        assert any(
            re.search(
                r"(?:\$PANOPTICON_SERVICE_AUTH_TOKEN(?![A-Za-z0-9_])|\$\{PANOPTICON_SERVICE_AUTH_TOKEN\})",
                rendered.content,
            )
            for rendered in surface.files
        ), (
            f"{harness}: authenticated rendering contains no credential indirection reference; "
            "represent the production authentication channel without embedding its value"
        )


@pytest.mark.parametrize(
    "malformed",
    [
        "PANOPTICON_SERVICE_AUTH_TOKEN",
        "${PANOPTICON_SERVICE_AUTH_TOKEN",
        "$TOKEN",
        "$PANOPTICON_SERVICE_AUTH_TOKEN_SUFFIX",
    ],
)
def test_harness_indirection_rejects_near_miss_references(malformed: str) -> None:
    # 2119: REQ-044.4.1
    exact_reference = re.compile(
        r"(?:\$PANOPTICON_SERVICE_AUTH_TOKEN(?![A-Za-z0-9_])|\$\{PANOPTICON_SERVICE_AUTH_TOKEN\})"
    )
    assert exact_reference.search(malformed) is None


def test_production_environment_enforces_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-044.5.1
    app = _production_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/tasks").status_code == 401
        assert (
            client.get("/tasks", headers={"Authorization": f"Bearer {WRITE_TOKEN}"}).status_code
            == 200
        )
        assert (
            client.get(
                "/tasks", headers={"Authorization": "Bearer unknown-guard-token"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/tasks", headers={"Authorization": f"Bearer {WRITE_TOKEN}"}, json={}
            ).status_code
            == 422
        )


def test_query_credential_is_absent_from_production_composition_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 2119: REQ-044.6.1
    caplog.set_level(logging.INFO)
    app = _production_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.get("/tasks", params={"access_token": WRITE_TOKEN})
    assert response.status_code == 401
    messages = [record.getMessage() for record in caplog.records]
    assert messages, "log secrecy guard captured no service logs; repair capture before trusting it"
    assert any(
        record.name == "root"
        and "panopticon: task-service authentication mode" in record.getMessage()
        for record in caplog.records
    )
    assert all(WRITE_TOKEN not in message for message in messages), (
        "production-composed request logging persisted a query-string credential; redact query "
        "values or keep raw access logging disabled"
    )


def test_python_caller_limitation_and_compensating_check_are_documented() -> None:
    # 2119: REQ-044.8.1
    spec = Path("specs/REQ-044-standing-credential-boundary-guards.md").read_text()
    sentence = (
        "Static enumeration of hand-built Python HTTP callers is not a reliable security proof; "
        "shared TaskServiceClient tests are the compensating automated check."
    )
    assert sentence in spec.splitlines()

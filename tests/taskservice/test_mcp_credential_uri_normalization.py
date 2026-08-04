"""Executable contract for mcp-credential-uri-normalization.

`authorize_mcp_async` used to derive an artifact-read target from the *raw* request URI
string while FastMCP dispatches the request after parsing it into a Pydantic ``AnyUrl``
(``ReadResourceRequestParams.uri``) — a step that resolves ``.``/``..`` path segments. A
dot-segment traversal could therefore authorize against the capability's own task while
FastMCP actually served an unrelated one (see issue #202). These tests drive the *real*
mounted MCP app over a full session handshake and assert on the bytes actually returned,
not on the intermediate policy decision, so a future regression in that parity would be
caught here even if it slipped past a decision-only assertion.

Confirmed manually against fc04718e (pre-fix): the dot-segment traversal test below
returned HTTP 200 with the victim's artifact bytes; after the fix it returns 403.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.core.models import Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.auth import derive_task_capability
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Orchestrator, Spike

WRITE_TOKEN = "fleet-writer-token"
SCOPE_FAILURE = {"detail": "credential scope forbids operation"}


def _service(tmp_path: Path) -> TaskService:
    service = TaskService(
        SqlAlchemyStore(f"sqlite:///{tmp_path / 'task.db'}"),
        {"spike": Spike(), "orchestrator": Orchestrator()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/one", git_url="https://x/r1")))
    return service


def _credential_file(tmp_path: Path) -> str:
    secrets = tmp_path / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    path = secrets / "task-service-auth.json"
    path.write_text(json.dumps({"write": [WRITE_TOKEN]}))
    path.chmod(0o600)
    return path.name


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            _service(tmp_path),
            auth_file=_credential_file(tmp_path),
            auth_mode="enforced",
            secrets_dir=tmp_path / "secrets",
        )
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_task(
    client: TestClient, *, workflow: str = "spike", governor_task_id: str | None = None
) -> dict[str, object]:
    response = client.post(
        "/tasks",
        headers=_bearer(WRITE_TOKEN),
        json={"repo_id": "r1", "workflow": workflow, "governor_task_id": governor_task_id},
    )
    assert response.status_code == 201
    return response.json()


def _task_token(task_id: object) -> str:
    return derive_task_capability(WRITE_TOKEN, str(task_id))


def _mcp_headers(token: str) -> dict[str, str]:
    return {
        **_bearer(token),
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


def _parse_mcp_body(response: object) -> dict[str, object]:
    content_type = response.headers.get("content-type", "")  # type: ignore[attr-defined]
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():  # type: ignore[attr-defined]
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise AssertionError("no data: line in SSE response")
    return response.json()  # type: ignore[attr-defined]


def _asgi_denial(app: object, path: str, token: str) -> tuple[int, dict[str, object]]:
    """Drive a raw ASGI GET at ``path`` directly, bypassing httpx's client-side URL
    normalization (which otherwise collapses ``..`` segments before a request ever reaches the
    server, making a traversal payload indistinguishable from an ordinary request by the time
    the server sees it)."""
    sent: list[dict[str, object]] = []
    first = True

    async def receive() -> dict[str, object]:
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))  # type: ignore[operator]
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")  # type: ignore[arg-type]
        for message in sent
        if message["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(body)


def _real_resources_read(client: TestClient, token: str, uri: str) -> tuple[int, dict[str, object]]:
    """Drive a genuine MCP session handshake, then a raw ``resources/read`` for ``uri``.

    Unlike the rest of the credential-scoping suite (which asserts only on HTTP status for
    denied requests), a request that the scope policy *allows* is forwarded into the real
    mounted FastMCP app here, so a successful read exercises actual dispatch and returns the
    actual resource bytes — not just the authorization decision.
    """
    headers = _mcp_headers(token)
    init = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.0.1"},
            },
        },
    )
    session_id = init.headers.get("mcp-session-id")
    session_headers = dict(headers)
    if session_id:
        session_headers["Mcp-Session-Id"] = session_id
    client.post(
        "/mcp",
        headers=session_headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    read = client.post(
        "/mcp",
        headers=session_headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )
    return read.status_code, _parse_mcp_body(read)


def test_uppercase_scheme_reads_via_normalized_parsing_not_raw_matching(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.1.1
    # Every traversal-denial test in this file also denies under a naive raw-string match (the
    # tightened single-segment regex from 1.2 already rejects a raw multi-segment traversal
    # string on its own), so none of them by themselves prove authorization goes through actual
    # SDK normalization rather than skipping straight to raw regex matching. Scheme
    # case-folding is a case AnyUrl normalizes (``PANOPTICON://`` -> ``panopticon://``) that a
    # raw-string match against the lowercase-literal pattern would instead reject outright — and
    # real FastMCP dispatch normalizes the same way, so a real client sending this URI is served
    # successfully. A raw-string-only implementation would wrongly deny this legitimate read.
    with _client(tmp_path) as client:
        own = _create_task(client)
        token = _task_token(own["id"])
        client.put(
            f"/tasks/{own['id']}/artifacts/plan.md", headers=_bearer(token), content=b"OWN-BYTES"
        )
        uri = f"PANOPTICON://tasks/{own['id']}/artifacts/plan.md"
        status, body = _real_resources_read(client, token, uri)
        assert status == 200, body
        [content] = body["result"]["contents"]  # type: ignore[index]
        assert content["text"] == "OWN-BYTES"  # type: ignore[index]


def test_dot_segment_traversal_denied_by_real_mcp_dispatch(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.1.4
    # 2119: mcp-credential-uri-normalization.2.1
    with _client(tmp_path) as client:
        own = _create_task(client)
        victim = _create_task(client)
        token = _task_token(own["id"])
        client.put(
            f"/tasks/{own['id']}/artifacts/plan.md", headers=_bearer(token), content=b"OWN-BYTES"
        )
        client.put(
            f"/tasks/{victim['id']}/artifacts/plan.md",
            headers=_bearer(_task_token(victim["id"])),
            content=b"VICTIM-BYTES",
        )
        hostile_uri = (
            f"panopticon://tasks/{own['id']}/artifacts/../../{victim['id']}/artifacts/plan.md"
        )
        status, body = _real_resources_read(client, token, hostile_uri)
        assert status == 403
        assert body == SCOPE_FAILURE
        assert "VICTIM-BYTES" not in json.dumps(body)


def test_dot_segment_traversal_to_a_nonexistent_task_denied(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.1.4
    with _client(tmp_path) as client:
        own = _create_task(client)
        token = _task_token(own["id"])
        client.put(
            f"/tasks/{own['id']}/artifacts/plan.md", headers=_bearer(token), content=b"OWN-BYTES"
        )
        missing_id = uuid.uuid4().hex
        # The raw string names ``own`` (the capability's own task); the normalized form the SDK
        # actually dispatches on addresses a task id that was never created. The "missing" half
        # of 1.4's "unrelated or missing" disjunction must deny this exactly as it would deny an
        # existing-but-unrelated target, never falling back to serving ``own``'s own bytes.
        hostile_uri = (
            f"panopticon://tasks/{own['id']}/artifacts/../../{missing_id}/artifacts/plan.md"
        )
        status, body = _real_resources_read(client, token, hostile_uri)
        assert status == 403
        assert body == SCOPE_FAILURE
        assert "OWN-BYTES" not in json.dumps(body)


def test_task_scheme_dot_segment_traversal_denied(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.1.1
    # 2119: mcp-credential-uri-normalization.2.1
    with _client(tmp_path) as client:
        own = _create_task(client)
        victim = _create_task(client)
        token = _task_token(own["id"])
        hostile_uri = f"task://{own['id']}/artifacts/../../{victim['id']}/artifacts/plan.md"
        status, body = _real_resources_read(client, token, hostile_uri)
        assert status == 403
        assert body == SCOPE_FAILURE


def test_percent_encoded_dot_segment_traversal_denied(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.1.3
    # 2119: mcp-credential-uri-normalization.2.1
    with _client(tmp_path) as client:
        own = _create_task(client)
        victim = _create_task(client)
        token = _task_token(own["id"])
        hostile_uri = (
            f"panopticon://tasks/{own['id']}/artifacts/%2e%2e/{victim['id']}/artifacts/plan.md"
        )
        status, body = _real_resources_read(client, token, hostile_uri)
        assert status == 403
        assert body == SCOPE_FAILURE


def test_percent_encoded_slash_separator_traversal_denied(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.2.1
    # Distinct from the encoded-dots case above: this encodes the path *separators*
    # themselves (``%2F`` for ``/``) rather than the dots, so the traversal segments arrive as
    # one opaque, un-decoded run of characters rather than real ``..`` segments AnyUrl would
    # resolve. It must still be denied rather than treated as a single legitimate name/id.
    with _client(tmp_path) as client:
        own = _create_task(client)
        victim = _create_task(client)
        token = _task_token(own["id"])
        hostile_uri = (
            f"panopticon://tasks/{own['id']}/artifacts/..%2F..%2F{victim['id']}/artifacts/plan.md"
        )
        status, body = _real_resources_read(client, token, hostile_uri)
        assert status == 403
        assert body == SCOPE_FAILURE


def test_unicode_confusable_separator_traversal_denied(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.2.1
    with _client(tmp_path) as client:
        own = _create_task(client)
        victim = _create_task(client)
        token = _task_token(own["id"])
        # U+FF0F FULLWIDTH SOLIDUS and U+2024 ONE DOT LEADER: neither is treated as a literal
        # ``.``/``/`` by URI normalization, so this must land on ``own`` (its only real
        # segment boundary) and 403 as a self-scoped-but-malformed request, never on victim.
        hostile_uri = (
            f"panopticon://tasks/{own['id']}/artifacts/․․／{victim['id']}/artifacts/plan.md"
        )
        status, body = _real_resources_read(client, token, hostile_uri)
        assert status == 403
        assert body == SCOPE_FAILURE


def test_artifact_name_cannot_span_a_path_separator(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.1.2
    # 2119: mcp-credential-uri-normalization.2.1
    with _client(tmp_path) as client:
        own = _create_task(client)
        token = _task_token(own["id"])
        # No dot segments at all: a literal extra ``/`` inside what would be the artifact-name
        # capture. The pre-fix ``(.+)`` name group spanned this and authorized it as self; the
        # real FastMCP template can only ever match a single path segment for {name}, so this
        # must now be denied rather than silently authorizing a request dispatch can't serve.
        uri = f"panopticon://tasks/{own['id']}/artifacts/sub/plan.md"
        status, body = _real_resources_read(client, token, uri)
        assert status == 403
        assert body == SCOPE_FAILURE


def test_task_id_cannot_span_a_path_separator(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.1.2
    # 2119: mcp-credential-uri-normalization.2.1
    with _client(tmp_path) as client:
        own = _create_task(client)
        token = _task_token(own["id"])
        # A literal extra ``/`` inside what would be the task-id capture, mirroring the
        # artifact-name case above: the requirement demands the task id, not just the name,
        # come from exactly one path segment. Neither FastMCP's own template (which applies
        # the same single-segment rule to every named parameter) nor our policy can serve
        # this, so it must deny rather than resolve to some multi-segment task id.
        uri = f"panopticon://tasks/{own['id']}/sub/artifacts/plan.md"
        status, body = _real_resources_read(client, token, uri)
        assert status == 403
        assert body == SCOPE_FAILURE


def test_normalized_artifact_target_requires_single_segment_ids_and_names() -> None:
    # 2119: mcp-credential-uri-normalization.1.2
    # A real task's id never contains a literal ``/`` (ids are opaque uuid4 hex), so a URI whose
    # task-id capture spans an extra segment can never coincide with an existing task's id — an
    # end-to-end request denies either way, which cannot by itself distinguish a correctly
    # single-segment task-id capture from a widened one. This asserts directly on the parser's
    # output instead, so a widened capture (e.g. ``(.+)`` in place of ``([^/]+)``) is caught even
    # though it would still end up denied downstream.
    from panopticon.taskservice.auth_scope import _normalized_artifact_target

    assert _normalized_artifact_target("panopticon://tasks/OWNID/artifacts/plan.md") == (
        "OWNID",
        "plan.md",
    )
    assert _normalized_artifact_target("panopticon://tasks/OWNID/sub/artifacts/plan.md") == (
        "",
        "",
    )
    assert _normalized_artifact_target("panopticon://tasks/OWNID/artifacts/sub/plan.md") == (
        "",
        "",
    )
    # "Exactly one *nonempty* segment": an empty task-id or name segment must also fail to
    # match, not just a multi-segment one — a capture widened to allow zero characters (e.g.
    # ``[^/]*`` in place of ``[^/]+``) would otherwise satisfy every assertion above while still
    # violating this requirement.
    assert _normalized_artifact_target("panopticon://tasks//artifacts/plan.md") == ("", "")
    assert _normalized_artifact_target("panopticon://tasks/OWNID/artifacts/") == ("", "")


def test_own_artifact_with_punctuation_name_returns_exact_bytes_via_real_dispatch(
    tmp_path: Path,
) -> None:
    # 2119: mcp-credential-uri-normalization.1.1
    # 2119: mcp-credential-uri-normalization.1.2
    # 2119: mcp-credential-uri-normalization.2.2
    with _client(tmp_path) as client:
        own = _create_task(client)
        token = _task_token(own["id"])
        # Punctuation mid-string alone wouldn't catch a decode step or traversal guard that only
        # misbehaves right at the segment boundary, so these sit a literal ``%`` and a leading
        # ``.`` immediately after the ``/artifacts/`` boundary, and a bare ``~`` as the entire
        # trailing segment (no alphanumeric buffer before the closing boundary at all).
        boundary_names = (
            ".50%~leading-dot-and-percent.md",
            "trailing-special~",
            "_leading-underscore.md",
            "+leading-plus.md",
        )
        for name in boundary_names:
            content = f"BYTES-FOR-{name}".encode()
            put = client.put(
                f"/tasks/{own['id']}/artifacts/{name}",
                headers=_bearer(token),
                content=content,
            )
            assert put.status_code == 204, (name, put.text)
            uri = f"panopticon://tasks/{own['id']}/artifacts/{name}"
            status, body = _real_resources_read(client, token, uri)
            assert status == 200, (name, body)
            [returned] = body["result"]["contents"]  # type: ignore[index]
            assert returned["text"] == content.decode()  # type: ignore[index]


def test_rest_artifact_route_still_denies_equivalent_traversal(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.2.3
    # httpx (the test client) collapses ``..`` segments client-side before the request is even
    # sent, per ordinary URL-parsing rules — so a ``client.get(".../../victim/...")`` call
    # reaches the server as an already-resolved ordinary path, never proving the *server's* own
    # route matching still rejects it. ``_asgi_denial`` drives the raw ASGI app directly with
    # the literal, unresolved path so this genuinely exercises server-side matching.
    with _client(tmp_path) as client:
        own = _create_task(client)
        victim = _create_task(client)
        token = _task_token(own["id"])
        status, body = _asgi_denial(
            client.app,
            f"/tasks/{own['id']}/artifacts/../../{victim['id']}/artifacts/plan.md",
            token,
        )
        assert (status, body) == (403, SCOPE_FAILURE)


def test_rest_artifact_route_denies_a_raw_multi_segment_name_server_side(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.2.3
    # Mirrors the MCP-side ``test_artifact_name_cannot_span_a_path_separator``: a literal extra
    # ``/`` in the artifact-name position, with no dot segments at all (so there is nothing for
    # any client-side URL normalization to collapse), driven at the raw ASGI layer to prove the
    # REST route's ``{name}`` template still matches only a single path segment server-side.
    with _client(tmp_path) as client:
        own = _create_task(client)
        token = _task_token(own["id"])
        status, body = _asgi_denial(client.app, f"/tasks/{own['id']}/artifacts/sub/plan.md", token)
        assert (status, body) == (403, SCOPE_FAILURE)


def test_rest_artifact_route_object_itself_requires_a_single_segment_name(
    tmp_path: Path,
) -> None:
    # 2119: mcp-credential-uri-normalization.2.3
    # The task-capability auth middleware runs its own independent route matcher ahead of
    # dispatch and would deny a multi-segment name regardless of what the *actual* FastAPI
    # route accepts, so a request-level test alone can't prove the registered
    # ``GET /tasks/{task_id}/artifacts/{name}`` route's own pattern is unchanged (only that
    # *something* upstream of it still denies). This calls the real ``starlette.routing.Route``
    # object's own ``matches`` directly, with no auth layer involved at all, to prove {name}
    # still uses the default single-segment string convertor rather than a ``:path`` one.
    from starlette.routing import Match

    with _client(tmp_path) as client:
        route = next(
            candidate
            for candidate in client.app.routes
            if getattr(candidate, "path", "").startswith("/tasks/{task_id}/artifacts/{name")
            and "GET" in getattr(candidate, "methods", set())
        )
        multi_segment, _ = route.matches(
            {"type": "http", "method": "GET", "path": "/tasks/OWNID/artifacts/sub/plan.md"}
        )
        assert multi_segment == Match.NONE
        single_segment, _ = route.matches(
            {"type": "http", "method": "GET", "path": "/tasks/OWNID/artifacts/plan.md"}
        )
        assert single_segment == Match.FULL


def test_rest_artifact_route_still_serves_legitimate_reads(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.2.3
    # A denial-only assertion can't distinguish "REST's matching is unchanged" from "REST's
    # matching became stricter" (e.g. a route template accidentally narrowed while chasing MCP
    # parity). This proves ordinary REST artifact reads — a plain name and a punctuation-bearing
    # one — still succeed with the exact stored bytes.
    with _client(tmp_path) as client:
        own = _create_task(client)
        token = _task_token(own["id"])
        for name, content in (
            ("plan.md", b"PLAIN-BYTES"),
            (".50%~leading-dot-and-percent.md", b"PUNCTUATION-BYTES"),
        ):
            put = client.put(
                f"/tasks/{own['id']}/artifacts/{name}", headers=_bearer(token), content=content
            )
            assert put.status_code == 204, (name, put.text)
            read = client.get(f"/tasks/{own['id']}/artifacts/{name}", headers=_bearer(token))
            assert read.status_code == 200, (name, read.text)
            assert read.content == content


def test_set_dependencies_rest_denies_unrelated_and_missing_ids_identically(
    tmp_path: Path,
) -> None:
    # 2119: mcp-credential-uri-normalization.3.1
    # 2119: mcp-credential-uri-normalization.3.2
    with _client(tmp_path) as client:
        own = _create_task(client)
        unrelated = _create_task(client)
        token = _task_token(own["id"])
        missing_id = uuid.uuid4().hex
        unrelated_response = client.put(
            f"/tasks/{own['id']}/dependencies",
            headers=_bearer(token),
            json={"dep_ids": [unrelated["id"]]},
        )
        missing_response = client.put(
            f"/tasks/{own['id']}/dependencies",
            headers=_bearer(token),
            json={"dep_ids": [missing_id]},
        )
        assert (unrelated_response.status_code, unrelated_response.json()) == (403, SCOPE_FAILURE)
        assert (missing_response.status_code, missing_response.json()) == (403, SCOPE_FAILURE)


def test_set_dependencies_mcp_denies_unrelated_and_missing_ids_identically(
    tmp_path: Path,
) -> None:
    # 2119: mcp-credential-uri-normalization.3.1
    # 2119: mcp-credential-uri-normalization.3.2
    with _client(tmp_path) as client:
        own = _create_task(client)
        unrelated = _create_task(client)
        token = _task_token(own["id"])
        missing_id = uuid.uuid4().hex
        for dep_id in (unrelated["id"], missing_id):
            call = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "set_dependencies",
                    "arguments": {"task_id": own["id"], "dep_ids": [dep_id]},
                },
            }
            response = client.post("/mcp", headers=_bearer(token), json=call)
            assert (response.status_code, response.json()) == (403, SCOPE_FAILURE)


def test_set_dependencies_allows_a_governed_descendant_as_dependency(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.3.1
    with _client(tmp_path) as client:
        orchestrator = _create_task(client, workflow="orchestrator")
        child_a = _create_task(client, governor_task_id=str(orchestrator["id"]))
        child_b = _create_task(client, governor_task_id=str(orchestrator["id"]))
        token = _task_token(orchestrator["id"])
        response = client.put(
            f"/tasks/{child_a['id']}/dependencies",
            headers=_bearer(token),
            json={"dep_ids": [child_b["id"]]},
        )
        assert response.status_code == 200
        assert response.json()["depends_on_task_ids"] == [child_b["id"]]


def test_set_dependencies_denies_when_any_id_in_a_mixed_list_is_out_of_scope(
    tmp_path: Path,
) -> None:
    # 2119: mcp-credential-uri-normalization.3.1
    # The requirement scopes "every nonempty proposed dependency id" — a single-element list
    # can't tell an implementation that checks every id apart from one that only checks the
    # first. This mixes one in-scope id with one out-of-scope id in both orders, so an
    # implementation that stopped after the first (in-scope) id would wrongly allow the second
    # case here.
    with _client(tmp_path) as client:
        orchestrator = _create_task(client, workflow="orchestrator")
        child_a = _create_task(client, governor_task_id=str(orchestrator["id"]))
        child_b = _create_task(client, governor_task_id=str(orchestrator["id"]))
        unrelated = _create_task(client)
        token = _task_token(orchestrator["id"])
        for dep_ids in ([child_b["id"], unrelated["id"]], [unrelated["id"], child_b["id"]]):
            response = client.put(
                f"/tasks/{child_a['id']}/dependencies",
                headers=_bearer(token),
                json={"dep_ids": dep_ids},
            )
            assert (response.status_code, response.json()) == (403, SCOPE_FAILURE), dep_ids


def test_set_dependencies_denies_a_short_nonempty_out_of_scope_id(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.3.1
    # "Every *nonempty* proposed dependency id" governs any nonempty string, not just
    # id-shaped ones — an implementation that special-cased short strings (e.g. exempting
    # anything under some length threshold, mistaking it for a sentinel) would wrongly skip
    # the scope check for a value like this while still enforcing it for id-shaped strings.
    with _client(tmp_path) as client:
        orchestrator = _create_task(client, workflow="orchestrator")
        child_a = _create_task(client, governor_task_id=str(orchestrator["id"]))
        token = _task_token(orchestrator["id"])
        response = client.put(
            f"/tasks/{child_a['id']}/dependencies",
            headers=_bearer(token),
            json={"dep_ids": ["x"]},
        )
        assert (response.status_code, response.json()) == (403, SCOPE_FAILURE)


def test_denied_set_dependencies_leaves_persisted_list_unchanged(tmp_path: Path) -> None:
    # 2119: mcp-credential-uri-normalization.3.3
    with _client(tmp_path) as client:
        orchestrator = _create_task(client, workflow="orchestrator")
        child_a = _create_task(client, governor_task_id=str(orchestrator["id"]))
        child_b = _create_task(client, governor_task_id=str(orchestrator["id"]))
        unrelated = _create_task(client)
        orchestrator_token = _task_token(orchestrator["id"])
        baseline = client.put(
            f"/tasks/{child_a['id']}/dependencies",
            headers=_bearer(orchestrator_token),
            json={"dep_ids": [child_b["id"]]},
        )
        assert baseline.status_code == 200

        denied = client.put(
            f"/tasks/{child_a['id']}/dependencies",
            headers=_bearer(orchestrator_token),
            json={"dep_ids": [unrelated["id"]]},
        )
        assert denied.status_code == 403

        current = client.get(f"/tasks/{child_a['id']}", headers=_bearer(orchestrator_token))
        assert current.json()["depends_on_task_ids"] == [child_b["id"]]

"""REST API contract tests via FastAPI's TestClient."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.core.models import Repo, Responsibility
from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


class _GatedWorkflow(Workflow):
    """WORKING (agent) carries a responsibility gating the handoff to COMPLETE."""

    name = "gated"

    class Working(InitialState):
        label = "WORKING"
        responsibilities = (Responsibility(key="tests-pass", description="Tests pass"),)
        transitions = (Complete,)

    initial = Working


class _TunedWorkflow(Workflow):
    name = "tuned"
    default_harness = "codex"
    default_model = "gpt-5.6-sol:high"

    class Working(InitialState):
        label = "WORKING"
        transitions = (Complete,)

    initial = Working


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    service = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git")))
    with TestClient(create_app(service)) as c:
        yield c


def _new_task(client: TestClient) -> str:
    resp = client.post("/tasks", json={"repo_id": "r1", "workflow": "spike"})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def test_health_and_workflows(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/workflows").json() == [
        {
            "name": "spike",
            "when_to_use": Spike().when_to_use,
            "opt_in": False,
        }
    ]


def test_stale_persisted_state_remains_listable_after_workflow_code_changes(
    tmp_path: Path,
) -> None:
    class BeforeRename(Workflow):
        name = "versioned"

        class RenamedAway(InitialState):
            label = "RENAMED_AWAY"
            transitions = (Complete,)

        initial = RenamedAway

    class AfterRename(Workflow):
        name = "versioned"

        class Current(InitialState):
            label = "CURRENT"
            transitions = (Complete,)

        initial = Current

    store = SqlAlchemyStore()
    old_service = TaskService(
        store,
        {"versioned": BeforeRename()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(old_service.init())
    asyncio.run(
        old_service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    )
    task = asyncio.run(old_service.create_task("r1", "versioned"))

    new_service = TaskService(
        store,
        {"versioned": AfterRename()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(new_service.init())

    with TestClient(create_app(new_service)) as stale_client:
        listed = stale_client.get("/tasks")
        assert listed.status_code == 200
        assert listed.json()[0]["state"] == "RENAMED_AWAY"
        assert listed.json()[0]["container_status"] == "queued"

        active = stale_client.get("/tasks", params={"terminal": "false"})
        assert active.status_code == 200
        assert [item["id"] for item in active.json()] == [task.id]


def test_repo_workflows_endpoint_filters_by_opt_in(tmp_path: Path) -> None:
    from panopticon.workflows import GithubSelfReviewed

    svc = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike(), "github-self-reviewed": GithubSelfReviewed()},
        FilesystemArtifactStore(tmp_path),
    )
    import asyncio

    asyncio.run(svc.init())
    asyncio.run(svc.create_repo(Repo(id="r1", name="acme", git_url="https://x/r1.git")))
    asyncio.run(
        svc.create_repo(
            Repo(
                id="r2",
                name="acme2",
                git_url="https://x/r2.git",
                enabled_workflows=["github-self-reviewed"],
            )
        )
    )

    with TestClient(create_app(svc)) as c:
        # r1: no enabled_workflows → opt-out only (spike); opt-in github-self-reviewed hidden
        r1_names = {w["name"] for w in c.get("/repos/r1/workflows").json()}
        assert "spike" in r1_names
        assert "github-self-reviewed" not in r1_names

        # r2: github-self-reviewed explicitly enabled
        r2_names = {w["name"] for w in c.get("/repos/r2/workflows").json()}
        assert "spike" in r2_names
        assert "github-self-reviewed" in r2_names


def test_workflow_image_layer_endpoint(client: TestClient) -> None:
    # spike needs no layer (empty); the runner composes this onto the base image (ADR 0005).
    assert client.get("/workflows/spike/image-layer").json() == {"layer": ""}


def test_workflow_image_layer_surfaces_github_peer_revieweds_gh_layer(tmp_path: Path) -> None:
    from panopticon.workflows import GithubPeerReviewed

    svc = TaskService(
        SqlAlchemyStore(),
        {"github-peer-reviewed": GithubPeerReviewed()},
        FilesystemArtifactStore(tmp_path),
    )
    with TestClient(create_app(svc)) as c:
        assert (
            "gh" in c.get("/workflows/github-peer-reviewed/image-layer").json()["layer"]
        )  # forge skills need gh


def _repo_layer_client(tmp_path: Path, *, image_layer_file: str | None) -> TestClient:
    from panopticon.taskservice.layers_fs import FilesystemLayerStore

    layers = tmp_path / "layers"
    layers.mkdir()
    (layers / "r1.layer").write_text("RUN pip install uv")
    svc = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
        layers=FilesystemLayerStore(layers),
    )
    asyncio.run(svc.init())
    asyncio.run(
        svc.create_repo(
            Repo(
                id="r1", name="acme", git_url="https://x/r1.git", image_layer_file=image_layer_file
            )
        )
    )
    return TestClient(create_app(svc))


def test_repo_image_layer_endpoint_reads_the_referenced_file(tmp_path: Path) -> None:
    # image_layer_file references a file under the layers dir; the endpoint serves its content.
    with _repo_layer_client(tmp_path, image_layer_file="r1.layer") as c:
        assert c.get("/repos/r1/image-layer").json() == {"layer": "RUN pip install uv"}


def test_repo_image_layer_empty_when_unset(tmp_path: Path) -> None:
    with _repo_layer_client(tmp_path, image_layer_file=None) as c:
        assert c.get("/repos/r1/image-layer").json() == {"layer": ""}  # no repo layer declared


def test_repo_image_layer_missing_file_404(tmp_path: Path) -> None:
    with _repo_layer_client(tmp_path, image_layer_file="absent.layer") as c:
        assert c.get("/repos/r1/image-layer").status_code == 404  # configured but no such file


def test_repo_image_layer_unknown_repo_404(tmp_path: Path) -> None:
    with _repo_layer_client(tmp_path, image_layer_file=None) as c:
        assert c.get("/repos/ghost/image-layer").status_code == 404


def test_create_repo_with_a_missing_env_file_is_400(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # env_file is a name under the secrets dir (#291); an unresolvable name is a 400 at create.
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    resp = client.post(
        "/repos",
        json={
            "id": "r2",
            "name": "acme/other",
            "git_url": "https://x/r2.git",
            "env_file": "absent.env",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "env_file" in resp.json()["detail"]


def test_create_repo_with_an_existing_env_file_is_201(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "r2.env").write_text("ANTHROPIC_API_KEY=sk-test\n")
    resp = client.post(
        "/repos",
        json={
            "id": "r2",
            "name": "acme/other",
            "git_url": "https://x/r2.git",
            "env_file": "r2.env",
        },
    )
    assert resp.status_code == 201, resp.text


def test_mcp_is_mounted(client: TestClient) -> None:
    # The MCP streamable-HTTP app is mounted at /mcp (in-container agents connect there); it must
    # be a mount, not a REST route, and reachable (i.e. not a REST 404).
    assert any(getattr(r, "path", None) == "/mcp" for r in client.app.routes)
    resp = client.get("/mcp/", headers={"Accept": "text/event-stream"})
    assert resp.status_code != 404


def test_create_and_get_task(client: TestClient) -> None:
    task_id = _new_task(client)
    got = client.get(f"/tasks/{task_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["state"] == "ITERATING"
    assert body["turn"] == "user"  # spike's initial state → turn starts with the user
    assert body["slug"] is None
    assert body["memo"] is None  # none given at creation
    assert body["provisioned"] is False  # no branch yet (computed Task.provisioned)
    assert [h["to_state"] for h in body["history"]] == ["ITERATING"]


def test_create_task_records_the_memo(client: TestClient) -> None:
    resp = client.post(
        "/tasks", json={"repo_id": "r1", "workflow": "spike", "memo": "make it green"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["memo"] == "make it green"
    got = client.get(f"/tasks/{resp.json()['id']}")  # and it survives a reload
    assert got.json()["memo"] == "make it green"


def test_get_missing_task_404(client: TestClient) -> None:
    assert client.get("/tasks/ghost").status_code == 404


def test_create_task_unknown_workflow_400(client: TestClient) -> None:
    resp = client.post("/tasks", json={"repo_id": "r1", "workflow": "nope"})
    assert resp.status_code == 400


def test_create_task_records_the_harness(client: TestClient) -> None:
    resp = client.post("/tasks", json={"repo_id": "r1", "workflow": "spike", "harness": "codex"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["harness"] == "codex"
    got = client.get(f"/tasks/{resp.json()['id']}")  # and it survives a reload
    assert got.json()["harness"] == "codex"


def test_create_task_materializes_the_app_default_harness(client: TestClient) -> None:
    task_id = _new_task(client)
    assert client.get(f"/tasks/{task_id}").json()["harness"] == "claude"


def test_repo_default_harness_flows_to_new_tasks(client: TestClient, tmp_path: Path) -> None:
    # The on-the-rails path: the repo names the harness once; task creation never touches it.
    resp = client.post(
        "/repos",
        json={
            "id": "r2",
            "name": "acme/other",
            "git_url": "https://x/r2.git",
            "default_harness": "claude",
        },
    )
    assert resp.status_code == 201, resp.text
    task = client.post("/tasks", json={"repo_id": "r2", "workflow": "spike"}).json()
    assert task["harness"] == "claude"  # resolved at creation and recorded on the task


def test_repo_without_a_default_harness_materializes_the_system_default(
    client: TestClient,
) -> None:
    task_id = _new_task(client)
    assert client.get(f"/tasks/{task_id}").json()["harness"] == "claude"


def test_create_repo_with_an_unknown_default_harness_is_400(client: TestClient) -> None:
    resp = client.post(
        "/repos",
        json={
            "id": "r2",
            "name": "acme/other",
            "git_url": "https://x/r2.git",
            "default_harness": "cursor",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "cursor" in resp.json()["detail"]


def test_patch_repo_validates_the_default_harness(client: TestClient) -> None:
    assert (
        client.patch("/repos/r1", json={"default_harness": "cursor"}).status_code == 400
    )  # unknown name rejected on update too
    resp = client.patch("/repos/r1", json={"default_harness": "claude"})
    assert resp.status_code == 200 and resp.json()["default_harness"] == "claude"


def test_repo_default_model_is_opaque_and_patchable(client: TestClient) -> None:
    value = "operator-owned vocabulary:maximum"
    harness = client.patch("/repos/r1", json={"default_harness": "claude"})
    assert harness.status_code == 200, harness.text
    resp = client.patch("/repos/r1", json={"default_model": value})
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_model"] == value
    assert client.get("/repos/r1").json()["default_model"] == value
    # 2119: REQ-012.4.1
    task = client.post("/tasks", json={"repo_id": "r1", "workflow": "spike"}).json()
    assert task["starting_model"] == value


# 2119: REQ-012.2.4
def test_app_default_harness_is_materialized_on_the_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import panopticon.harnesses as harness_registry

    monkeypatch.setattr(harness_registry, "DEFAULT_HARNESS", "codex")
    task_id = _new_task(client)
    task = client.get(f"/tasks/{task_id}").json()
    assert (task["harness"], task["starting_model"]) == ("codex", None)


# 2119: REQ-012.4.3
def test_materialized_app_default_survives_later_default_changes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import panopticon.harnesses as harness_registry

    task_id = _new_task(client)
    monkeypatch.setattr(harness_registry, "DEFAULT_HARNESS", "codex")
    later_task_id = _new_task(client)
    assert client.get(f"/tasks/{task_id}").json()["harness"] == "claude"
    assert client.get(f"/tasks/{later_task_id}").json()["harness"] == "codex"


# 2119: REQ-012.5.1
def test_repo_model_requires_an_explicit_repo_harness(client: TestClient) -> None:
    create = client.post(
        "/repos",
        json={
            "id": "r2",
            "name": "acme/other",
            "git_url": "https://x/r2.git",
            "default_model": "opus",
        },
    )
    assert create.status_code == 400, create.text
    patch = client.patch("/repos/r1", json={"default_model": "opus"})
    assert patch.status_code == 400, patch.text
    configured = client.patch(
        "/repos/r1", json={"default_harness": "claude", "default_model": "opus"}
    )
    assert configured.status_code == 200, configured.text
    clear_harness = client.patch("/repos/r1", json={"default_harness": None})
    assert clear_harness.status_code == 400, clear_harness.text


# 2119: REQ-012.3.1
def test_explicit_task_harness_beats_app_defaults(client: TestClient) -> None:
    resp = client.post("/tasks", json={"repo_id": "r1", "workflow": "spike", "harness": "codex"})
    assert (resp.json()["harness"], resp.json()["starting_model"]) == ("codex", None)
    client.post(
        "/repos",
        json={
            "id": "r2",
            "name": "acme/other",
            "git_url": "https://x/r2.git",
            "default_harness": "claude",
            "default_model": "opus",
        },
    )
    against_non_null_default = client.post(
        "/tasks", json={"repo_id": "r2", "workflow": "spike", "harness": "codex"}
    )
    assert against_non_null_default.json()["harness"] == "codex"


# 2119: REQ-012.2.2
def test_repo_launch_pair_beats_app_defaults(client: TestClient) -> None:
    client.post(
        "/repos",
        json={
            "id": "r3",
            "name": "acme/three",
            "git_url": "https://x/r3.git",
            "default_harness": "codex",
            "default_model": "gpt-5.6-sol:medium",
        },
    )
    resp = client.post("/tasks", json={"repo_id": "r3", "workflow": "spike"})
    assert (resp.json()["harness"], resp.json()["starting_model"]) == (
        "codex",
        "gpt-5.6-sol:medium",
    )


# 2119: REQ-012.2.1
def test_workflow_launch_pair_beats_repo_pair(tmp_path: Path) -> None:
    service = TaskService(
        SqlAlchemyStore(), {"tuned": _TunedWorkflow()}, FilesystemArtifactStore(tmp_path)
    )
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(
            Repo(
                id="r1",
                name="acme/widgets",
                git_url="https://x/r1.git",
                default_harness="claude",
                default_model="opus:low",
            )
        )
    )
    with TestClient(create_app(service)) as tuned_client:
        task = tuned_client.post("/tasks", json={"repo_id": "r1", "workflow": "tuned"}).json()
    assert (task["harness"], task["starting_model"]) == ("codex", "gpt-5.6-sol:high")


# 2119: REQ-012.3.3
def test_explicit_task_harness_drops_losing_pair_model(tmp_path: Path) -> None:
    service = TaskService(
        SqlAlchemyStore(), {"tuned": _TunedWorkflow()}, FilesystemArtifactStore(tmp_path)
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme", git_url="https://x/r1.git")))
    with TestClient(create_app(service)) as tuned_client:
        task = tuned_client.post(
            "/tasks", json={"repo_id": "r1", "workflow": "tuned", "harness": "claude"}
        ).json()
    assert (task["harness"], task["starting_model"]) == ("claude", None)


# 2119: REQ-012.3.2
def test_explicit_task_model_keeps_winning_pair_harness(tmp_path: Path) -> None:
    service = TaskService(
        SqlAlchemyStore(), {"tuned": _TunedWorkflow()}, FilesystemArtifactStore(tmp_path)
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme", git_url="https://x/r1.git")))
    with TestClient(create_app(service)) as tuned_client:
        task = tuned_client.post(
            "/tasks", json={"repo_id": "r1", "workflow": "tuned", "starting_model": "custom"}
        ).json()
    assert (task["harness"], task["starting_model"]) == ("codex", "custom")


# 2119: REQ-012.3.4
def test_explicit_same_harness_keeps_winning_pair_model(tmp_path: Path) -> None:
    service = TaskService(
        SqlAlchemyStore(), {"tuned": _TunedWorkflow()}, FilesystemArtifactStore(tmp_path)
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme", git_url="https://x/r1.git")))
    with TestClient(create_app(service)) as tuned_client:
        task = tuned_client.post(
            "/tasks", json={"repo_id": "r1", "workflow": "tuned", "harness": "codex"}
        ).json()
    assert (task["harness"], task["starting_model"]) == ("codex", "gpt-5.6-sol:high")


# 2119: REQ-012.4.2
def test_created_tasks_keep_launch_values_after_defaults_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike(), "tuned": _TunedWorkflow()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(
            Repo(
                id="r1",
                name="acme",
                git_url="https://x/r1.git",
                default_harness="claude",
                default_model="opus:low",
            )
        )
    )
    with TestClient(create_app(service)) as tuned_client:
        repo_task = tuned_client.post("/tasks", json={"repo_id": "r1", "workflow": "spike"}).json()
        workflow_task = tuned_client.post(
            "/tasks", json={"repo_id": "r1", "workflow": "tuned"}
        ).json()
        tuned_client.patch(
            "/repos/r1",
            json={"default_harness": "codex", "default_model": "replacement"},
        )
        monkeypatch.setattr(_TunedWorkflow, "default_harness", "claude")
        monkeypatch.setattr(_TunedWorkflow, "default_model", "replacement")

        persisted_repo_task = tuned_client.get(f"/tasks/{repo_task['id']}").json()
        persisted_workflow_task = tuned_client.get(f"/tasks/{workflow_task['id']}").json()

    assert (persisted_repo_task["harness"], persisted_repo_task["starting_model"]) == (
        "claude",
        "opus:low",
    )
    assert (persisted_workflow_task["harness"], persisted_workflow_task["starting_model"]) == (
        "codex",
        "gpt-5.6-sol:high",
    )


def test_create_task_records_an_explicit_starting_model(client: TestClient) -> None:
    resp = client.post(
        "/tasks",
        json={
            "repo_id": "r1",
            "workflow": "spike",
            "harness": "codex",
            "starting_model": "gpt-5.6-sol",
        },
    )
    assert resp.json()["starting_model"] == "gpt-5.6-sol"


def test_create_repo_with_a_missing_credential_dir_is_400(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # credential_dir is a directory name under the secrets dir (the sibling of env_file); an
    # unresolvable name is a 400 at create rather than an obscure mount failure at spawn.
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    resp = client.post(
        "/repos",
        json={
            "id": "r2",
            "name": "acme/other",
            "git_url": "https://x/r2.git",
            "credential_dir": "absent.d",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "credential_dir" in resp.json()["detail"]


def test_create_repo_with_an_existing_credential_dir_round_trips(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    (tmp_path / "secrets" / "openai.d").mkdir(parents=True)
    resp = client.post(
        "/repos",
        json={
            "id": "r2",
            "name": "acme/other",
            "git_url": "https://x/r2.git",
            "credential_dir": "openai.d",
        },
    )
    assert resp.status_code == 201, resp.text
    assert client.get("/repos/r2").json()["credential_dir"] == "openai.d"


def test_create_task_unknown_harness_400(client: TestClient) -> None:
    resp = client.post("/tasks", json={"repo_id": "r1", "workflow": "spike", "harness": "cursor"})
    assert resp.status_code == 400
    assert "cursor" in resp.json()["detail"]  # the error names the offender (and the known set)


def test_create_task_missing_repo_404(client: TestClient) -> None:
    resp = client.post("/tasks", json={"repo_id": "ghost", "workflow": "spike"})
    assert resp.status_code == 404


def test_list_legal_transitions(client: TestClient) -> None:
    task_id = _new_task(client)
    resp = client.get(f"/tasks/{task_id}/transitions")
    assert resp.status_code == 200
    assert resp.json() == ["COMPLETE", "DROPPED"]  # spike ITERATING, sorted


def test_list_and_apply_operations(client: TestClient) -> None:
    task_id = _new_task(client)  # spike ITERATING
    ops = client.get(f"/tasks/{task_id}/operations")
    assert ops.json() == {
        "advance": "COMPLETE",
        "drop": "DROPPED",
    }  # advance derived, drop implicit

    advanced = client.post(f"/tasks/{task_id}/operations/advance")
    assert advanced.status_code == 200
    assert advanced.json()["state"] == "COMPLETE"
    assert advanced.json()["history"][-1]["trigger"] == "advance"  # the verb is the trigger


def test_apply_unavailable_operation_409(client: TestClient) -> None:
    task_id = _new_task(client)
    resp = client.post(f"/tasks/{task_id}/operations/iterate")  # spike offers no iterate
    assert resp.status_code == 409


def test_list_states(client: TestClient) -> None:
    task_id = _new_task(client)
    assert set(client.get(f"/tasks/{task_id}/states").json()) == {
        "ITERATING",
        "COMPLETE",
        "DROPPED",
    }


def test_list_skills_is_just_provision_for_a_forgeless_workflow(client: TestClient) -> None:
    task_id = _new_task(client)
    resp = client.get(f"/tasks/{task_id}/skills")
    assert resp.status_code == 200
    # spike has no forge skills, but every task gets the agnostic `provision` skill (ADR 0011).
    assert [s["name"] for s in resp.json()] == ["provision"]


def test_briefing_describes_the_current_phase(client: TestClient) -> None:
    task_id = _new_task(client)  # spike, ITERATING
    body = client.get(f"/tasks/{task_id}/briefing").json()
    assert "ITERATING" in body["briefing"]  # the agent's current-phase briefing (the hook emits it)


def test_workflow_overview_maps_the_workflow(client: TestClient) -> None:
    task_id = _new_task(client)  # spike
    body = client.get(f"/tasks/{task_id}/workflow-overview").json()
    assert "spike" in body["overview"] and "ITERATING" in body["overview"]  # the whole-workflow map


def test_legal_transition(client: TestClient) -> None:
    task_id = _new_task(client)
    resp = client.post(
        f"/tasks/{task_id}/transition", json={"to_state": "COMPLETE", "trigger": "finish"}
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "COMPLETE"


def test_illegal_transition_409(client: TestClient) -> None:
    task_id = _new_task(client)
    resp = client.post(f"/tasks/{task_id}/transition", json={"to_state": "WORKING"})
    assert resp.status_code == 409


def test_set_slug(client: TestClient) -> None:
    task_id = _new_task(client)
    resp = client.put(f"/tasks/{task_id}/slug", json={"slug": "fix-widget"})
    assert resp.status_code == 200
    assert resp.json()["slug"] == "fix-widget"


def test_set_turn_and_blocked(client: TestClient) -> None:
    task_id = _new_task(client)  # turn=agent, blocked=false
    turned = client.put(f"/tasks/{task_id}/turn", json={"turn": "user"})
    assert turned.status_code == 200
    assert turned.json()["turn"] == "user"

    blocked = client.put(f"/tasks/{task_id}/blocked", json={"blocked": True})
    assert blocked.json()["blocked"] is True
    assert blocked.json()["turn"] == "user"  # flip-independent: the block left the turn alone


def test_claim_release_over_rest(client: TestClient) -> None:
    task_id = _new_task(client)
    assert client.get(f"/tasks/{task_id}").json()["claimed_by"] is None

    claimed = client.put(f"/tasks/{task_id}/claim", json={"runner_id": "host-1"})
    assert claimed.status_code == 200 and claimed.json()["claimed_by"] == "host-1"

    # a different runner is refused with 409 while it's held
    assert client.put(f"/tasks/{task_id}/claim", json={"runner_id": "host-2"}).status_code == 409

    # release frees it; another runner can then claim
    assert client.delete(f"/tasks/{task_id}/claim").json()["claimed_by"] is None
    assert (
        client.put(f"/tasks/{task_id}/claim", json={"runner_id": "host-2"}).json()["claimed_by"]
        == "host-2"
    )


# -- artifacts ----------------------------------------------------------------------


def test_artifact_put_get_list(client: TestClient) -> None:
    task_id = _new_task(client)
    put = client.put(f"/tasks/{task_id}/artifacts/plan.md", content=b"# Plan")
    assert put.status_code == 204
    assert client.get(f"/tasks/{task_id}/artifacts/plan.md").content == b"# Plan"
    assert client.get(f"/tasks/{task_id}/artifacts").json() == ["plan.md"]


def test_artifact_missing_404(client: TestClient) -> None:
    task_id = _new_task(client)
    assert client.get(f"/tasks/{task_id}/artifacts/plan.md").status_code == 404


# -- liveness -----------------------------------------------------------------------


def test_register_list_deregister(client: TestClient) -> None:
    # The explicit register/deregister routes back the in-process path (the stub runner); a real
    # container instead holds the `/live` connection (see test_liveness_connection.py).
    task_id = _new_task(client)
    reg = client.post(
        f"/tasks/{task_id}/registrations", json={"container_id": "c-abc", "runner_id": "r-1"}
    )
    assert reg.status_code == 201
    reg_id = reg.json()["id"]

    listed = client.get(f"/tasks/{task_id}/registrations").json()
    assert [r["id"] for r in listed] == [reg_id]

    assert client.delete(f"/registrations/{reg_id}").status_code == 204
    assert client.get(f"/tasks/{task_id}/registrations").json() == []


# -- responsibilities over the wire -------------------------------------------------


@pytest.fixture
def gated_client(tmp_path: Path) -> Iterator[TestClient]:
    service = TaskService(
        SqlAlchemyStore(),
        {"gated": _GatedWorkflow()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git")))
    with TestClient(create_app(service)) as c:
        yield c


def test_set_state_bypasses_the_gate(gated_client: TestClient) -> None:
    task_id = gated_client.post("/tasks", json={"repo_id": "r1", "workflow": "gated"}).json()["id"]
    # The declared transition is gated (409); the user's free state-set overrides it.
    assert (
        gated_client.post(f"/tasks/{task_id}/transition", json={"to_state": "COMPLETE"}).status_code
        == 409
    )
    forced = gated_client.put(f"/tasks/{task_id}/state", json={"state": "COMPLETE"})
    assert forced.status_code == 200
    assert forced.json()["state"] == "COMPLETE"


def test_resolve_responsibility_then_transition(gated_client: TestClient) -> None:
    task_id = gated_client.post("/tasks", json={"repo_id": "r1", "workflow": "gated"}).json()["id"]

    # WORKING has an unresolved promise → the transition is gated (409).
    bare = gated_client.post(f"/tasks/{task_id}/transition", json={"to_state": "COMPLETE"})
    assert bare.status_code == 409

    # Report it met → 200, and the WORKING entry records the resolution in place.
    reported = gated_client.post(
        f"/tasks/{task_id}/responsibilities", json={"key": "tests-pass", "status": "met"}
    )
    assert reported.status_code == 200, reported.text
    assert reported.json()["history"][-1]["responsibilities"] == [
        {"key": "tests-pass", "description": "Tests pass", "status": "met", "comment": None}
    ]

    # Now the gate is clear → the transition succeeds.
    ok = gated_client.post(f"/tasks/{task_id}/transition", json={"to_state": "COMPLETE"})
    assert ok.status_code == 200
    assert ok.json()["state"] == "COMPLETE"


def test_failed_responsibility_needs_comment(gated_client: TestClient) -> None:
    task_id = gated_client.post("/tasks", json={"repo_id": "r1", "workflow": "gated"}).json()["id"]
    resp = gated_client.post(
        f"/tasks/{task_id}/responsibilities", json={"key": "tests-pass", "status": "failed"}
    )
    assert resp.status_code == 400  # FAILED without a comment is rejected at report time


def test_report_unknown_responsibility(gated_client: TestClient) -> None:
    task_id = gated_client.post("/tasks", json={"repo_id": "r1", "workflow": "gated"}).json()["id"]
    resp = gated_client.post(
        f"/tasks/{task_id}/responsibilities", json={"key": "ghost", "status": "met"}
    )
    assert resp.status_code == 400

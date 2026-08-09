"""Regression coverage for host ownership gating in issue #249."""

from __future__ import annotations

from panopticon.client import JsonObj
from panopticon.sessionservice.host import HostDaemon


def test_tick_provisions_only_the_claimed_task_among_unclaimed_candidates() -> None:
    provisioned: list[str] = []
    cleaned: list[str] = []

    class _Spawner:
        def mark_healing(self, task: JsonObj) -> None:
            return None

        def spawn_one(self, task: JsonObj) -> None:
            return None

        def reconcile(self, task: JsonObj) -> None:
            return None

        def heal(self, task: JsonObj) -> None:
            return None

        def cleanup(self, task: JsonObj) -> None:
            cleaned.append(task["id"])

    class _Provisioner:
        def provision(self, task: JsonObj, *, runner_id: str) -> None:
            assert runner_id == "host-a"
            provisioned.append(task["id"])

    task: JsonObj = {
        "id": "unclaimed",
        "state": "ITERATING",
        "claimed_by": None,
        "depends_on_task_ids": [],
        "slug": "synthesize-product-audits",
        "branch": "panopticon/synthesize-product-audits",
        "clone": "/tasks/unclaimed",
        "provisioned": False,
        "provisioned_by": None,
        "workspace_verified_by": None,
    }

    # 2119-spec: skip-unclaimed-provisioning
    gated = dict(task, id="gated", depends_on_task_ids=["unfinished-dependency"])
    claimed = dict(task, id="claimed", claimed_by="host-a")
    foreign_claim = dict(task, id="foreign-claim", claimed_by="host-b")

    # 2119: 1.1
    # 2119: 1.2
    HostDaemon(object(), _Spawner(), _Provisioner(), runner_id="host-a").tick(  # type: ignore[arg-type]
        [task, gated, foreign_claim, claimed]
    )

    assert provisioned == ["claimed"]  # the unclaimed task never enters the swallowed-error path
    assert cleaned == ["unclaimed", "gated", "foreign-claim", "claimed"]

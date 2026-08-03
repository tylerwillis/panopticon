"""Temporal dashboard coverage for gated task visibility."""

from test_dashboard import _TASK, _FakeClient, _settle
from textual.widgets import DataTable

from panopticon.terminal.dashboard import Dashboard


# 2119: REQ-026.3.5
async def test_gated_unclaimed_task_remains_visible_after_feed_refresh() -> None:
    gated = {
        **_TASK,
        "id": "gated-dependent",
        "claimed_by": None,
        "container_status": "gated",
        "depends_on_task_ids": ["active-dependency"],
    }
    dependency = {
        **_TASK,
        "id": "active-dependency",
        "state": "ITERATING",
    }
    fake = _FakeClient([gated, dependency])
    app = Dashboard(fake, refresh_interval=0.05)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert "gated-dependent" in {str(key.value) for key in table.rows}

        builds = fake.list_tasks_calls
        fake.signal_change()
        await _settle(pilot, lambda: fake.list_tasks_calls > builds)

        assert "gated-dependent" in {str(key.value) for key in table.rows}
        assert gated["claimed_by"] is None

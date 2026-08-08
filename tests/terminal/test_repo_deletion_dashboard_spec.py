"""RFC 2119 coverage for repository deletion on the dashboard edit page."""

from __future__ import annotations

from typing import Any

import pytest
from test_dashboard import _FakeClient, _http_400
from textual.widgets import DataTable, Input, Label, Static

from panopticon.terminal import dashboard
from panopticon.terminal.dashboard import Dashboard

_REPO = {
    "id": "r1",
    "name": "acme/widgets",
    "git_url": "https://x/r1.git",
    "default_base": "main",
}


class _RepoDeleteClient(_FakeClient):
    def __init__(self, repos: list[dict[str, Any]]) -> None:
        super().__init__([], repos=repos)
        self.deleted_repos: list[str] = []

    def delete_repo(self, repo_id: str) -> None:
        if self.repo_error is not None:
            raise _http_400(self.repo_error)
        self.deleted_repos.append(repo_id)
        self._repos = [repo for repo in self._repos if repo["id"] != repo_id]


# 2119: repo-deletion.2.1
# 2119: repo-deletion.2.2
@pytest.mark.parametrize("repo_name", ["acme/widgets", "other/project"])
async def test_repo_delete_action_is_edit_only_and_opens_typed_name_confirmation(
    repo_name: str,
) -> None:
    fake = _RepoDeleteClient([{**_REPO, "name": repo_name}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g", "n")
        await pilot.pause()
        assert len(app.screen.query("#delete-repo")) == 0
        await pilot.press("escape", "e")
        await pilot.pause()
        assert len(app.screen.query("#delete-repo")) == 1

        await pilot.click("#delete-repo")
        await pilot.pause()

        assert isinstance(app.screen, dashboard.DeleteRepoScreen)
        prompt_lines = [str(label.render()) for label in app.screen.query(Label)]
        assert prompt_lines == [
            "Repository deletion is irreversible.",
            f"type {repo_name!r} exactly and press Enter to delete",
        ]


# 2119: repo-deletion.2.3
# 2119: repo-deletion.2.4
# 2119: repo-deletion.2.6
async def test_repo_delete_requires_exact_name_and_enter_then_refreshes_list() -> None:
    survivor = {**_REPO, "id": "r2", "name": "other/project"}
    fake = _RepoDeleteClient([dict(_REPO), survivor])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g", "e")
        await pilot.pause()
        await pilot.click("#delete-repo")
        await pilot.pause()

        confirmation = app.screen.query_one("#delete-repo-name", Input)
        fake._repos[0]["name"] = "renamed/after-open"
        confirmation.value = "renamed/after-open"
        await pilot.press("enter")
        await pilot.pause()
        assert fake.deleted_repos == []  # confirmation remains bound to its displayed name

        confirmation.value = "acme/widgets "
        await pilot.press("enter")
        await pilot.pause()
        assert fake.deleted_repos == []
        assert isinstance(app.screen, dashboard.DeleteRepoScreen)
        assert "match" in str(app.screen.query_one("#delete-repo-error", Static).render()).lower()

        confirmation.value = "ACME/widgets"
        await pilot.press("enter")
        await pilot.pause()
        assert fake.deleted_repos == []  # equality is case-sensitive
        assert isinstance(app.screen, dashboard.DeleteRepoScreen)
        assert "match" in str(app.screen.query_one("#delete-repo-error", Static).render()).lower()

        confirmation.value = "acme/widgets"
        await pilot.press("tab")
        await pilot.pause()
        assert fake.deleted_repos == []  # exact text plus a non-Enter key is insufficient
        confirmation.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert fake.deleted_repos == ["r1"]
        assert isinstance(app.screen, dashboard.ReposScreen)
        table = app.screen.query_one("#repos", DataTable)
        assert table.row_count == 1
        assert str(table.ordered_rows[0].key.value) == "r2"


# 2119: repo-deletion.2.5
@pytest.mark.parametrize("reference_count", [1, 3])
async def test_repo_delete_service_refusal_keeps_confirmation_open_with_task_count(
    reference_count: int,
) -> None:
    fake = _RepoDeleteClient([dict(_REPO)])
    noun = "task" if reference_count == 1 else "tasks"
    fake.repo_error = f"repo 'r1' is referenced by {reference_count} {noun}"
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g", "e")
        await pilot.pause()
        await pilot.click("#delete-repo")
        await pilot.pause()
        app.screen.query_one("#delete-repo-name", Input).value = "acme/widgets"
        await pilot.press("enter")
        await pilot.pause()

        assert fake.deleted_repos == []
        assert isinstance(app.screen, dashboard.DeleteRepoScreen)
        assert f"referenced by {reference_count} {noun}" in str(
            app.screen.query_one("#delete-repo-error", Static).render()
        )

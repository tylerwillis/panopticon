"""Regression coverage for task-specific overflow hints staying off shared tables."""

from textual.app import App, ComposeResult
from textual.widgets import DataTable

from panopticon.terminal.dashboard import _VimDataTable


class _GenericTableApp(App[None]):
    def compose(self) -> ComposeResult:
        yield _VimDataTable(id="generic")

    def on_mount(self) -> None:
        table = self.query_one("#generic", DataTable)
        table.add_column("workflow")
        for index in range(30):
            table.add_row(f"workflow-{index:02}")


async def test_generic_vim_table_overflow_does_not_render_task_indicators() -> None:
    app = _GenericTableApp()
    async with app.run_test(size=(40, 8)) as pilot:
        await pilot.pause()
        table = app.query_one("#generic", DataTable)
        assert table.max_scroll_y > 0
        rendered = "\n".join(
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()[
                table.region.y : table.region.bottom
            ]
        )

        assert "↑ more" not in rendered
        assert "↓ more" not in rendered

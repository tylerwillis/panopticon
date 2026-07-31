"""Registry-driven checks for collisions in Textual's active binding contexts."""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections import defaultdict
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.binding import BindingsMap
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import Input

from panopticon.terminal import dashboard


@dataclass(frozen=True)
class _BindingContext:
    name: str
    screen: type[Any]
    nodes: tuple[type[Any], ...]


def _project_binding_keys(node: type[Any]) -> set[str]:
    """Normalized keys declared outside Textual in ``node``'s effective binding ancestry."""
    keys: set[str] = set()
    inherit = bool(getattr(node, "_inherit_bindings", True))
    for owner in node.__mro__:
        if owner is not node and not inherit:
            break
        bindings = owner.__dict__.get("BINDINGS")
        if bindings and not owner.__module__.startswith("textual."):
            keys.update(BindingsMap(bindings).key_to_bindings)
    return keys


def _effective_targets(node: type[Any]) -> dict[str, set[str]]:
    """Textual-normalized effective targets, qualified by the active binding owner."""
    return {
        key: {f"{node.__name__}.{binding.action}" for binding in bindings}
        for key, bindings in node._merged_bindings.key_to_bindings.items()
    }


def _resolve_compose_reference(expression: ast.expr, namespace: dict[str, Any]) -> Any:
    if isinstance(expression, ast.Name):
        return namespace.get(expression.id)
    if isinstance(expression, ast.Attribute):
        owner = _resolve_compose_reference(expression.value, namespace)
        return getattr(owner, expression.attr, None)
    return None


def _widget_calls(owner: type[Any]) -> set[type[Widget]]:
    """Find real widget classes constructed by generator methods in Panopticon's compose tree."""
    widgets: set[type[Widget]] = set()
    for provider in owner.__mro__:
        if provider.__module__.startswith("textual."):
            continue
        for member in provider.__dict__.values():
            if not inspect.isfunction(member):
                continue
            try:
                tree = ast.parse(textwrap.dedent(inspect.getsource(member)))
            except (OSError, TypeError):
                continue
            if not any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(tree)):
                continue
            for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
                candidate = _resolve_compose_reference(call.func, member.__globals__)
                if (
                    isinstance(candidate, type)
                    and issubclass(candidate, Widget)
                    and not issubclass(candidate, Screen)
                ):
                    widgets.add(candidate)
    return widgets


def _composed_widget_types(screen: type[Any]) -> set[type[Widget]]:
    """Recursively discover widget types from the screen's real compose definitions."""
    found: set[type[Widget]] = set()
    pending = list(_widget_calls(screen))
    while pending:
        widget = pending.pop()
        if widget in found:
            continue
        found.add(widget)
        if not widget.__module__.startswith("textual."):
            pending.extend(_widget_calls(widget) - found)
    return found


def _contexts_for_screen(screen: type[Any]) -> list[_BindingContext]:
    contexts = [_BindingContext(screen.__name__, screen, (screen,))]
    contexts.extend(
        _BindingContext(
            f"{screen.__name__} > {widget.__name__}",
            screen,
            (screen, widget),
        )
        for widget in sorted(_composed_widget_types(screen), key=lambda item: item.__name__)
        if bool(getattr(widget, "can_focus", False))
    )
    return contexts


def _discover_contexts(module: ModuleType) -> list[_BindingContext]:
    """Discover Dashboard/Screen classes from the module itself, never from an inventory."""
    screens = {
        candidate
        for candidate in vars(module).values()
        if isinstance(candidate, type)
        and candidate.__module__ == module.__name__
        and issubclass(candidate, (App, Screen))
    }
    return [
        context
        for screen in sorted(screens, key=lambda item: item.__name__)
        for context in _contexts_for_screen(screen)
    ]


def _assert_unique_bindings(contexts: list[_BindingContext]) -> None:
    collisions: list[str] = []
    for context in contexts:
        project_keys = set().union(*(_project_binding_keys(node) for node in context.nodes))
        targets: dict[str, set[str]] = defaultdict(set)
        for node in context.nodes:
            for chord, actions in _effective_targets(node).items():
                targets[chord].update(actions)
        for chord in sorted(project_keys):
            actions = sorted(targets[chord])
            if len(actions) > 1:
                collisions.append(
                    f"context={context.name}; chord={chord}; actions={', '.join(actions)}"
                )
    if collisions:
        raise AssertionError("keybinding collisions:\n" + "\n".join(collisions))


# 2119: REQ-023.1.1
# 2119: REQ-023.2.1
def test_every_dashboard_binding_context_has_unique_action_targets() -> None:
    _assert_unique_bindings(_discover_contexts(dashboard))


# 2119: REQ-023.4.1
def test_context_registry_discovers_every_dashboard_screen_without_an_inventory() -> None:
    expected = {
        candidate
        for candidate in vars(dashboard).values()
        if isinstance(candidate, type)
        and candidate.__module__ == dashboard.__name__
        and issubclass(candidate, (App, Screen))
    }
    discovered = {context.screen for context in _discover_contexts(dashboard)}
    assert discovered == expected


class _CaseSensitiveScreen(ModalScreen[None]):
    BINDINGS = [("y", "copy_slug", ""), ("Y", "copy_id", "")]


# 2119: REQ-023.3.1
def test_lower_and_uppercase_letters_are_distinct_chords() -> None:
    _assert_unique_bindings(_contexts_for_screen(_CaseSensitiveScreen))


class _InheritedConflictInput(Input):
    pass


class _NestedConflictContainer(Widget):
    def compose(self) -> ComposeResult:
        yield _InheritedConflictInput()


class _InheritedConflictScreen(ModalScreen[None]):
    BINDINGS = [("enter", "save_record", "")]

    def compose(self) -> ComposeResult:
        yield _NestedConflictContainer()


# 2119: REQ-023.2.1
# 2119: REQ-023.4.1
# 2119: REQ-023.5.1
# 2119: REQ-023.6.1
def test_deliberate_inherited_widget_conflict_fails_with_actionable_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_module = ModuleType("deliberate_binding_conflict")
    monkeypatch.setattr(_InheritedConflictScreen, "__module__", fixture_module.__name__)
    fixture_module.ConflictScreen = _InheritedConflictScreen

    with pytest.raises(AssertionError) as raised:
        _assert_unique_bindings(_discover_contexts(fixture_module))

    message = str(raised.value)
    assert "_InheritedConflictScreen > _InheritedConflictInput" in message
    assert "chord=enter" in message
    assert "_InheritedConflictScreen.save_record" in message
    assert "_InheritedConflictInput.submit" in message

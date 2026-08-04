from pathlib import Path


# 2119: layered-settings-hints.7.1
def test_layered_settings_convention_is_documented_with_scope_and_current_relationships() -> None:
    convention = Path("docs/layered-settings.md").read_text()

    assert (
        convention
        == """# Layered settings

Any layered setting names its neighbouring layer in both directions: the surface showing a
default points to the override or filter surface, and that surface identifies where its default
comes from. Declare every dashboard default/override or default/filter relationship in the shared
relationship registry; tests can enforce both directions for declared relationships but cannot
infer layered semantics that were never declared. A declaration makes its copy enumerable; a new
dashboard surface still needs explicit widget wiring and an integration test.

Keep the combined signposts on each dashboard surface to one muted hint widget with no
explicit newline; narrow terminals may wrap that logical line, but must not clip it. Use
direction-specific copy from the shared hint renderer, because inheritance, override, and filter
semantics are not interchangeable. Settings that are not layered do not receive these hints.

The current relationships are:

- workflow reviewer models default in workflow config and can be overridden in repo config;
- workflow `default_harness` / `default_model` values can be overridden at per-task creation;
- repo `default_harness` / `default_model` values apply when the workflow has no pair and can be
  overridden at per-task creation, with the app default as the final fallback; and
- workflow availability is filtered by repo `enabled_workflows` / `disabled_workflows`: the repo
  filter starts from and filters the workflow's `opt_in` default rather than overriding a value.

The registry plus shared hint renderer is deliberate: adding a declared relationship makes both
directions enumerable and testable, while each declaration retains honest screen-specific copy.
"""
    )

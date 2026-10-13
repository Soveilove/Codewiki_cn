# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Unit tests for module-name collision handling (issue #76)."""

from codewiki.src.be.module_naming import (
    collect_module_tree_names,
    dedupe_module_tree_names,
    normalize_sub_module_specs,
    plan_sub_module_specs,
    resolve_unique_name,
    sanitize_module_name,
)


class TestSanitizeModuleName:
    def test_spaces_become_underscores(self):
        assert sanitize_module_name("text ui") == "text_ui"

    def test_path_separators_removed(self):
        assert "/" not in sanitize_module_name("engine/search")
        assert "\\" not in sanitize_module_name("engine\\search")

    def test_case_preserved(self):
        assert sanitize_module_name("TextUI") == "TextUI"

    def test_empty_falls_back(self):
        assert sanitize_module_name("   ") == "module"


class TestResolveUniqueName:
    def test_unique_name_unchanged(self):
        assert resolve_unique_name("search", "engine", {"evaluation"}) == "search"

    def test_conflict_gets_parent_prefix(self):
        assert resolve_unique_name("search", "engine", {"search"}) == "engine_search"

    def test_prefixed_name_also_taken_gets_numeric_suffix(self):
        taken = {"search", "engine_search"}
        assert resolve_unique_name("search", "engine", taken) == "engine_search_2"

    def test_conflict_without_parent_gets_numeric_suffix(self):
        assert resolve_unique_name("search", None, {"search"}) == "search_2"


class TestNormalizeSubModuleSpecs:
    def test_no_conflicts_names_unchanged(self, tmp_path):
        module_tree = {"engine": {"components": [], "children": {}}}
        name_map = normalize_sub_module_specs(
            {"search": [], "evaluation": []}, "engine", module_tree, str(tmp_path)
        )
        assert name_map == {"search": "search", "evaluation": "evaluation"}

    def test_conflict_with_tree_name_gets_parent_prefix(self, tmp_path):
        # Issue #76 variant 1: engine's sub-modules named like top-level modules
        module_tree = {
            "engine": {"components": [], "children": {}},
            "search": {"components": [], "children": {}},
            "evaluation": {"components": [], "children": {}},
        }
        name_map = normalize_sub_module_specs(
            {"search": [], "evaluation": []}, "engine", module_tree, str(tmp_path)
        )
        assert name_map == {
            "search": "engine_search",
            "evaluation": "engine_evaluation",
        }

    def test_conflict_with_existing_md_file_gets_parent_prefix(self, tmp_path):
        (tmp_path / "search.md").write_text("existing docs")
        name_map = normalize_sub_module_specs({"search": []}, "engine", {}, str(tmp_path))
        assert name_map == {"search": "engine_search"}

    def test_reserved_stems_are_taken(self, tmp_path):
        name_map = normalize_sub_module_specs({"overview": []}, "engine", {}, str(tmp_path))
        assert name_map == {"overview": "engine_overview"}

    def test_batch_internal_collision_after_sanitization(self, tmp_path):
        # Two requested names that sanitize to the same stem
        name_map = normalize_sub_module_specs(
            {"text ui": [], "text_ui": []}, "textui", {}, str(tmp_path)
        )
        assert name_map["text ui"] == "text_ui"
        assert name_map["text_ui"] == "textui_text_ui"
        assert len(set(name_map.values())) == 2


class TestDedupeModuleTreeNames:
    def test_nested_vs_toplevel_collision(self):
        tree = {
            "engine": {
                "components": [],
                "children": {"search": {"components": [], "children": {}}},
            },
            "search": {"components": [], "children": {}},
        }
        deduped = dedupe_module_tree_names(tree)
        names = []
        stack = [deduped]
        while stack:
            level = stack.pop()
            for name, info in level.items():
                names.append(name)
                if isinstance(info.get("children"), dict):
                    stack.append(info["children"])
        assert len(names) == len(set(names))
        assert "engine" in names

    def test_no_collision_tree_unchanged(self):
        tree = {
            "engine": {
                "components": ["a"],
                "children": {"engine_search": {"components": ["b"], "children": {}}},
            },
        }
        assert dedupe_module_tree_names(tree) == tree


class TestCollectModuleTreeNames:
    def test_collects_all_depths(self):
        tree = {
            "a": {"children": {"b": {"children": {"c": {"children": {}}}}}},
        }
        assert collect_module_tree_names(tree) == {"a", "b", "c"}


class TestPlanSubModuleSpecs:
    """Issue #113: agent-inserted sub-modules are never renamed x_2, x_3, ...;
    a request that is already documented is skipped instead."""

    def test_no_conflicts_matches_normalize(self, tmp_path):
        module_tree = {"engine": {"components": [], "children": {}}}
        specs = {"search": [], "evaluation": []}
        plan = plan_sub_module_specs(specs, "engine", module_tree, str(tmp_path))
        assert plan.name_map == normalize_sub_module_specs(
            specs, "engine", module_tree, str(tmp_path)
        )
        assert plan.skipped == {}

    def test_conflict_gets_parent_prefix(self, tmp_path):
        (tmp_path / "search.md").write_text("existing docs")
        plan = plan_sub_module_specs({"search": []}, "engine", {}, str(tmp_path))
        assert plan.name_map == {"search": "engine_search"}
        assert plan.skipped == {}

    def test_plain_and_prefixed_taken_is_skipped_not_suffixed(self, tmp_path):
        (tmp_path / "approval.md").write_text("round 1")
        (tmp_path / "overview_approval.md").write_text("round 2")
        plan = plan_sub_module_specs({"approval": []}, "overview", {}, str(tmp_path))
        assert plan.name_map == {}
        assert "already documented as approval.md" in plan.skipped["approval"]

    def test_tree_names_count_as_taken(self, tmp_path):
        module_tree = {
            "search": {"components": [], "children": {}},
            "engine_search": {"components": [], "children": {}},
        }
        plan = plan_sub_module_specs(
            {"search": [], "ranking": []}, "engine", module_tree, str(tmp_path)
        )
        assert plan.name_map == {"ranking": "ranking"}
        assert set(plan.skipped) == {"search"}

    def test_batch_internal_duplicate_is_prefixed_then_skipped(self, tmp_path):
        # Same stem requested three times in one batch: plain, prefixed, then skipped.
        plan = plan_sub_module_specs(
            {"text ui": [], "text_ui": [], " text ui": []}, "textui", {}, str(tmp_path)
        )
        assert plan.name_map == {"text ui": "text_ui", "text_ui": "textui_text_ui"}
        assert " text ui" in plan.skipped
        assert not any(v.endswith("_2") for v in plan.name_map.values())

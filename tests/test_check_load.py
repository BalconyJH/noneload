from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "src" / "check_load.py"
SPEC = importlib.util.spec_from_file_location("check_load", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
check_load = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_load)


class ProjectDependencyGroupsTests(unittest.TestCase):
    def test_reads_base_and_optional_dependencies_from_pyproject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pyproject = Path(directory) / "pyproject.toml"
            pyproject.write_text(
                """
[project]
name = "nonebot-plugin-example"
dependencies = ["nonebot2>=2.0"]

[project.optional-dependencies]
http = ["httpx>=0.28"]
plugins = ["nonebot-plugin-demo>=1"]
""".lstrip(),
                encoding="utf-8",
            )

            self.assertEqual(
                check_load.project_dependency_groups(Path(directory)),
                (
                    ["nonebot2>=2.0"],
                    ["httpx>=0.28", "nonebot-plugin-demo>=1"],
                    ["http", "plugins"],
                ),
            )

    def test_merges_all_optional_extras_into_local_package_spec(self) -> None:
        self.assertEqual(
            check_load.with_optional_extras(".[all]", ["http", "plugins"]),
            ".[all,http,plugins]",
        )

    def test_rejects_non_local_package_specs_for_optional_dependency_checks(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "local package spec"):
            check_load.with_optional_extras("nonebot-plugin-example", ["http"])


class DependencyModuleTests(unittest.TestCase):
    def test_only_returns_installed_registry_plugins(self) -> None:
        original_fetch = check_load.fetch_registry_plugins
        check_load.fetch_registry_plugins = lambda _: {
            "nonebot-plugin-base": "nonebot_plugin_base",
            "nonebot-plugin-optional": "nonebot_plugin_optional",
            "nonebot-plugin-missing": "nonebot_plugin_missing",
        }
        try:
            dependencies = check_load.build_dependency_modules(
                requirements=[
                    "nonebot-plugin-base>=1",
                    "nonebot-plugin-optional>=1",
                    "nonebot-plugin-missing>=1",
                ],
                installed_packages={"nonebot-plugin-base", "nonebot-plugin-optional"},
                project_link="nonebot-plugin-example",
                registry_url="https://example.invalid/plugins.json",
            )
        finally:
            check_load.fetch_registry_plugins = original_fetch

        self.assertEqual(dependencies, ["nonebot_plugin_base", "nonebot_plugin_optional"])

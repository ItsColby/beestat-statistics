"""Static checks for Home Assistant integration-quality conventions."""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

from scripts.run_dependency_light_tests import discover_home_assistant_test_files

ROOT = Path(__file__).resolve().parents[1]


class HomeAssistantQualityStaticTest(unittest.TestCase):
    """Validate HA quality rules that can be checked without HA test deps."""

    def test_logs_do_not_expose_raw_beestat_identifiers(self) -> None:
        integration_root = ROOT / "custom_components/beestat_statistics"
        for path in integration_root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("thermostat_id=%s", text)
            self.assertNotIn("sensor_id=%s", text)
            tree = ast.parse(text)
            for call in ast.walk(tree):
                if not _is_logger_call(call):
                    continue
                rendered = ast.unparse(call)
                self.assertNotIn("_LOGGER.exception", rendered)
                self.assertNotIn("exc_info=True", rendered)
                self.assertNotIn("type(err).__name__", rendered)
                for private_expression in (
                    "entity_entry.entity_id",
                    "existing_entity_id",
                    "new_unique_id",
                    "device_entry.name",
                ):
                    self.assertNotIn(private_expression, rendered)
                for argument in call.args[1:]:
                    self.assertFalse(
                        isinstance(argument, ast.Name) and argument.id == "err",
                        f"Raw exception logged by {path.name}: {rendered}",
                    )

    def test_config_flow_fields_have_descriptions(self) -> None:
        strings = _json_file(
            "custom_components/beestat_statistics/translations/en.json"
        )
        config_steps = strings["config"]["step"]

        for step_id in ("user", "reconfigure", "reauth_confirm"):
            self.assertEqual(
                set(config_steps[step_id]["data"]),
                set(config_steps[step_id]["data_description"]),
                f"config step {step_id} must describe every field",
            )

        self.assertEqual(
            set(strings["options"]["step"]["init"]["menu_options"]),
            {
                "timing",
                "source_scope",
                "confirm_automatic_mappings",
                "thermostat_mapping",
                "sensor_mapping",
            },
        )

        for step_id, options_step in strings["options"]["step"].items():
            self.assertIn(
                "title",
                options_step,
                f"options step {step_id} needs a title",
            )
            self.assertIn(
                "description",
                options_step,
                f"options step {step_id} needs a useful description",
            )

        for step_id, options_step in strings["options"]["step"].items():
            if "data" not in options_step:
                continue
            self.assertEqual(
                set(options_step["data"]),
                set(options_step["data_description"]),
                f"options step {step_id} must describe every field",
            )

        self.assertIn(
            "api_key_required",
            strings["config"]["error"],
            "reauth blank-key validation needs a translated field error",
        )
        self.assertIn(
            "unknown",
            strings["config"]["error"],
            "unexpected config-flow validation errors need a translated base error",
        )
        self.assertIn("account_change_confirm", config_steps)

    def test_package_is_marked_typed(self) -> None:
        self.assertTrue(
            (ROOT / "custom_components/beestat_statistics/py.typed").is_file()
        )

    def test_manifest_and_hacs_metadata_are_publishable(self) -> None:
        manifest = _json_file("custom_components/beestat_statistics/manifest.json")
        hacs = _json_file("hacs.json")
        integrations = [
            path.name
            for path in (ROOT / "custom_components").iterdir()
            if path.is_dir()
        ]

        for key in (
            "codeowners",
            "config_flow",
            "documentation",
            "domain",
            "integration_type",
            "iot_class",
            "issue_tracker",
            "name",
            "requirements",
            "version",
        ):
            self.assertIn(key, manifest)

        self.assertEqual(manifest["domain"], "beestat_statistics")
        self.assertEqual(manifest["integration_type"], "hub")
        self.assertEqual(manifest["iot_class"], "cloud_polling")
        self.assertTrue(manifest["config_flow"])
        # Home Assistant applies manifest-level single_config_entry before async_step_import,
        # which would prevent YAML imports from merging into the existing entry.
        self.assertNotIn("single_config_entry", manifest)
        self.assertEqual(manifest["requirements"], [])
        self.assertEqual(integrations, ["beestat_statistics"])
        self.assertEqual(hacs["name"], "Beestat Statistics")
        self.assertIn("homeassistant", hacs)
        self.assertTrue(
            (ROOT / "custom_components/beestat_statistics/brand/icon.png").is_file()
        )
        release_notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
        versions = re.findall(
            r"^## Beestat Statistics v([^\n]+)$", release_notes, re.MULTILINE
        )
        self.assertTrue(versions, "Release notes must identify released versions")
        self.assertEqual(versions[0], manifest["version"])

    def test_ci_python_matches_advertised_home_assistant_target(self) -> None:
        workflow = (ROOT / ".github/workflows/validate.yaml").read_text(
            encoding="utf-8"
        )
        runner = (ROOT / "scripts/verify-release-local.sh").read_text(encoding="utf-8")
        minimum = (
            (ROOT / "requirements-ha-test.txt").read_text(encoding="utf-8").strip()
        )
        current = (
            (ROOT / "requirements-ha-current.txt").read_text(encoding="utf-8").strip()
        )
        self.assertEqual(
            minimum, f"homeassistant=={_json_file('hacs.json')['homeassistant']}"
        )
        for requirement in (minimum, current):
            self.assertRegex(requirement, r"^homeassistant==[0-9]+[.][0-9]+[.][0-9]+$")
            self.assertIn(f"Core {requirement.split('==')[1]}", workflow)
        self.assertNotEqual(
            minimum, current, "Equal support lanes should be consolidated"
        )
        self.assertIn('python-version: "3.14"', workflow)
        self.assertIn(
            "asyncio_mode = auto", (ROOT / "pytest.ini").read_text(encoding="utf-8")
        )
        for command in (
            "python -m mypy --strict custom_components/beestat_statistics",
            "python -m ruff format --check custom_components tests scripts",
            "python -m ruff check custom_components tests scripts",
            "shellcheck scripts/verify-release-local.sh",
            "zizmor --strict-collection --persona auditor .",
            "python scripts/check_public_safety.py",
        ):
            self.assertIn(command, runner)
        self.assertNotIn("GH_TOKEN", runner)
        self.assertEqual(1, workflow.count("permissions:"))
        self.assertEqual(
            "  contents: read",
            workflow.split("\npermissions:\n", 1)[1].split("\n\n", 1)[0],
        )
        dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        self.assertEqual(1, dependabot.count("package-ecosystem: github-actions"))
        self.assertNotIn("package-ecosystem: pip", dependabot)
        self.assertEqual(1, dependabot.count("interval: weekly"))

    def test_development_guide_matches_validation_owners(self) -> None:
        development = (ROOT / "docs/development.md").read_text(encoding="utf-8")
        for name in ("requirements-ha-test.txt", "requirements-ha-current.txt"):
            version = (ROOT / name).read_text(encoding="utf-8").strip().split("==")[1]
            self.assertIn(name, development)
            self.assertIn(f"`{version}`", development)

    def test_discovered_ha_modules_fail_closed_without_harness(self) -> None:
        test_files = tuple(sorted((ROOT / "tests").rglob("test_*.py")))
        discovered_ha_filenames = {
            path.name for path in discover_home_assistant_test_files(test_files)
        }
        ha_modules = tuple(
            f"tests/{filename}" for filename in sorted(discovered_ha_filenames)
        )

        for relative_path in ha_modules:
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotIn("unittest.SkipTest", text)
                self.assertNotIn("except ModuleNotFoundError", text)

    def test_platforms_declare_parallel_updates(self) -> None:
        expected = {
            "binary_sensor.py": "PARALLEL_UPDATES = 0",
            "button.py": "PARALLEL_UPDATES = 1",
            "date.py": "PARALLEL_UPDATES = 0",
            "sensor.py": "PARALLEL_UPDATES = 0",
        }
        for filename, declaration in expected.items():
            text = (
                ROOT / f"custom_components/beestat_statistics/{filename}"
            ).read_text(encoding="utf-8")
            self.assertIn(declaration, text)

    def test_diagnostic_attributes_are_excluded_from_recorder_history(self) -> None:
        for filename, class_name, expected in (
            ("sensor.py", "BeestatSensor", {"last_error", "profiles", "active_alerts"}),
            ("binary_sensor.py", "BeestatSensorInUseBinarySensor", {"beestat_name"}),
            (
                "binary_sensor.py",
                "BeestatThermostatAlertProblemBinarySensor",
                {"active_alerts"},
            ),
            (
                "date.py",
                "BeestatFilterChangedDate",
                {"change_day_runtime_baseline_seconds", "legacy_helper_entity_id"},
            ),
        ):
            with self.subTest(filename=filename, class_name=class_name):
                tree = ast.parse(
                    (
                        ROOT / "custom_components/beestat_statistics" / filename
                    ).read_text(encoding="utf-8")
                )
                classes = {
                    node.name: node
                    for node in tree.body
                    if isinstance(node, ast.ClassDef)
                }
                declarations = [
                    node.value
                    for node in classes[class_name].body
                    if isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name)
                        and target.id == "_unrecorded_attributes"
                        for target in node.targets
                    )
                ]
                self.assertEqual(len(declarations), 1)
                declaration = declarations[0]
                self.assertIsInstance(declaration, ast.Call)
                self.assertEqual(ast.unparse(declaration.func), "frozenset")
                self.assertEqual(len(declaration.args), 1)
                self.assertIsInstance(declaration.args[0], ast.Set)
                attributes = {
                    node.value
                    for node in declaration.args[0].elts
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                }
                self.assertLessEqual(expected, attributes)

    def test_room_sensor_state_attributes_do_not_expose_mapping_internals(self) -> None:
        binary_text = (
            ROOT / "custom_components/beestat_statistics/binary_sensor.py"
        ).read_text(encoding="utf-8")
        method_text = _class_method_source(
            binary_text,
            "BeestatSensorInUseBinarySensor",
            "extra_state_attributes",
        )

        self.assertIn('"beestat_name"', method_text)
        self.assertIn('"sensor_type"', method_text)
        for snippet in (
            '"identifier"',
            '"sensor_id"',
            '"thermostat_id"',
            '"temperature_entity_id"',
            '"occupancy_entity_id"',
            '"motion_entity_id"',
        ):
            self.assertNotIn(snippet, method_text)

    def test_user_visible_exceptions_and_repairs_are_translated(self) -> None:
        strings = _json_file(
            "custom_components/beestat_statistics/translations/en.json"
        )
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")
        button_text = (
            ROOT / "custom_components/beestat_statistics/button.py"
        ).read_text(encoding="utf-8")
        coordinator_text = (
            ROOT / "custom_components/beestat_statistics/coordinator.py"
        ).read_text(encoding="utf-8")

        translated_exception_names = {
            "ConfigEntryAuthFailed",
            "ConfigEntryError",
            "HomeAssistantError",
            "ServiceValidationError",
            "UpdateFailed",
        }
        trees = tuple(
            ast.parse(text) for text in (init_text, button_text, coordinator_text)
        )
        exception_keys = {
            keyword.value.value
            for tree in trees
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in translated_exception_names
            for keyword in call.keywords
            if keyword.arg == "translation_key"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        }
        self.assertTrue(exception_keys)
        self.assertTrue(exception_keys <= set(strings["exceptions"]))

        for tree in trees:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Raise) or node.exc is None:
                    continue
                if not (
                    isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Name)
                    and node.exc.func.id in translated_exception_names
                ):
                    continue
                self.assertTrue(
                    node.cause is None
                    or (
                        isinstance(node.cause, ast.Constant)
                        and node.cause.value is None
                    ),
                    "Translated Home Assistant exceptions must not retain a raw "
                    "exception cause",
                )

        self.assertTrue(
            {
                "missing_override_entities",
                "invalid_override_entity_domains",
                "mapping_device_conflicts",
                "yaml_connection_change_requires_reconfigure",
            }
            <= set(strings["issues"])
        )

    def test_skipped_window_logs_do_not_format_api_exceptions(self) -> None:
        """CodeQL-sensitive logs must not read possibly credential-bearing text."""

        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("self._client.redact_error(err)", init_text)

    def test_import_lifecycle_uses_entity_state_not_custom_bus_events(self) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("bus.async_fire", init_text)

    def test_stale_runtime_blueprint_is_documented_and_native(self) -> None:
        blueprint_path = (
            ROOT
            / "blueprints/automation/beestat_statistics/stale_runtime_notification.yaml"
        )
        blueprint = blueprint_path.read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        usage = (ROOT / "docs/usage.md").read_text(encoding="utf-8")

        self.assertIn("domain: automation", blueprint)
        self.assertIn("min_version: 2026.8.0", blueprint)
        self.assertIn("trigger: numeric_state", blueprint)
        self.assertIn("selector:\n        action: {}", blueprint)
        self.assertNotIn("trigger: template", blueprint)
        self.assertIn(str(blueprint_path.relative_to(ROOT)).replace("\\", "/"), usage)
        self.assertIn("raw.githubusercontent.com", usage)
        self.assertIn("my.home-assistant.io/redirect/hacs_repository", readme)

    def test_documentation_navigation_and_action_references(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in ("docs/usage.md", "docs/architecture.md", "docs/development.md"):
            self.assertIn(name, readme)
        self.assertIn(_json_file("hacs.json")["homeassistant"], readme)
        usage = (ROOT / "docs/usage.md").read_text(encoding="utf-8")
        services = _json_file(
            "custom_components/beestat_statistics/translations/en.json"
        )
        for action in services["services"]:
            self.assertIn(action, usage, f"Undocumented action: {action}")
        documents = [ROOT / "README.md", ROOT / "RELEASE_NOTES.md"]
        documents.extend((ROOT / "docs").glob("*.md"))
        for document in documents:
            text = document.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^\s)]+)\)", text):
                if "://" in target:
                    continue
                path, _, anchor = target.partition("#")
                destination = document.parent / path if path else document
                with self.subTest(document=document.name, target=target):
                    self.assertTrue(destination.is_file())
                    if anchor:
                        headings = re.findall(
                            r"^#{1,6} (.+)$",
                            destination.read_text(encoding="utf-8"),
                            re.MULTILINE,
                        )
                        anchors = {
                            re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
                            for heading in headings
                        }
                        self.assertIn(anchor, anchors)

    def test_documentation_json_examples_parse(self) -> None:
        for path in (ROOT / "docs/examples").glob("*.json"):
            with self.subTest(example=path.name):
                json.loads(path.read_text(encoding="utf-8"))

    def test_repository_support_templates_reduce_secret_leak_risk(self) -> None:
        bug_template = (ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(
            encoding="utf-8"
        )

        self.assertRegex(bug_template, r"(?i)redact")
        self.assertRegex(bug_template, r"(?i)API keys")
        self.assertTrue((ROOT / ".github/ISSUE_TEMPLATE/config.yml").is_file())

    def test_entity_translation_keys_have_names_and_icons(self) -> None:
        strings = _json_file(
            "custom_components/beestat_statistics/translations/en.json"
        )
        icons = _json_file("custom_components/beestat_statistics/icons.json")

        for platform, relative_path in {
            "binary_sensor": "custom_components/beestat_statistics/binary_sensor.py",
            "button": "custom_components/beestat_statistics/button.py",
            "date": "custom_components/beestat_statistics/date.py",
            "sensor": "custom_components/beestat_statistics/sensor.py",
        }.items():
            translation_keys = _literal_translation_keys(ROOT / relative_path)
            self.assertGreater(
                len(translation_keys),
                0,
                f"No literal translation keys found in {relative_path}",
            )
            for key in translation_keys:
                self.assertIn(
                    key,
                    strings["entity"][platform],
                    f"{platform}.{key} is missing from translations/en.json",
                )
                self.assertIn(
                    key,
                    icons["entity"][platform],
                    f"{platform}.{key} is missing from icons.json",
                )

    def test_services_have_complete_translations_and_current_icons(self) -> None:
        services_text = (
            ROOT / "custom_components/beestat_statistics/services.yaml"
        ).read_text(encoding="utf-8")
        translations = _json_file(
            "custom_components/beestat_statistics/translations/en.json"
        )
        icons = _json_file("custom_components/beestat_statistics/icons.json")
        service_matches = list(
            re.finditer(r"^([a-z_]+):$", services_text, re.MULTILINE)
        )
        service_keys = {match.group(1) for match in service_matches}

        self.assertEqual(service_keys, set(translations["services"]))
        self.assertEqual(service_keys, set(icons["services"]))
        for index, match in enumerate(service_matches):
            key = match.group(1)
            block_end = (
                service_matches[index + 1].start()
                if index + 1 < len(service_matches)
                else len(services_text)
            )
            service_block = services_text[match.end() : block_end]
            field_keys = set(
                re.findall(r"^    ([a-z_][a-z0-9_]*):$", service_block, re.MULTILINE)
            )
            self.assertIn("name", translations["services"][key])
            self.assertIn("description", translations["services"][key])
            self.assertEqual(field_keys, set(translations["services"][key]["fields"]))
            for field in translations["services"][key]["fields"].values():
                self.assertIn("name", field)
                self.assertIn("description", field)
            self.assertEqual(set(icons["services"][key]), {"service"})
            self.assertRegex(icons["services"][key]["service"], r"^mdi:[a-z0-9-]+$")

    def test_custom_integration_translations_do_not_use_core_references(self) -> None:
        translations_text = (
            ROOT / "custom_components/beestat_statistics/translations/en.json"
        ).read_text(encoding="utf-8")

        self.assertNotIn("[%key:", translations_text)

    def test_options_abort_translation_is_scoped_to_options_flow(self) -> None:
        """Options-flow abort reasons belong under the options namespace."""

        translations = _json_file(
            "custom_components/beestat_statistics/translations/en.json"
        )

        self.assertNotIn("abort", translations)
        self.assertTrue(
            translations["options"]["abort"]["no_automatic_mappings"].strip()
        )

    def test_validate_workflow_is_change_driven_or_manual(self) -> None:
        workflow = (ROOT / ".github/workflows/validate.yaml").read_text(
            encoding="utf-8"
        )

        self.assertNotRegex(workflow, r"(?m)^  schedule:\s*$")
        self.assertRegex(workflow, r"(?m)^  push:\s*$")
        self.assertRegex(workflow, r"(?m)^  pull_request:\s*$")
        self.assertRegex(workflow, r"(?m)^  workflow_dispatch:\s*$")

    def test_workflows_pin_actions_and_cover_supported_ha_versions(self) -> None:
        workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / ".github/workflows").iterdir())
            if path.suffix in {".yaml", ".yml"}
        )
        action_refs = re.findall(r"(?m)^\s*- uses: [^@\s]+@([^\s#]+)", workflows)

        self.assertGreater(len(action_refs), 0)
        for action_ref in action_refs:
            self.assertRegex(action_ref, r"^[0-9a-f]{40}$")
        self.assertEqual(
            workflows.count("runs-on:"),
            workflows.count("timeout-minutes:"),
        )
        self.assertEqual(
            workflows.count("uses: actions/checkout@"),
            workflows.count("persist-credentials: false"),
        )

        validate = (ROOT / ".github/workflows/validate.yaml").read_text(
            encoding="utf-8"
        )
        release_runner = (ROOT / "scripts/verify-release-local.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("bash scripts/verify-release-local.sh minimum native", validate)
        self.assertIn("bash scripts/verify-release-local.sh current native", validate)
        self.assertIn("requirements-ha-test.txt", release_runner)
        self.assertIn("requirements-ha-current.txt", release_runner)
        self.assertIn("name: Release gate", validate)
        self.assertIn(
            "needs: [plan, unit, home_assistant_minimum, home_assistant_current, hassfest, hacs]",
            validate,
        )

    def test_reported_sensor_use_labels_and_icons_do_not_imply_occupancy(self) -> None:
        """Reported upstream metadata should use neutral sensor icons."""

        translations = _json_file(
            "custom_components/beestat_statistics/translations/en.json"
        )
        self.assertEqual(
            translations["entity"]["sensor"]["active_sensor_count"]["name"],
            "Beestat-reported in-use sensor count",
        )
        self.assertEqual(
            translations["entity"]["binary_sensor"]["sensor_in_use"]["name"],
            "Beestat-reported sensor in use",
        )

        icons = _json_file("custom_components/beestat_statistics/icons.json")
        sensor_in_use = icons["entity"]["binary_sensor"]["sensor_in_use"]
        active_sensor_count = icons["entity"]["sensor"]["active_sensor_count"]

        self.assertEqual("mdi:home-thermometer", sensor_in_use["default"])
        self.assertEqual("mdi:home-thermometer", sensor_in_use["state"]["off"])
        self.assertEqual("mdi:thermometer-check", sensor_in_use["state"]["on"])
        self.assertEqual("mdi:home-thermometer", active_sensor_count["default"])
        self.assertEqual("mdi:home-thermometer", active_sensor_count["range"]["0"])
        self.assertEqual("mdi:thermometer-check", active_sensor_count["range"]["1"])
        self.assertNotIn("account", str(sensor_in_use))
        self.assertNotIn("account", str(active_sensor_count))

    def test_entity_unique_ids_do_not_repeat_integration_scope(self) -> None:
        const_text = (ROOT / "custom_components/beestat_statistics/const.py").read_text(
            encoding="utf-8"
        )
        sensor_text = (
            ROOT / "custom_components/beestat_statistics/sensor.py"
        ).read_text(encoding="utf-8")
        button_text = (
            ROOT / "custom_components/beestat_statistics/button.py"
        ).read_text(encoding="utf-8")
        date_text = (ROOT / "custom_components/beestat_statistics/date.py").read_text(
            encoding="utf-8"
        )
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")

        self.assertIn('return f"thermostat_{thermostat_id}_{suffix}"', const_text)
        self.assertIn('return f"sensor_{sensor_id}_{suffix}"', const_text)
        self.assertNotIn('return f"beestat_', const_text)
        self.assertNotIn('        key="beestat_', sensor_text)
        self.assertNotIn('        key="beestat_', button_text)
        self.assertNotIn('        key="beestat_', date_text)
        self.assertIn('mappings[f"beestat_{new_unique_id}"]', init_text)


def _class_method(
    tree: ast.AST,
    class_name: str,
    method_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == method_name
            ):
                return item
    return None


def _class_method_source(text: str, class_name: str, method_name: str) -> str:
    tree = ast.parse(text)
    node = _class_method(tree, class_name, method_name)
    if node is None:
        raise AssertionError(f"{class_name}.{method_name} is missing")
    return ast.get_source_segment(text, node) or ""


def _is_logger_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_LOGGER"
    )


def _json_file(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def _literal_translation_keys(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return set(
        re.findall(r'_attr_translation_key\s*=\s*"([^"]+)"', text)
        + re.findall(r'ThermostatSettingSensorSpec\(\s*"([^"]+)"', text)
        + re.findall(
            (
                r"(?:Button|BeestatSensor)EntityDescription\("
                r'[\s\S]*?translation_key\s*=\s*"([^"]+)"'
            ),
            text,
        )
    )


if __name__ == "__main__":
    unittest.main()

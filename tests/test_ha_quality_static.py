"""Static checks for Home Assistant integration-quality conventions."""

from __future__ import annotations

import ast
import json
import re
import tomllib
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

    def test_coordinator_entities_preserve_coordinator_availability(self) -> None:
        for relative_path, class_names in {
            "custom_components/beestat_statistics/sensor.py": {
                "BeestatSensor",
            },
            "custom_components/beestat_statistics/binary_sensor.py": {
                "BeestatFilterDueProblemBinarySensor",
                "BeestatCloudDataStaleProblemBinarySensor",
                "BeestatSensorInUseBinarySensor",
                "BeestatThermostatAlertProblemBinarySensor",
                "BeestatRuntimeStaleProblemBinarySensor",
            },
        }.items():
            tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
            for class_name in class_names:
                method = _class_method(tree, class_name, "available")
                self.assertIsNotNone(method, f"{class_name}.available is missing")
                self.assertTrue(
                    _contains_super_available(method),
                    f"{class_name}.available must include super().available",
                )

    def test_status_sensor_can_surface_coordinator_errors(self) -> None:
        text = (ROOT / "custom_components/beestat_statistics/sensor.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("uses_coordinator_availability: bool = True", text)
        self.assertIn('translation_key="status"', text)
        self.assertIn("uses_coordinator_availability=False", text)
        self.assertIn("_mapping_summary_attributes(data)", text)
        for attribute in (
            '"last_import_source_rows"',
            '"mapped_room_sensor_count"',
            '"mapped_thermostat_count"',
            '"room_sensor_count"',
            '"thermostat_count"',
            '"unmapped_room_sensor_count"',
            '"unmapped_thermostat_count"',
        ):
            self.assertIn(attribute, text)

    def test_importer_uses_windowed_summary_refresh_with_lazy_full_fallback(
        self,
    ) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")
        coordinator_text = (
            ROOT / "custom_components/beestat_statistics/coordinator.py"
        ).read_text(encoding="utf-8")
        sensor_text = (
            ROOT / "custom_components/beestat_statistics/sensor.py"
        ).read_text(encoding="utf-8")

        self.assertIn("summary_window=not force_full_summary", init_text)
        self.assertIn("async def _async_full_summary_rows", init_text)
        self.assertNotIn("full_rows = list(runtime_data.summary_rows)", init_text)
        self.assertIn("summary_window: bool = False", coordinator_text)
        self.assertIn("async_read_runtime_thermostat_summary", coordinator_text)
        self.assertNotIn("last_filter_alert_dismiss_thermostat_id", sensor_text)

    def test_recorder_statistics_reads_use_recorder_executor(self) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "from homeassistant.helpers.recorder import "
            "get_instance as get_recorder_instance",
            init_text,
        )
        self.assertIn(
            "get_recorder_instance(self._hass).async_add_executor_job",
            init_text,
        )

    def test_config_flow_exposes_required_user_recovery_paths(self) -> None:
        path = ROOT / "custom_components/beestat_statistics/config_flow.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        flow_methods = _class_method_names(tree, "BeestatStatisticsConfigFlow")
        options_methods = _class_method_names(tree, "BeestatStatisticsOptionsFlow")
        text = path.read_text(encoding="utf-8")

        self.assertTrue(
            {
                "async_get_options_flow",
                "async_step_import",
                "async_step_reauth",
                "async_step_reauth_confirm",
                "async_step_account_change_confirm",
                "async_step_reconfigure",
                "async_step_user",
            }.issubset(flow_methods)
        )
        self.assertIn("async_step_init", options_methods)
        self.assertIn("async_step_timing", options_methods)
        self.assertIn("async_step_source_scope", options_methods)
        self.assertIn("async_step_source_scope_confirm", options_methods)
        self.assertIn("async_step_confirm_automatic_mappings", options_methods)
        self.assertIn("async_step_thermostat_mapping", options_methods)
        self.assertIn("async_step_thermostat_mapping_detail", options_methods)
        self.assertIn("async_step_sensor_mapping", options_methods)
        self.assertIn("async_step_sensor_mapping_detail", options_methods)
        self.assertIn("_async_validate_input", text)
        self.assertIn('async_read_id("thermostat")', text)
        self.assertIn("_account_fingerprint", text)
        self.assertIn("CONF_ACCOUNT_FINGERPRINT", text)
        self.assertIn("_abort_if_unique_id_configured", text)
        self.assertIn("_abort_if_unique_id_mismatch", text)
        self.assertIn("async_update_reload_and_abort", text)
        self.assertIn("require_api_key=True", text)
        self.assertIn('"api_key_required"', text)
        self.assertIn('errors["base"] = "unknown"', text)
        self.assertIn(
            '"Unexpected exception validating Beestat setup (%s)"',
            text,
        )
        self.assertIn("exception_fingerprint(err)", text)
        self.assertIn("NumberSelector(", text)
        self.assertIn("NumberSelectorMode.BOX", text)
        self.assertIn("EntitySelector(", text)
        self.assertIn("SelectSelector(", text)
        self.assertIn("TextSelectorType.URL", text)
        self.assertIn("data, options = split_entry_payload(user_input)", text)
        self.assertIn(
            "entry.options, data, options, existing_data=entry_data_snapshot", text
        )
        self.assertIn("options_from_user_input(user_input)", text)
        self.assertIn("OPTIONS_MENU = {", text)
        self.assertIn("menu_options=OPTIONS_MENU", text)
        self.assertIn("description_placeholders=_thermostat_placeholders", text)
        self.assertIn("description_placeholders=_sensor_placeholders", text)
        self.assertNotIn(
            "_async_validate_input(\n                        self.hass,\n                        user_input",
            text,
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
            strings["options"]["step"]["init"]["menu_options"],
            {
                "timing": "Import timing",
                "source_scope": "Choose Beestat sources",
                "confirm_automatic_mappings": "Confirm automatic mappings",
                "thermostat_mapping": "Map a thermostat",
                "sensor_mapping": "Map a room sensor",
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

    def test_diagnostics_redact_shareable_identifiers(self) -> None:
        text = (ROOT / "custom_components/beestat_statistics/diagnostics.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("CONF_API_KEY", text)
        self.assertIn("CONF_ACCOUNT_FINGERPRINT", text)
        for key in (
            "id",
            "identifier",
            "sensor_id",
            "thermostat_id",
            "thermostat_slug",
        ):
            self.assertIn(f'"{key}"', text)
        for key in (
            "CONF_CLIMATE_ENTITY_ID",
            "CONF_TEMPERATURE_ENTITY_ID",
            "CONF_OCCUPANCY_ENTITY_ID",
            "CONF_MOTION_ENTITY_ID",
        ):
            self.assertIn(key, text)
        self.assertIn('"thermostats": _thermostat_diagnostics(data)', text)
        self.assertNotIn("_thermostat_diagnostics_by_slug", text)
        self.assertIn('"last_error": _redacted_text(', text)
        self.assertIn("CONF_API_BASE", text)
        self.assertIn("return async_redact_data(diagnostics, TO_REDACT)", text)

    def test_runtime_data_is_config_entry_owned(self) -> None:
        for path in (ROOT / "custom_components/beestat_statistics").glob("*.py"):
            self.assertNotIn("hass.data", path.read_text(encoding="utf-8"))

        runtime_text = (
            ROOT / "custom_components/beestat_statistics/runtime.py"
        ).read_text(encoding="utf-8")
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "type BeestatStatisticsConfigEntry = ConfigEntry[BeestatStatisticsRuntime]",
            runtime_text,
        )
        self.assertIn("entry.runtime_data = runtime", init_text)
        self.assertIn("ConfigEntryState.LOADED", init_text)
        self.assertIn("entry.async_create_background_task", init_text)
        self.assertNotIn("hass.async_create_task", init_text)
        self.assertIn("eager_start=False", init_text)

    def test_setup_lifecycle_and_action_paths_follow_quality_rules(self) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")
        coordinator_text = (
            ROOT / "custom_components/beestat_statistics/coordinator.py"
        ).read_text(encoding="utf-8")
        button_text = (
            ROOT / "custom_components/beestat_statistics/button.py"
        ).read_text(encoding="utf-8")
        self.assertIn("hass.services.async_register(", init_text)
        self.assertIn("supports_response=SupportsResponse.ONLY", init_text)
        self.assertIn("SERVICE_GET_CONFIGURATION", init_text)
        self.assertIn("ServiceValidationError", init_text)
        self.assertIn("HomeAssistantError", init_text)
        self.assertIn("vol.Length(min=1)", init_text)
        self.assertIn("async_config_entry_first_refresh()", init_text)
        self.assertIn("async def async_unload_entry", init_text)
        self.assertIn("async_unload_platforms(entry, PLATFORMS)", init_text)
        self.assertIn("entry.async_on_unload(", init_text)
        self.assertIn("async_track_state_change_event(", init_text)
        self.assertIn("async_track_time_interval(", init_text)
        self.assertIn("async_register_service_device(hass, entry)", init_text)
        self.assertIn("ConfigEntryAuthFailed", coordinator_text)
        self.assertIn("raise UpdateFailed", coordinator_text)
        self.assertIn("HomeAssistantError", button_text)
        self.assertIn("async_start_reauth_if_available", button_text)
        self.assertIn(
            '"Unexpected Beestat statistics import service failure (%s)"',
            init_text,
        )
        self.assertIn(
            '"Unexpected Beestat button failure during %s (%s)"',
            button_text,
        )

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
        self.assertTrue(release_notes.startswith("# Release notes\n"))
        self.assertTrue(versions, "Release notes must identify released versions")
        self.assertEqual(versions[0], manifest["version"])

    def test_quality_scale_tracks_claimed_home_assistant_rules(self) -> None:
        quality_scale_path = (
            ROOT / "custom_components/beestat_statistics/quality_scale.yaml"
        )
        text = quality_scale_path.read_text(encoding="utf-8")

        self.assertIn("rules:\n", text)
        for rule in (
            "action-setup",
            "config-flow",
            "config-flow-test-coverage",
            "diagnostics",
            "entity-device-class",
            "has-entity-name",
            "inject-websession",
            "reauthentication-flow",
            "repair-issues",
            "runtime-data",
            "stale-devices",
            "strict-typing",
        ):
            self.assertIn(f"  {rule}: done", text)

        for exempt_rule in (
            "discovery",
            "discovery-update-info",
            "docs-conditions",
            "docs-triggers",
        ):
            self.assertRegex(
                text,
                rf"  {re.escape(exempt_rule)}:\n    status: exempt\n    comment: .+",
            )

        self.assertIsNone(re.search(r"^  test-coverage:", text, re.MULTILINE))

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
        for lane, requirements in (
            ("minimum", "requirements-ha-test.txt"),
            ("current", "requirements-ha-current.txt"),
        ):
            with self.subTest(lane=lane):
                block = runner.split(f"run_{lane}() {{", 1)[1].split("\n}\n", 1)[0]
                self.assertRegex(
                    block,
                    r'python -m pip install "pytest-homeassistant-custom-component==[0-9.]+"',
                )
                install = f"python -m pip install --upgrade -r {requirements}"
                self.assertLess(
                    block.index("pytest-homeassistant-custom-component"),
                    block.index(install),
                )
                self.assertLess(
                    block.rindex("python -m pip install"),
                    block.index("python -m pip check"),
                )
                self.assertLess(
                    block.index("python -m pip check"),
                    block.index(
                        "python scripts/run_dependency_light_tests.py --home-assistant"
                    ),
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

    def test_ruff_policy_is_repository_owned_and_high_signal(self) -> None:
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        ruff = config["tool"]["ruff"]
        lint = ruff["lint"]

        self.assertEqual("py314", ruff["target-version"])
        self.assertNotIn("required-version", ruff)
        self.assertEqual(11, lint["mccabe"]["max-complexity"])
        self.assertTrue(
            {
                "ASYNC",
                "B",
                "BLE",
                "C4",
                "C901",
                "DTZ",
                "LOG",
                "N818",
                "PERF",
                "PLC",
                "PLE",
                "PLW",
                "RUF",
                "S104",
                "S113",
                "S310",
                "S314",
                "S324",
                "S501",
                "S506",
                "S507",
                "TID",
            }
            <= set(lint["extend-select"])
        )
        self.assertTrue({"RUF001", "RUF002", "RUF003"}.isdisjoint(lint["ignore"]))
        self.assertEqual(["T20"], lint["per-file-ignores"]["scripts/**"])
        self.assertTrue(config["tool"]["mypy"]["strict"])
        self.assertNotIn("overrides", config["tool"]["mypy"])

    def test_development_guide_matches_validation_owners(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        development = (ROOT / "docs/development.md").read_text(encoding="utf-8")
        self.assertIn("docs/development.md", readme)
        for name in ("requirements-ha-test.txt", "requirements-ha-current.txt"):
            version = (ROOT / name).read_text(encoding="utf-8").strip().split("==")[1]
            self.assertIn(name, development)
            self.assertIn(f"`{version}`", development)
        self.assertIn("scripts/verify-release-local.ps1", development)
        self.assertIn("bash scripts/verify-release-local.sh all", development)
        self.assertIn("python -m pip check", development)

    def test_discovered_ha_modules_fail_closed_without_harness(self) -> None:
        release_runner = (ROOT / "scripts/verify-release-local.sh").read_text(
            encoding="utf-8"
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        dependency_light_runner = (
            ROOT / "scripts/run_dependency_light_tests.py"
        ).read_text(encoding="utf-8")
        test_files = tuple(sorted((ROOT / "tests").rglob("test_*.py")))
        discovered_ha_filenames = {
            path.name for path in discover_home_assistant_test_files(test_files)
        }
        self.assertEqual(
            discovered_ha_filenames,
            {
                "test_config_flow_ha.py",
                "test_coordinator_runtime_ha.py",
                "test_runtime_ha.py",
                "test_entity_runtime_ha.py",
                "test_filter_actions_ha.py",
                "test_filter_lifecycle_ha.py",
                "test_setup_cancellation_ha.py",
                "test_source_identity_ha.py",
            },
        )
        ha_modules = tuple(
            f"tests/{filename}" for filename in sorted(discovered_ha_filenames)
        )

        self.assertEqual(
            2,
            release_runner.count(
                "python scripts/run_dependency_light_tests.py --home-assistant"
            ),
        )
        self.assertIn("python scripts/run_dependency_light_tests.py", release_runner)
        development = (ROOT / "docs/development.md").read_text(encoding="utf-8")
        self.assertIn(
            r".\.venv\Scripts\python.exe scripts\run_dependency_light_tests.py",
            development,
        )
        self.assertIn("docs/development.md", readme)
        self.assertNotIn("python -m unittest discover -s tests", release_runner)
        self.assertIn("if path not in ha_test_files", dependency_light_runner)
        for relative_path in ha_modules:
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("from homeassistant", text)
                self.assertNotIn("unittest.SkipTest", text)
                self.assertNotIn("except ModuleNotFoundError", text)

    def test_http_client_uses_home_assistant_async_websession(self) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")
        config_flow_text = (
            ROOT / "custom_components/beestat_statistics/config_flow.py"
        ).read_text(encoding="utf-8")
        api_text = (ROOT / "custom_components/beestat_statistics/api.py").read_text(
            encoding="utf-8"
        )
        manifest = _json_file("custom_components/beestat_statistics/manifest.json")

        self.assertEqual(manifest["requirements"], [])
        self.assertIn("async_get_clientsession", init_text)
        self.assertIn("async_get_clientsession", config_flow_text)
        self.assertIn("aiohttp.ClientSession", api_text)
        self.assertIn("asyncio.timeout", api_text)
        self.assertNotIn("async_timeout", api_text)
        self.assertIn("async with self._session.get", api_text)
        self.assertIn("allow_boolean_response=True", api_text)
        self.assertIn("Unexpected response data shape: bool", api_text)
        self.assertIn("Unexpected response row shape", api_text)

    def test_stale_devices_can_be_removed_without_touching_homekit_devices(
        self,
    ) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")
        self.assertIn("async def async_remove_config_entry_device", init_text)
        self.assertIn("_current_beestat_device_identifiers", init_text)
        self.assertIn("is_beestat_only_device", init_text)
        self.assertIn("_async_migrate_homekit_device_assignments", init_text)
        self.assertIn("device_id=target_device_id", init_text)
        self.assertIn("async_remove_device", init_text)
        self.assertIn("beestat_identifiers.isdisjoint", init_text)

    def test_mapped_entities_use_home_assistant_helper_device_linking(self) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")
        entity_text = (
            ROOT / "custom_components/beestat_statistics/entity.py"
        ).read_text(encoding="utf-8")

        self.assertIn("entity.device_entry = device_entry", entity_text)
        self.assertIn("if thermostat.device_id is not None", entity_text)
        self.assertIn("if sensor.device_id is not None", entity_text)
        self.assertNotIn("via_device=", entity_text)
        self.assertNotIn("async_get_device", init_text)
        self.assertIn("async_remove_cross_integration_device_ownership", init_text)
        self.assertIn("async_remove_helper_devices", entity_text)
        self.assertIn(
            "async_remove_helper_config_entry_from_source_device",
            entity_text,
        )

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
            self.assertIn("AddConfigEntryEntitiesCallback", text)
            self.assertNotIn("AddEntitiesCallback", text)

    def test_entity_metadata_uses_native_classes_categories_and_noisy_defaults(
        self,
    ) -> None:
        sensor_text = (
            ROOT / "custom_components/beestat_statistics/sensor.py"
        ).read_text(encoding="utf-8")
        binary_text = (
            ROOT / "custom_components/beestat_statistics/binary_sensor.py"
        ).read_text(encoding="utf-8")
        button_text = (
            ROOT / "custom_components/beestat_statistics/button.py"
        ).read_text(encoding="utf-8")
        for snippet in (
            "device_class=SensorDeviceClass.DATE",
            "device_class=SensorDeviceClass.DURATION",
            "device_class=SensorDeviceClass.TIMESTAMP",
            "state_class=SensorStateClass.MEASUREMENT",
            "entity_registry_enabled_default=False",
            "entity_category=EntityCategory.DIAGNOSTIC",
        ):
            self.assertIn(snippet, sensor_text)

        self.assertIn("BinarySensorDeviceClass.PROBLEM", binary_text)
        self.assertIn("_attr_entity_registry_enabled_default = False", binary_text)
        self.assertIn("EntityCategory.DIAGNOSTIC", binary_text)
        self.assertIn("entity_category=EntityCategory.DIAGNOSTIC", button_text)
        self.assertIn("EntityCategory.CONFIG", button_text)

    def test_diagnostic_attributes_are_excluded_from_recorder_history(self) -> None:
        sensor_text = (
            ROOT / "custom_components/beestat_statistics/sensor.py"
        ).read_text(encoding="utf-8")
        binary_text = (
            ROOT / "custom_components/beestat_statistics/binary_sensor.py"
        ).read_text(encoding="utf-8")
        date_text = (ROOT / "custom_components/beestat_statistics/date.py").read_text(
            encoding="utf-8"
        )

        for text, snippets in {
            sensor_text: (
                "_unrecorded_attributes = frozenset(",
                '"last_error"',
                '"profiles"',
                '"active_alerts"',
            ),
            binary_text: (
                "_unrecorded_attributes = frozenset(",
                '"beestat_name"',
                '"active_alerts"',
            ),
            date_text: (
                "_unrecorded_attributes = frozenset(",
                '"change_day_runtime_baseline_seconds"',
                '"legacy_helper_entity_id"',
            ),
        }.items():
            for snippet in snippets:
                self.assertIn(snippet, text)

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

    def test_per_thermostat_sensors_go_unavailable_when_source_disappears(self) -> None:
        sensor_text = (
            ROOT / "custom_components/beestat_statistics/sensor.py"
        ).read_text(encoding="utf-8")

        self.assertIn("def _summary_available", sensor_text)
        self.assertIn("def _thermostat_metadata_available", sensor_text)
        self.assertGreaterEqual(sensor_text.count("available_fn=partial("), 11)
        self.assertNotIn(
            "lambda coordinator, thermostat_id=thermostat_id",
            sensor_text,
        )

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
        issues_text = (
            ROOT / "custom_components/beestat_statistics/issues.py"
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
        self.assertEqual(
            exception_keys,
            {
                "beestat_auth_failed",
                "beestat_request_failed",
                "invalid_api_base",
                "filter_change_boundary_date_mismatch",
                "filter_change_boundary_local_time_invalid",
                "filter_change_boundary_out_of_range",
                "no_loaded_entry",
                "invalid_rebuild_date_range",
                "unknown_thermostat_id",
                "statistics_import_failed",
            },
        )
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

        self.assertIn("async_record_import_error", coordinator_text)
        self.assertIn("def _async_record_error", coordinator_text)
        self.assertIn(
            "self.last_error = self._client.redact_error(err)", coordinator_text
        )
        self.assertNotIn("self.last_error = str(err)", coordinator_text)

        self.assertIn("ir.async_create_issue(", init_text)
        self.assertIn("er.EVENT_ENTITY_REGISTRY_UPDATED", init_text)
        self.assertIn("_async_track_override_issue_updates(hass, entry)", init_text)
        self.assertIn("_MISSING_OVERRIDE_ENTITIES_ISSUE_ID", init_text)
        self.assertIn("_INVALID_OVERRIDE_ENTITY_DOMAINS_ISSUE_ID", init_text)
        self.assertIn("_MAPPING_DEVICE_CONFLICTS_ISSUE_ID", init_text)
        self.assertIn("entry_runtime_config_data", init_text)
        self.assertIn(
            "_missing_override_entity_ids(hass, entry_runtime_config_data(entry))",
            init_text,
        )
        self.assertIn(
            "configured_override_entity_domain_errors(entry_runtime_config_data(entry))",
            init_text,
        )
        self.assertIn(
            "entry.async_on_unload(\n        hass.bus.async_listen(",
            init_text,
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
        self.assertIn("ir.async_create_issue(", issues_text)
        self.assertIn("ir.async_delete_issue(", issues_text)

    def test_manual_import_failures_update_status_diagnostics(self) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")
        button_text = (
            ROOT / "custom_components/beestat_statistics/button.py"
        ).read_text(encoding="utf-8")

        self.assertGreaterEqual(
            init_text.count("runtime.coordinator.async_record_import_error(err)"),
            3,
        )
        self.assertGreaterEqual(
            button_text.count("self._coordinator.async_record_import_error(err)"),
            3,
        )

    def test_skipped_window_logs_do_not_format_api_exceptions(self) -> None:
        """CodeQL-sensitive logs must not read possibly credential-bearing text."""

        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("self._client.redact_error(err)", init_text)
        self.assertGreaterEqual(init_text.count("exception_fingerprint(err)"), 5)

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

    def test_repository_support_templates_reduce_secret_leak_risk(self) -> None:
        bug_template = (ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("Integration version", bug_template)
        self.assertIn("Home Assistant version", bug_template)
        self.assertIn("Redacted diagnostics and logs", bug_template)
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
                re.findall(r"^    ([a-z_]+):$", service_block, re.MULTILINE)
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
        self.assertEqual(
            translations["options"]["abort"]["no_automatic_mappings"],
            "No unconfirmed automatic mappings are currently available. "
            "Existing explicit mappings were left unchanged.",
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
        self.assertNotIn("matrix:", validate)
        self.assertIn("bash scripts/verify-release-local.sh minimum native", validate)
        self.assertIn("bash scripts/verify-release-local.sh current native", validate)
        self.assertIn("requirements-ha-test.txt", release_runner)
        self.assertIn("requirements-ha-current.txt", release_runner)
        self.assertIn("name: Release gate", validate)
        self.assertIn(
            "needs: [unit, home_assistant_minimum, home_assistant_current, hassfest, hacs]",
            validate,
        )

    def test_device_entity_names_do_not_repeat_integration_name(self) -> None:
        strings = _json_file(
            "custom_components/beestat_statistics/translations/en.json"
        )

        device_backed_keys = {
            "binary_sensor": {
                "active_alert",
                "cloud_data_stale",
                "equipment_alert",
                "filter_due",
                "filter_due_soon",
                "runtime_summary_stale",
                "sensor_in_use",
            },
            "date": {"filter_changed_date"},
            "sensor": {
                "active_alert_count",
                "active_alert_category",
                "active_sensor_count",
                "cloud_data_end",
                "cloud_data_lag_minutes",
                "current_comfort_profile",
                "filter_days_remaining",
                "filter_due_date",
                "filter_max_age_due_date",
                "filter_recent_runtime_hours_per_day",
                "filter_remaining_runtime_hours",
                "filter_runtime_hours",
                "filter_runtime_due_date",
                "next_scheduled_comfort_profile_time",
                "runtime_summary_lag_days",
                "runtime_summary_latest_date",
                "scheduled_comfort_profile",
            },
        }
        for platform, keys in device_backed_keys.items():
            for key in keys:
                name = strings["entity"][platform][key]["name"]
                self.assertFalse(
                    name.lower().startswith("beestat "),
                    f"{platform}.{key} repeats the integration name in '{name}'",
                )

    def test_entities_define_explicit_runtime_names(self) -> None:
        """Entity classes should not depend on translations for basic HA names."""

        binary_sensor_text = (
            ROOT / "custom_components/beestat_statistics/binary_sensor.py"
        ).read_text(encoding="utf-8")
        button_text = (
            ROOT / "custom_components/beestat_statistics/button.py"
        ).read_text(encoding="utf-8")
        date_text = (ROOT / "custom_components/beestat_statistics/date.py").read_text(
            encoding="utf-8"
        )
        sensor_text = (
            ROOT / "custom_components/beestat_statistics/sensor.py"
        ).read_text(encoding="utf-8")

        for expected in (
            'name="Runtime summary latest date"',
            'name="Runtime summary lag days"',
            'name="Current comfort profile"',
            'name="Beestat-reported in-use sensor count"',
            'name="Filter runtime hours"',
            'name="Filter recent runtime hours per day"',
            'name="Filter due date"',
        ):
            self.assertIn(expected, sensor_text)
        for expected in (
            '_attr_name = "Beestat-reported sensor in use"',
            '_attr_name = "Active alert"',
            '_attr_name = "Equipment alert"',
            '_attr_name = "Filter due"',
            '_attr_name = "Filter due soon"',
            '_attr_name = "HomeKit mapping incomplete"',
            '_attr_name = "Import partial"',
            '_attr_name = "Runtime summary stale"',
            '_attr_name = "Cloud data stale"',
        ):
            self.assertIn(expected, binary_sensor_text)
        self.assertIn('name="Refresh runtime"', button_text)
        self.assertIn('name="Import statistics"', button_text)
        self.assertIn('_attr_name = "Mark filter changed"', button_text)
        self.assertIn('_attr_name = "Filter changed date"', date_text)

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

    def test_reported_sensor_use_icons_do_not_imply_occupancy(self) -> None:
        """Reported upstream metadata should use neutral sensor icons."""

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
        self.assertIn("_GLOBAL_UNIQUE_ID_MIGRATION", init_text)
        self.assertIn("_DEFAULT_ENABLED_PROBLEM_ENTITY_SUFFIXES", init_text)
        self.assertIn("_async_enable_default_problem_entities", init_text)
        self.assertIn("RegistryEntryDisabler.INTEGRATION", init_text)
        self.assertIn("_default_problem_entity_id", init_text)
        self.assertIn("_is_generic_problem_entity_id", init_text)
        self.assertIn("new_entity_id", init_text)


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


def _class_method_names(tree: ast.AST, class_name: str) -> set[str]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        return {
            item.name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
    return set()


def _class_method_source(text: str, class_name: str, method_name: str) -> str:
    tree = ast.parse(text)
    node = _class_method(tree, class_name, method_name)
    if node is None:
        raise AssertionError(f"{class_name}.{method_name} is missing")
    return ast.get_source_segment(text, node) or ""


def _contains_super_available(node: ast.AST | None) -> bool:
    if node is None:
        return False
    for item in ast.walk(node):
        if not isinstance(item, ast.Attribute) or item.attr != "available":
            continue
        value = item.value
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        if isinstance(func, ast.Name) and func.id == "super":
            return True
    return False


def _is_logger_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_LOGGER"
    )


def _contains_method_call(node: ast.AST | None, method_name: str) -> bool:
    if node is None:
        return False
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        func = item.func
        if isinstance(func, ast.Attribute) and func.attr == method_name:
            return True
    return False


def _json_file(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def _literal_translation_keys(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return set(
        re.findall(r'_attr_translation_key\s*=\s*"([^"]+)"', text)
        + re.findall(
            (
                r"Beestat(?:Button|Sensor)EntityDescription\("
                r'[\s\S]*?translation_key\s*=\s*"([^"]+)"'
            ),
            text,
        )
    )


if __name__ == "__main__":
    unittest.main()

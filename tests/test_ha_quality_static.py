"""Static checks for Home Assistant integration-quality conventions."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class HomeAssistantQualityStaticTest(unittest.TestCase):
    """Validate HA quality rules that can be checked without HA test deps."""

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
        text = (
            ROOT / "custom_components/beestat_statistics/sensor.py"
        ).read_text(encoding="utf-8")
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

    def test_manual_refresh_failures_update_coordinator_availability(self) -> None:
        tree = ast.parse(
            (ROOT / "custom_components/beestat_statistics/coordinator.py").read_text(
                encoding="utf-8"
            )
        )
        method = _class_method(
            tree,
            "BeestatRuntimeDataCoordinator",
            "async_refresh_runtime",
        )
        self.assertIsNotNone(method, "async_refresh_runtime is missing")
        self.assertTrue(
            _contains_method_call(method, "async_set_update_error"),
            "Manual refresh failures must call async_set_update_error",
        )

    def test_importer_uses_windowed_summary_refresh_with_lazy_full_fallback(self) -> None:
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

    def test_init_datetime_date_alias_survives_date_platform_import(self) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")

        self.assertIn("from datetime import date as dt_date", init_text)
        self.assertIn("dt_date.fromisoformat", init_text)
        self.assertNotRegex(init_text, r"(?<!dt_)date\.fromisoformat")

    def test_recorder_statistics_reads_use_recorder_executor(self) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "from homeassistant.components.recorder import "
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
        self.assertIn("_LOGGER.exception", text)
        self.assertIn("NumberSelector(", text)
        self.assertIn("NumberSelectorMode.BOX", text)
        self.assertIn("EntitySelector(", text)
        self.assertIn("SelectSelector(", text)
        self.assertIn("TextSelectorType.URL", text)
        self.assertIn("data, options = split_entry_payload(user_input)", text)
        self.assertIn(
            "options = merge_import_options(entry.options, data, options)", text
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
        self.assertIn("TypeAlias", runtime_text)
        self.assertIn("BeestatStatisticsConfigEntry: TypeAlias", runtime_text)
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
            '_LOGGER.exception("Unexpected Beestat statistics import service failure")',
            init_text,
        )
        self.assertIn(
            '_LOGGER.exception("Unexpected Beestat button failure during %s", action)',
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
            (
                ROOT
                / "custom_components/beestat_statistics/brand/icon.png"
            ).is_file()
        )

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
        self.assertNotIn("strict-typing:", text)

    def test_ci_python_matches_advertised_home_assistant_target(self) -> None:
        hacs = _json_file("hacs.json")
        pytest_ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/validate.yaml").read_text(
            encoding="utf-8"
        )
        requirements = (ROOT / "requirements-ha-test.txt").read_text(
            encoding="utf-8"
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        config_flow_tests = (ROOT / "tests/test_config_flow_ha.py").read_text(
            encoding="utf-8"
        )
        required_pins = {
            "homeassistant==2026.7.1",
            "pytest==9.0.3",
            "pytest-homeassistant-custom-component==0.13.345",
        }

        self.assertEqual(hacs["homeassistant"], "2026.7.1")
        self.assertIn("asyncio_mode = auto", pytest_ini)
        self.assertIn('python-version: "3.14"', workflow)
        self.assertIn("Python `3.14.2` or newer", readme)
        self.assertTrue(required_pins <= set(requirements.splitlines()))
        self.assertIn("python -m pip install -r requirements-ha-test.txt", workflow)
        self.assertIn("requirements-ha-test.txt", readme)
        self.assertIn("pytest tests/test_config_flow_ha.py -q", workflow)
        self.assertIn("pytest tests/test_config_flow_ha.py -q", readme)
        self.assertIn("async_process_deps_reqs", config_flow_tests)

    def test_ha_config_flow_harness_covers_non_user_paths(self) -> None:
        text = (ROOT / "tests/test_config_flow_ha.py").read_text(encoding="utf-8")

        for snippet in (
            "SOURCE_IMPORT",
            "test_import_flow_creates_config_entry",
            "test_import_flow_updates_existing_entry",
            "test_user_flow_normalizes_copy_paste_whitespace",
            "test_user_flow_recovers_from_unexpected_error",
            "test_reauth_flow_updates_api_key",
            "test_reauth_flow_confirms_different_account",
            "test_reauth_flow_recovers_from_unexpected_error",
            "test_reauth_flow_rejects_blank_api_key",
            "test_reconfigure_flow_allows_blank_key_to_keep_current",
            "test_reconfigure_flow_confirms_different_account",
            "test_reconfigure_flow_recovers_from_unexpected_error",
            'result["errors"] == {"base": "invalid_auth"}',
            'result["errors"] == {"base": "cannot_connect"}',
            'result["step_id"] == "account_change_confirm"',
            'result["errors"] == {"base": "unknown"}',
            'result["errors"] == {CONF_API_KEY: "api_key_required"}',
        ):
            self.assertIn(snippet, text)

    def test_http_client_uses_home_assistant_async_websession(self) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")
        config_flow_text = (
            ROOT / "custom_components/beestat_statistics/config_flow.py"
        ).read_text(encoding="utf-8")
        api_text = (
            ROOT / "custom_components/beestat_statistics/api.py"
        ).read_text(encoding="utf-8")
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

    def test_stale_devices_can_be_removed_without_touching_homekit_devices(self) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("async def async_remove_config_entry_device", init_text)
        self.assertIn("_current_beestat_device_identifiers", init_text)
        self.assertIn("_async_migrate_homekit_device_assignments", init_text)
        self.assertIn("device_id=target_device_id", init_text)
        self.assertIn("async_remove_device", init_text)
        self.assertIn("beestat_identifiers.isdisjoint", init_text)
        self.assertIn("If a Beestat-only fallback device disappears", readme)
        self.assertIn("Shared HomeKit/Ecobee devices are not removed", readme)

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

    def test_entity_metadata_uses_native_classes_categories_and_noisy_defaults(self) -> None:
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
        self.assertNotIn("entity_registry_enabled_default = False", binary_text)
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
        self.assertGreaterEqual(
            sensor_text.count("available_fn=lambda coordinator, thermostat_id=thermostat_id"),
            11,
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

        exception_keys = set(
            re.findall(
                r"(?:HomeAssistantError|ServiceValidationError)\("
                r"[\s\S]*?translation_key=\"([^\"]+)\"",
                init_text + button_text,
            )
        )
        self.assertEqual(
            exception_keys,
            {
                "beestat_auth_failed",
                "beestat_request_failed",
                "no_loaded_entry",
                "invalid_rebuild_date_range",
                "unknown_thermostat_id",
                "statistics_import_failed",
            },
        )
        self.assertTrue(exception_keys <= set(strings["exceptions"]))

        coordinator_text = (
            ROOT / "custom_components/beestat_statistics/coordinator.py"
        ).read_text(encoding="utf-8")
        self.assertIn("async_record_import_error", coordinator_text)
        self.assertIn("def _async_record_error", coordinator_text)
        self.assertIn("self.last_error = self._client.redact_error(err)", coordinator_text)
        self.assertNotIn("self.last_error = str(err)", coordinator_text)

        self.assertIn("ir.async_create_issue(", init_text)
        self.assertIn("_MISSING_OVERRIDE_ENTITIES_ISSUE_ID", init_text)
        self.assertIn("_INVALID_OVERRIDE_ENTITY_DOMAINS_ISSUE_ID", init_text)
        self.assertIn("entry_runtime_config_data", init_text)
        self.assertIn(
            "_missing_override_entity_ids(hass, entry_runtime_config_data(entry))",
            init_text,
        )
        self.assertIn(
            "configured_override_entity_domain_errors(entry_runtime_config_data(entry))",
            init_text,
        )
        self.assertTrue(
            {
                "missing_override_entities",
                "invalid_override_entity_domains",
            }
            <= set(strings["issues"])
        )

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

    def test_import_lifecycle_uses_entity_state_not_custom_bus_events(self) -> None:
        init_text = (
            ROOT / "custom_components/beestat_statistics/__init__.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("bus.async_fire", init_text)

    def test_recorder_statistics_metadata_uses_current_shape(self) -> None:
        statistics_text = (
            ROOT / "custom_components/beestat_statistics/statistics_builder.py"
        ).read_text(encoding="utf-8")
        const_text = (
            ROOT / "custom_components/beestat_statistics/const.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn('"has_mean"', statistics_text)
        self.assertIn("STATISTIC_MEAN_TYPE_ARITHMETIC", statistics_text)
        self.assertIn("STATISTIC_MEAN_TYPE_NONE", statistics_text)
        self.assertIn("STATISTIC_UNIT_CLASS_TEMPERATURE", const_text)
        self.assertIn('UNIT_FAHRENHEIT = "\\N{DEGREE SIGN}F"', const_text)

    def test_stale_runtime_blueprint_is_documented_and_native(self) -> None:
        blueprint_path = (
            ROOT
            / "blueprints/automation/beestat_statistics/stale_runtime_notification.yaml"
        )
        blueprint = blueprint_path.read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("domain: automation", blueprint)
        self.assertIn("trigger: numeric_state", blueprint)
        self.assertIn("selector:\n        action: {}", blueprint)
        self.assertNotIn("trigger: template", blueprint)
        self.assertIn(str(blueprint_path.relative_to(ROOT)).replace("\\", "/"), readme)
        self.assertIn("raw.githubusercontent.com", readme)
        self.assertIn("my.home-assistant.io/redirect/hacs_repository", readme)

    def test_readme_covers_quality_documentation_rules(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for heading in (
            "# Beestat Statistics",
            "## Installation With HACS",
            "## Configuration",
            "## Entities",
            "## Data Updates",
            "## Automation Examples",
            "## Use Cases",
            "## Service Action",
            "## Diagnostics",
            "## Recorder Statistics",
            "## Supported Scope",
            "## Known Limitations",
            "## Troubleshooting",
            "## Development Validation",
            "## Release Publishing",
            "## Removal",
        ):
            self.assertIn(heading, readme)

        for phrase in (
            "Configuration fields:",
            "Advanced thermostat override fields:",
            "Advanced room-sensor override fields:",
            "This integration does not provide custom device triggers or conditions.",
            "No automation is required for normal operation.",
            "Map a thermostat",
            "Filter changed date",
            "Temperature statistics use Home Assistant recorder temperature metadata",
            "HomeKit/Ecobee entities should remain the primary source",
            "--notes-file",
            "excluded from Recorder history",
        ):
            self.assertIn(phrase, readme)

    def test_repository_support_templates_reduce_secret_leak_risk(self) -> None:
        bug_template = (
            ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("Integration version", bug_template)
        self.assertIn("Home Assistant version", bug_template)
        self.assertIn("Redacted diagnostics and logs", bug_template)
        self.assertIn("Do not paste API keys", bug_template)
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
            'name="Filter runtime hours"',
            'name="Filter recent runtime hours per day"',
            'name="Filter due date"',
        ):
            self.assertIn(expected, sensor_text)
        for expected in (
            '_attr_name = "Sensor in use"',
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

    def test_entity_unique_ids_do_not_repeat_integration_scope(self) -> None:
        const_text = (
            ROOT / "custom_components/beestat_statistics/const.py"
        ).read_text(encoding="utf-8")
        sensor_text = (
            ROOT / "custom_components/beestat_statistics/sensor.py"
        ).read_text(encoding="utf-8")
        button_text = (
            ROOT / "custom_components/beestat_statistics/button.py"
        ).read_text(encoding="utf-8")
        date_text = (
            ROOT / "custom_components/beestat_statistics/date.py"
        ).read_text(encoding="utf-8")
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


def _iter_text_files(paths: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    allowed_suffixes = {".json", ".md", ".py", ".yaml", ".yml"}
    for path in paths:
        if path.is_file():
            files.append(path)
            continue
        files.extend(
            item
            for item in path.rglob("*")
            if item.is_file() and item.suffix.lower() in allowed_suffixes
        )
    return files


def _literal_translation_keys(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return set(
        re.findall(r'_attr_translation_key\s*=\s*"([^"]+)"', text)
        + re.findall(
            (
                r'Beestat(?:Button|Sensor)EntityDescription\('
                r'[\s\S]*?translation_key\s*=\s*"([^"]+)"'
            ),
            text,
        )
    )


if __name__ == "__main__":
    unittest.main()

"""Static checks for Home Assistant integration-quality conventions."""

from __future__ import annotations

import ast
import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _exact_core_pin(group: str) -> str:
    """Read one unconditional exact Core pin from a dependency group."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        requirements = tomllib.load(handle)["dependency-groups"][group]
    pins = []
    for content in requirements:
        if not re.match(r"homeassistant(?=[^A-Za-z0-9_.-]|$)", content, re.IGNORECASE):
            continue
        match = re.fullmatch(
            r"homeassistant\s*==\s*([0-9]{4}\.(?:[1-9]|1[0-2])\.(?:0|[1-9][0-9]*))",
            content,
            re.IGNORECASE,
        )
        if match is None:
            raise AssertionError(
                f"{group} must use an unconditional stable exact Home Assistant pin"
            )
        pins.append(match.group(1))
    if len(pins) != 1:
        raise AssertionError(f"{group} must contain exactly one Home Assistant pin")
    return pins[0]


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

    def test_declared_minimum_matches_the_tested_support_floor(self) -> None:
        self.assertEqual(
            _exact_core_pin("ha-current"), _json_file("hacs.json")["homeassistant"]
        )

    def test_user_visible_exceptions_and_repairs_are_translated(self) -> None:
        strings = _json_file(
            "custom_components/beestat_statistics/translations/en.json"
        )
        texts = tuple(
            path.read_text(encoding="utf-8")
            for path in sorted(
                (ROOT / "custom_components/beestat_statistics").glob("*.py")
            )
        )

        translated_exception_names = {
            "ConfigEntryAuthFailed",
            "ConfigEntryError",
            "HomeAssistantError",
            "ServiceValidationError",
            "UpdateFailed",
        }
        trees = tuple(ast.parse(text) for text in texts)
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
            }
            <= set(strings["issues"])
        )

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

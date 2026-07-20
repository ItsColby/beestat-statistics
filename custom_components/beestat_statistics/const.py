"""Constants for the Beestat statistics integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

DOMAIN = "beestat_statistics"

API_BASE = "https://api.beestat.io/"
CONFIG_ENTRY_MINOR_VERSION = 3
CONFIG_ENTRY_UNIQUE_ID = "beestat_statistics"
CONFIG_ENTRY_VERSION = 1
CONFIG_TITLE = "Beestat Statistics"
STATISTIC_SOURCE = "beestat"
STATISTIC_MEAN_TYPE_NONE = 0
STATISTIC_MEAN_TYPE_ARITHMETIC = 1

STATISTIC_UNIT_CLASS_DURATION = "duration"
STATISTIC_UNIT_CLASS_TEMPERATURE = "temperature"
STATISTIC_UNIT_CLASS_UNITLESS = "unitless"
UNIT_FAHRENHEIT = "\N{DEGREE SIGN}F"

CONF_API_BASE = "api_base"
CONF_ACCOUNT_FINGERPRINT = "account_fingerprint"
CONF_CLIMATE_ENTITY_ID = "climate_entity_id"
CONF_ENABLED = "enabled"
CONF_FILTER_CHANGED_ENTITY_ID = "filter_changed_entity_id"
CONF_FILTER_CHANGED_DATE = "filter_changed_date"
CONF_FILTER_LIFETIME_RUNTIME_HOURS = "filter_lifetime_runtime_hours"
CONF_FILTER_MAX_AGE_DAYS = "filter_max_age_days"
CONF_FILTER_NOTICE_DAYS = "filter_notice_days"
CONF_ID = "id"
CONF_INCLUDE_AIR_QUALITY = "include_air_quality"
CONF_INCLUDE_CO2 = "include_co2"
CONF_INCLUDE_TEMPERATURE = "include_temperature"
CONF_INCLUDE_VOC = "include_voc"
CONF_OVERRIDE_NAME = "name"
CONF_MOTION_ENTITY_ID = "motion_entity_id"
CONF_OCCUPANCY_ENTITY_ID = "occupancy_entity_id"
CONF_POINT_LOOKBACK_DAYS = "point_lookback_days"
CONF_SCAN_INTERVAL_SECONDS = "scan_interval_seconds"
CONF_SENSORS = "sensors"
CONF_SLUG = "slug"
CONF_TEMPERATURE_ENTITY_ID = "temperature_entity_id"
CONF_THERMOSTAT_ID = "thermostat_id"
CONF_THERMOSTATS = "thermostats"

DEFAULT_POINT_LOOKBACK_DAYS = 45
DEFAULT_SUMMARY_OVERLAP_DAYS = 7
DEFAULT_SCAN_INTERVAL = timedelta(hours=6)
DEFAULT_SCAN_INTERVAL_SECONDS = int(DEFAULT_SCAN_INTERVAL.total_seconds())
DEFAULT_FILTER_LIFETIME_RUNTIME_HOURS = 250.0
DEFAULT_FILTER_MAX_AGE_DAYS = 90
DEFAULT_FILTER_NOTICE_DAYS = 7
FILTER_RECENT_RUNTIME_DAYS = 30
MAX_FILTER_LIFETIME_RUNTIME_HOURS = 10000
MAX_FILTER_MAX_AGE_DAYS = 730
MAX_FILTER_NOTICE_DAYS = 365
MAX_POINT_LOOKBACK_DAYS = 366
MAX_WINDOW_DAYS = 30
MIN_SCAN_INTERVAL_SECONDS = 300

SERVICE_IMPORT_STATISTICS = "import_statistics"
SERVICE_GET_CONFIGURATION = "get_configuration"
SERVICE_REBUILD_STATISTICS = "rebuild_statistics"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_END_DATE = "end_date"
ATTR_SKIP_SYNC = "skip_sync"
ATTR_START_DATE = "start_date"


def thermostat_entity_unique_id(thermostat_id: int, suffix: str) -> str:
    """Return a stable unique ID for a Beestat thermostat entity."""

    return f"thermostat_{thermostat_id}_{suffix}"


def sensor_entity_unique_id(sensor_id: int, suffix: str) -> str:
    """Return a stable unique ID for a Beestat room-sensor entity."""

    return f"sensor_{sensor_id}_{suffix}"


RUNTIME_FIELD_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("cool", "Cool Runtime", ("sum_compressor_cool_1", "sum_compressor_cool_2")),
    (
        "heat",
        "Heat Runtime",
        (
            "sum_compressor_heat_1",
            "sum_compressor_heat_2",
            "sum_auxiliary_heat_1",
            "sum_auxiliary_heat_2",
        ),
    ),
    ("fan", "Fan Runtime", ("sum_fan",)),
)


@dataclass(frozen=True, slots=True)
class SensorStatistic:
    """A Beestat runtime_sensor field imported as one external statistic."""

    sensor_id: int
    statistic_suffix: str
    name: str
    field: str
    unit: str
    unit_class: str | None


@dataclass(frozen=True, slots=True)
class SummaryMeanStatistic:
    """A runtime_thermostat_summary average field imported as one statistic."""

    statistic_suffix: str
    name: str
    field: str
    unit: str
    unit_class: str | None
    min_field: str | None = None
    max_field: str | None = None


@dataclass(frozen=True, slots=True)
class SummarySumStatistic:
    """A runtime_thermostat_summary summed field imported as one statistic."""

    statistic_suffix: str
    name: str
    field: str
    unit: str
    unit_class: str | None


@dataclass(frozen=True, slots=True)
class ThermostatPointStatistic:
    """A runtime_thermostat point-history field imported as one statistic."""

    statistic_suffix: str
    name: str
    field: str
    unit: str
    unit_class: str | None


SUMMARY_MEAN_STATISTICS: tuple[SummaryMeanStatistic, ...] = (
    SummaryMeanStatistic(
        "indoor_humidity",
        "Indoor Humidity",
        "avg_indoor_humidity",
        "%",
        STATISTIC_UNIT_CLASS_UNITLESS,
    ),
    SummaryMeanStatistic(
        "outdoor_temperature",
        "Outdoor Temperature",
        "avg_outdoor_temperature",
        UNIT_FAHRENHEIT,
        STATISTIC_UNIT_CLASS_TEMPERATURE,
        "min_outdoor_temperature",
        "max_outdoor_temperature",
    ),
    SummaryMeanStatistic(
        "outdoor_humidity",
        "Outdoor Humidity",
        "avg_outdoor_humidity",
        "%",
        STATISTIC_UNIT_CLASS_UNITLESS,
    ),
)


THERMOSTAT_POINT_STATISTICS: tuple[ThermostatPointStatistic, ...] = (
    ThermostatPointStatistic(
        "heat_setpoint",
        "Heat Setpoint",
        "setpoint_heat",
        UNIT_FAHRENHEIT,
        STATISTIC_UNIT_CLASS_TEMPERATURE,
    ),
    ThermostatPointStatistic(
        "cool_setpoint",
        "Cool Setpoint",
        "setpoint_cool",
        UNIT_FAHRENHEIT,
        STATISTIC_UNIT_CLASS_TEMPERATURE,
    ),
)


SUMMARY_SUM_STATISTICS: tuple[SummarySumStatistic, ...] = (
    SummarySumStatistic(
        "heating_degree_days",
        "Heating Degree Days",
        "sum_heating_degree_days",
        "degree days",
        None,
    ),
    SummarySumStatistic(
        "cooling_degree_days",
        "Cooling Degree Days",
        "sum_cooling_degree_days",
        "degree days",
        None,
    ),
)

import os
from dataclasses import dataclass


SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]


def required_env(name: str) -> str:
    value = os.getenv(name)

    if value is None or str(value).strip() == "":
        raise ValueError(f"Missing required environment variable: {name}")

    return str(value).strip()


def optional_env(name: str, default: str) -> str:
    value = os.getenv(name)

    if value is None or str(value).strip() == "":
        return default

    return str(value).strip()


def optional_int_env(name: str, default: int) -> int:
    value = optional_env(name, str(default))

    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be a number. Current value: {value}")


@dataclass(frozen=True)
class Config:
    spreadsheet_id: str
    service_account_json: str

    apps_config_sheet: str
    summary_sheet: str
    details_sheet: str
    user_session_sheet: str
    retention_details_sheet: str
    audience_segments_sheet: str

    start_date: str
    end_date: str
    timezone: str

    default_home_screen_name: str
    default_screen_field: str

    retention_days: int


def load_config() -> Config:
    return Config(
        spreadsheet_id=required_env("SPREADSHEET_ID"),
        service_account_json=required_env("GA4_SERVICE_ACCOUNT_JSON"),

        apps_config_sheet=optional_env("APPS_CONFIG_SHEET", "Apps Config"),
        summary_sheet=optional_env("SUMMARY_SHEET", "GA4 Funnel Summary"),
        details_sheet=optional_env("DETAILS_SHEET", "GA4 Funnel Details"),
        user_session_sheet=optional_env(
            "USER_SESSION_SHEET",
            "GA4 User Session Summary",
        ),
        retention_details_sheet=optional_env(
            "RETENTION_DETAILS_SHEET",
            "GA4 Retention Details",
        ),
        audience_segments_sheet=optional_env(
            "AUDIENCE_SEGMENTS_SHEET",
            "GA4 Audience Segments",
        ),

        start_date=optional_env("START_DATE", "28daysAgo"),
        end_date=optional_env("END_DATE", "yesterday"),
        timezone=optional_env("TIMEZONE", "Asia/Karachi"),

        default_home_screen_name=optional_env(
            "DEFAULT_HOME_SCREEN_NAME",
            "MainActivity",
        ),
        default_screen_field=optional_env(
            "DEFAULT_SCREEN_FIELD",
            "unifiedPagePathScreen",
        ),

        retention_days=optional_int_env("RETENTION_DAYS", 7),
    )

import os
from dataclasses import dataclass


SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/firebase.remoteconfig",
    "https://www.googleapis.com/auth/cloud-platform",
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


def optional_bool_env(name: str, default: bool) -> bool:
    value = optional_env(name, "true" if default else "false").lower().strip()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be true or false. Current value: {value}")


@dataclass(frozen=True)
class Config:
    spreadsheet_id: str
    service_account_json: str

    apps_config_sheet: str
    merged_sheet: str

    start_date: str
    end_date: str
    timezone: str

    default_home_screen_name: str
    default_screen_field: str
    retention_days: int

    notification_event_names: str
    key_event_names: str
    app_open_event_names: str
    home_event_names: str
    feature_event_names: str
    notification_parameter_keywords: str
    personalized_top_n: int
    remote_config_event_limit: int
    remote_config_app_version_limit: int

    time_capping_parameter: str
    iap_screen_parameter: str
    iap_screen_parameter_keywords: str
    remote_config_namespace: str
    firebase_remote_config_api_base: str
    firebase_remote_config_timeout: int
    fcm_data_api_base: str
    firebase_management_api_base: str
    fcm_data_page_size: int

    fetch_package_name: bool
    ga4_admin_api_base: str
    cleanup_old_tabs: bool


def load_config() -> Config:
    return Config(
        spreadsheet_id=required_env("SPREADSHEET_ID"),
        service_account_json=required_env("GA4_SERVICE_ACCOUNT_JSON"),

        apps_config_sheet=optional_env("APPS_CONFIG_SHEET", "Apps Config"),
        merged_sheet=optional_env("MERGED_SHEET", "GA4 Merged Data"),

        start_date=optional_env("START_DATE", "7daysAgo"),
        end_date=optional_env("END_DATE", "today"),
        timezone=optional_env("TIMEZONE", "Asia/Karachi"),

        default_home_screen_name=optional_env("DEFAULT_HOME_SCREEN_NAME", "MainActivity"),
        default_screen_field=optional_env("DEFAULT_SCREEN_FIELD", "unifiedPagePathScreen"),
        retention_days=optional_int_env("RETENTION_DAYS", 7),

        notification_event_names=optional_env(
            "NOTIFICATION_EVENT_NAMES",
            "notification_receive,notification_foreground,notification_open,notification_dismiss",
        ),
        key_event_names=optional_env(
            "KEY_EVENT_NAMES",
            "ad_impression,in_app_purchase,purchase,begin_checkout,subscribe,trial_start",
        ),
        app_open_event_names=optional_env(
            "APP_OPEN_EVENT_NAMES",
            "session_start,app_open,first_open",
        ),
        home_event_names=optional_env("HOME_EVENT_NAMES", ""),
        feature_event_names=optional_env(
            "FEATURE_EVENT_NAMES",
            optional_env("KEY_EVENT_NAMES", "ad_impression,in_app_purchase,purchase,begin_checkout,subscribe,trial_start"),
        ),
        notification_parameter_keywords=optional_env(
            "NOTIFICATION_PARAMETER_KEYWORDS",
            "notification,notifications,notify,notif,push,daily_notification,daily_notifications,daily_push,reminder,fcm,title,body,message,time",
        ),
        personalized_top_n=optional_int_env("PERSONALIZED_TOP_N", 5),
        remote_config_event_limit=optional_int_env("REMOTE_CONFIG_EVENT_LIMIT", 25),
        remote_config_app_version_limit=optional_int_env("REMOTE_CONFIG_APP_VERSION_LIMIT", 0),

        time_capping_parameter=optional_env("TIME_CAPPING_PARAMETER", "ad_time_capping"),
        iap_screen_parameter=optional_env("IAP_SCREEN_PARAMETER", "iap_screen"),
        iap_screen_parameter_keywords=optional_env(
            "IAP_SCREEN_PARAMETER_KEYWORDS",
            "iap_screen,iap_screen_variant,iap_paywall,iap,paywall,premium_screen,subscription_screen,subscribe_screen,purchase_screen,pro_screen,upgrade_screen,offers_screen,pricing_screen",
        ),
        remote_config_namespace=optional_env("REMOTE_CONFIG_NAMESPACE", "firebase"),
        firebase_remote_config_api_base=optional_env(
            "FIREBASE_REMOTE_CONFIG_API_BASE",
            "https://firebaseremoteconfig.googleapis.com/v1",
        ),
        firebase_remote_config_timeout=optional_int_env("FIREBASE_REMOTE_CONFIG_TIMEOUT", 30),
        fcm_data_api_base=optional_env(
            "FCM_DATA_API_BASE",
            "https://fcmdata.googleapis.com/v1beta1",
        ),
        firebase_management_api_base=optional_env(
            "FIREBASE_MANAGEMENT_API_BASE",
            "https://firebase.googleapis.com/v1beta1",
        ),
        fcm_data_page_size=optional_int_env("FCM_DATA_PAGE_SIZE", 1000),

        fetch_package_name=optional_bool_env("FETCH_PACKAGE_NAME", True),
        ga4_admin_api_base=optional_env(
            "GA4_ADMIN_API_BASE",
            "https://analyticsadmin.googleapis.com/v1beta",
        ),
        cleanup_old_tabs=optional_bool_env("CLEANUP_OLD_TABS", True),
    )

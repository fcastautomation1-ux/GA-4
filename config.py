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
    personalized_ux_sheet: str
    remote_config_sheet: str
    time_capping_ab_sheet: str
    daily_notifications_sheet: str
    ga4_notification_events_sheet: str
    fcm_delivery_sheet: str

    start_date: str
    end_date: str
    timezone: str

    default_home_screen_name: str
    default_screen_field: str

    retention_days: int
    personalized_top_n: int
    remote_config_event_limit: int
    remote_config_app_version_limit: int

    time_capping_parameter: str
    remote_config_namespace: str
    firebase_remote_config_api_base: str
    firebase_remote_config_timeout: int

    daily_notification_parameters: str
    notification_parameter_keywords: str
    notification_event_names: str
    notification_event_limit: int
    fcm_data_api_base: str
    firebase_management_api_base: str
    fcm_data_page_size: int


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
        personalized_ux_sheet=optional_env(
            "PERSONALIZED_UX_SHEET",
            "GA4 Personalized User Experience",
        ),
        remote_config_sheet=optional_env(
            "REMOTE_CONFIG_SHEET",
            "GA4 Remote Configuration",
        ),
        time_capping_ab_sheet=optional_env(
            "TIME_CAPPING_AB_SHEET",
            "Firebase AB Time Capping",
        ),
        daily_notifications_sheet=optional_env(
            "DAILY_NOTIFICATIONS_SHEET",
            "Firebase Daily Notifications",
        ),
        ga4_notification_events_sheet=optional_env(
            "GA4_NOTIFICATION_EVENTS_SHEET",
            "GA4 Notification Events",
        ),
        fcm_delivery_sheet=optional_env(
            "FCM_DELIVERY_SHEET",
            "Firebase Notification Delivery",
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
        personalized_top_n=optional_int_env("PERSONALIZED_TOP_N", 10),
        remote_config_event_limit=optional_int_env("REMOTE_CONFIG_EVENT_LIMIT", 25),
        remote_config_app_version_limit=optional_int_env(
            "REMOTE_CONFIG_APP_VERSION_LIMIT",
            10,
        ),

        time_capping_parameter=optional_env(
            "TIME_CAPPING_PARAMETER",
            "ad_time_capping",
        ),
        remote_config_namespace=optional_env(
            "REMOTE_CONFIG_NAMESPACE",
            "firebase",
        ),
        firebase_remote_config_api_base=optional_env(
            "FIREBASE_REMOTE_CONFIG_API_BASE",
            "https://firebaseremoteconfig.googleapis.com/v1",
        ),
        firebase_remote_config_timeout=optional_int_env(
            "FIREBASE_REMOTE_CONFIG_TIMEOUT",
            30,
        ),

        daily_notification_parameters=optional_env(
            "DAILY_NOTIFICATION_PARAMETERS",
            "",
        ),
        notification_parameter_keywords=optional_env(
            "NOTIFICATION_PARAMETER_KEYWORDS",
            "notification,notifications,notify,notif,push,daily_notification,daily_notifications,daily_push,reminder,fcm",
        ),
        notification_event_names=optional_env(
            "NOTIFICATION_EVENT_NAMES",
            "notification_receive,notification_foreground,notification_open,notification_dismiss",
        ),
        notification_event_limit=optional_int_env(
            "NOTIFICATION_EVENT_LIMIT",
            500,
        ),
        fcm_data_api_base=optional_env(
            "FCM_DATA_API_BASE",
            "https://fcmdata.googleapis.com/v1beta1",
        ),
        firebase_management_api_base=optional_env(
            "FIREBASE_MANAGEMENT_API_BASE",
            "https://firebase.googleapis.com/v1beta1",
        ),
        fcm_data_page_size=optional_int_env(
            "FCM_DATA_PAGE_SIZE",
            1000,
        ),
    )

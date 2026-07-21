from __future__ import annotations

import os
from dataclasses import dataclass


SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/firebase.readonly",
    "https://www.googleapis.com/auth/firebase.remoteconfig",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/bigquery",
]


def required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return str(value).strip()


def optional_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return default
    return str(value).strip()


def optional_int_env(name: str, default: int) -> int:
    value = optional_env(name, str(default))
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer. Current value: {value}") from exc


def optional_bool_env(name: str, default: bool) -> bool:
    value = optional_env(name, "true" if default else "false").lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be true or false. Current value: {value}")


@dataclass(frozen=True)
class Config:
    service_account_json: str

    bigquery_project_id: str
    bigquery_dataset_id: str
    bigquery_table_id: str
    bigquery_location: str
    bigquery_write_disposition: str

    start_date: str
    end_date: str
    timezone: str

    personalized_top_n: int
    default_home_screen_name: str
    default_screen_field: str
    screen_field_candidates: str
    home_screen_keywords: str
    app_open_event_names: str
    home_event_keywords: str

    time_capping_parameter_keywords: str
    iap_screen_parameter_keywords: str
    remote_parameter_limit: int

    remote_config_namespace: str
    firebase_remote_config_api_base: str
    firebase_management_api_base: str
    fcm_data_api_base: str
    fcm_data_page_size: int
    ga4_admin_api_base: str
    ga4_admin_audience_api_base: str

    request_timeout_seconds: int
    max_retries: int
    continue_on_error: bool


def load_config() -> Config:
    service_account_json = optional_env("GOOGLE_SERVICE_ACCOUNT_JSON") or optional_env(
        "GA4_SERVICE_ACCOUNT_JSON"
    )
    if not service_account_json:
        raise ValueError(
            "Missing credentials. Set GOOGLE_SERVICE_ACCOUNT_JSON or "
            "GA4_SERVICE_ACCOUNT_JSON to the JSON text or JSON file path."
        )

    write_disposition = optional_env(
        "BIGQUERY_WRITE_DISPOSITION", "WRITE_TRUNCATE"
    ).upper()
    if write_disposition not in {"WRITE_TRUNCATE", "WRITE_APPEND"}:
        raise ValueError(
            "BIGQUERY_WRITE_DISPOSITION must be WRITE_TRUNCATE or WRITE_APPEND"
        )

    return Config(
        service_account_json=service_account_json,
        bigquery_project_id=optional_env("BIGQUERY_PROJECT_ID"),
        bigquery_dataset_id=optional_env("BIGQUERY_DATASET_ID", "ga4_firebase"),
        bigquery_table_id=optional_env("BIGQUERY_TABLE_ID", "all_apps_analytics"),
        bigquery_location=optional_env("BIGQUERY_LOCATION", "US"),
        bigquery_write_disposition=write_disposition,
        start_date=optional_env("START_DATE", "7daysAgo"),
        end_date=optional_env("END_DATE", "yesterday"),
        timezone=optional_env("TIMEZONE", "Asia/Karachi"),
        personalized_top_n=max(optional_int_env("PERSONALIZED_TOP_N", 5), 1),
        default_home_screen_name=optional_env(
            "DEFAULT_HOME_SCREEN_NAME", "MainActivity"
        ),
        default_screen_field=optional_env(
            "DEFAULT_SCREEN_FIELD", "unifiedPagePathScreen"
        ),
        screen_field_candidates=optional_env(
            "SCREEN_FIELD_CANDIDATES",
            "unifiedPagePathScreen,unifiedScreenName,unifiedScreenClass,screenName,screenClass",
        ),
        home_screen_keywords=optional_env(
            "HOME_SCREEN_KEYWORDS", "home,main,dashboard,landing,start"
        ),
        app_open_event_names=optional_env(
            "APP_OPEN_EVENT_NAMES", "session_start,app_open,first_open"
        ),
        home_event_keywords=optional_env(
            "HOME_EVENT_KEYWORDS", "home,main,dashboard,landing"
        ),
        time_capping_parameter_keywords=optional_env(
            "TIME_CAPPING_PARAMETER_KEYWORDS",
            "ad_time_capping,time_capping,ad_capping,interstitial_time_capping,app_open_time_capping,capping",
        ),
       iap_screen_parameter_keywords=optional_env(
            "IAP_SCREEN_PARAMETER_KEYWORDS",
             (
        "iap_screen,"
        "iap_screen_variant,"
        "iap_paywall,"
        "iap,"
        "paywall,"
        "premium_visibility,"
        "premium,"
        "premium_screen,"
        "premium_dialog,"
        "premium_popup,"
        "premium_modal,"
        "premium_offer,"
        "premium_plan,"
        "subscription,"
        "subscription_screen,"
        "subscription_dialog,"
        "subscription_popup,"
        "subscribe,"
        "subscribe_screen,"
        "purchase,"
        "purchase_screen,"
        "purchase_flow,"
        "purchase_dialog,"
        "checkout,"
        "billing,"
        "billing_screen,"
        "pricing,"
        "pricing_screen,"
        "price_screen,"
        "plans,"
        "plan_screen,"
        "offers,"
        "offers_screen,"
        "upgrade,"
        "upgrade_screen,"
        "pro_screen,"
        "trial,"
        "trial_screen,"
        "free_trial,"
        "membership,"
        "entitlement,"
        "show_premium,"
        "show_paywall,"
        "enable_premium,"
        "enable_paywall"
    ),
),
        ),
        remote_parameter_limit=max(optional_int_env("REMOTE_PARAMETER_LIMIT", 20), 1),
        remote_config_namespace=optional_env("REMOTE_CONFIG_NAMESPACE", "firebase"),
        firebase_remote_config_api_base=optional_env(
            "FIREBASE_REMOTE_CONFIG_API_BASE",
            "https://firebaseremoteconfig.googleapis.com/v1",
        ).rstrip("/"),
        firebase_management_api_base=optional_env(
            "FIREBASE_MANAGEMENT_API_BASE",
            "https://firebase.googleapis.com/v1beta1",
        ).rstrip("/"),
        fcm_data_api_base=optional_env(
            "FCM_DATA_API_BASE", "https://fcmdata.googleapis.com/v1beta1"
        ).rstrip("/"),
        fcm_data_page_size=max(optional_int_env("FCM_DATA_PAGE_SIZE", 1000), 1),
        ga4_admin_api_base=optional_env(
            "GA4_ADMIN_API_BASE", "https://analyticsadmin.googleapis.com/v1beta"
        ).rstrip("/"),
        ga4_admin_audience_api_base=optional_env(
            "GA4_ADMIN_AUDIENCE_API_BASE",
            "https://analyticsadmin.googleapis.com/v1alpha",
        ).rstrip("/"),
        request_timeout_seconds=max(optional_int_env("REQUEST_TIMEOUT_SECONDS", 45), 1),
        max_retries=max(optional_int_env("MAX_RETRIES", 5), 0),
        continue_on_error=optional_bool_env("CONTINUE_ON_ERROR", True),
    )

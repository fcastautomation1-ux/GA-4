from __future__ import annotations

import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.api_core.exceptions import NotFound
from google.analytics.data_v1beta.types import (
    Cohort,
    CohortSpec,
    CohortsRange,
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
    FilterExpressionList,
    Metric,
    OrderBy,
    RunReportRequest,
)
from google.auth.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery
from google.oauth2 import service_account
from requests import Response

from config import Config, SCOPES, load_config


LOGGER = logging.getLogger("ga4-firebase-bigquery")

# BigQuery does not allow '/' in a column name, so the four original slash
# columns use underscores. Every other requested output column is retained.
PERSONALIZED_COLUMN_SPECS = [
    ("Country", "country"),
    ("Language", "language"),
    ("Device Category", "device_category"),
    ("Operating System", "operating_system"),
    ("App Version", "app_version"),
    ("First User Medium", "first_user_medium"),
    ("Top Screens / Screen Class", "screen_class"),
]

FCM_COLUMNS = [
    "firebase_notifications_accepted",
    "firebase_delivered",
    "firebase_pending",
]


def build_output_headers() -> list[str]:
    headers = ["Package_Name", "Date"]
    headers.extend(FCM_COLUMNS)
    headers.extend(["Audience_Name", "Events_Name", "Countries", "Total_Users"])
    headers.extend(
        [
            "funnel_app_open_users",
            "funnel_app_open_events",
            "funnel_home_users",
            "funnel_home_events_views",
            "funnel_possible_drop_off",
            "funnel_home_reach_rate",
            "funnel_ad_impression_events",
            "funnel_ad_impression_users",
            "funnel_in_app_purchase_events",
            "funnel_in_app_purchase_users",
        ]
    )
    headers.extend(
        [
            "time_capping_name",
            "time_capping_condition",
            "time_capping_value",
            "time_capping_fetch_percent",
            "time_capping_last_published",
            "iap_screen_name",
            "iap_screen_condition",
            "iap_screen_value",
            "iap_screen_fetch_percent",
            "iap_screen_last_published",
            "time_analysis_country",
            "time_analysis_active_users",
            "time_analysis_new_users",
            "time_analysis_sessions",
            "time_analysis_engaged_sessions",
            "time_analysis_engagement_rate",
            "time_analysis_avg_session_duration",
            "time_analysis_sessions_per_active_user",
            "time_analysis_total_engagement_time",
            "retention_cohort_date",
            "retention_country",
            "retention_d1_first_session_retention",
            "retention_d3_first_session_retention",
            "retention_d7_first_session_retention",
            "retention_d30_first_session_retention",
        ]
    )
    for _, slug in PERSONALIZED_COLUMN_SPECS:
        headers.extend(
            [
                f"personalized_category_{slug}",
                f"personalized_{slug}_users",
                f"personalized_{slug}_sessions",
                f"personalized_{slug}_er",
                f"personalized_{slug}_avg",
            ]
        )
    if len(headers) != len(set(headers)):
        duplicates = sorted({name for name in headers if headers.count(name) > 1})
        raise ValueError(f"Duplicate output headers found: {duplicates}")
    return headers


OUTPUT_HEADERS = build_output_headers()

INTEGER_COLUMNS = {
    "firebase_notifications_accepted",
    "Total_Users",
    "funnel_app_open_users",
    "funnel_app_open_events",
    "funnel_home_users",
    "funnel_home_events_views",
    "funnel_possible_drop_off",
    "funnel_ad_impression_events",
    "funnel_ad_impression_users",
    "funnel_in_app_purchase_events",
    "funnel_in_app_purchase_users",
    "time_analysis_active_users",
    "time_analysis_new_users",
    "time_analysis_sessions",
    "time_analysis_engaged_sessions",
}
for _, _slug in PERSONALIZED_COLUMN_SPECS:
    INTEGER_COLUMNS.add(f"personalized_{_slug}_users")
    INTEGER_COLUMNS.add(f"personalized_{_slug}_sessions")

FLOAT_COLUMNS = {"time_analysis_sessions_per_active_user"}


def build_bigquery_schema() -> list[bigquery.SchemaField]:
    schema: list[bigquery.SchemaField] = []
    for name in OUTPUT_HEADERS:
        if name == "Date":
            field_type = "DATE"
        elif name in INTEGER_COLUMNS:
            field_type = "INT64"
        elif name in FLOAT_COLUMNS:
            field_type = "FLOAT64"
        else:
            field_type = "STRING"
        schema.append(bigquery.SchemaField(name, field_type, mode="NULLABLE"))
    return schema


BIGQUERY_SCHEMA = build_bigquery_schema()

SYSTEM_EVENTS = {
    "ad_activeview",
    "ad_click",
    "ad_exposure",
    "ad_impression",
    "ad_query",
    "adunit_exposure",
    "app_clear_data",
    "app_exception",
    "app_install",
    "app_open",
    "app_remove",
    "app_store_refund",
    "app_store_subscription_cancel",
    "app_store_subscription_convert",
    "app_store_subscription_renew",
    "app_update",
    "click",
    "error",
    "file_download",
    "firebase_campaign",
    "firebase_in_app_message_action",
    "firebase_in_app_message_dismiss",
    "firebase_in_app_message_impression",
    "first_open",
    "first_visit",
    "form_start",
    "form_submit",
    "in_app_purchase",
    "notification_dismiss",
    "notification_foreground",
    "notification_open",
    "notification_receive",
    "os_update",
    "page_view",
    "screen_view",
    "scroll",
    "session_start",
    "user_engagement",
    "video_complete",
    "video_progress",
    "video_start",
    "view_search_results",
}

EXCLUDED_GA4_EVENT_NAMES = {
    "notification_receive",
    "notification_foreground",
    "notification_open",
    "notification_dismiss",
    "dn_rc_inter_clicked",
    "dn_rc_inter_displayed",
    "dn_rc_inter_loaded",
    "dn_rc_inter_requested",
    "dn_rc_inter_dismissed",
}

RETENTION_DAY_OFFSETS = (1, 3, 7, 30)


@dataclass(frozen=True)
class FirebaseProject:
    project_id: str
    display_name: str


@dataclass(frozen=True)
class FirebaseApp:
    app_id: str
    project_id: str
    project_name: str
    display_name: str
    platform: str
    namespace: str


@dataclass
class AppTarget:
    app_name: str
    property_id: str
    ga4_stream_id: str
    package_name: str
    firebase_project_id: str = ""
    firebase_project_name: str = ""
    firebase_app_id: str = ""
    home_screen_name: str = ""
    screen_field: str = ""
    app_open_event_names: str = ""
    home_event_names: str = ""


def split_csv(value: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in str(value or "").split(","):
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def to_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def to_number(value: Any) -> int | float:
    number = to_float(value)
    return int(number) if number.is_integer() else round(number, 2)


def percent(value: Any) -> str:
    try:
        number = float(value)
        if abs(number) <= 1:
            number *= 100
        return f"{round(number, 2)}%"
    except (TypeError, ValueError):
        return ""


def rate(numerator: Any, denominator: Any) -> str:
    denominator_value = to_float(denominator)
    if denominator_value == 0:
        return "0%"
    return f"{round((to_float(numerator) / denominator_value) * 100, 2)}%"


def format_seconds(seconds_value: Any) -> str:
    total = max(int(round(to_float(seconds_value))), 0)
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def ga4_date_to_iso(value: str) -> str:
    value = str(value or "").strip()
    if re.fullmatch(r"\d{8}", value):
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    return value


def resolve_ga4_date(value: str, timezone: str) -> str:
    value = str(value).strip()
    today = datetime.now(ZoneInfo(timezone)).date()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    if value.lower() == "today":
        return today.isoformat()
    if value.lower() == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    match = re.fullmatch(r"(\d+)daysAgo", value, flags=re.IGNORECASE)
    if match:
        return (today - timedelta(days=int(match.group(1)))).isoformat()
    raise ValueError(
        f"Unsupported GA4 date '{value}'. Use YYYY-MM-DD, today, yesterday, or NdaysAgo."
    )


def get_report_dates(config: Config) -> list[str]:
    start = datetime.fromisoformat(
        resolve_ga4_date(config.start_date, config.timezone)
    ).date()
    end = datetime.fromisoformat(
        resolve_ga4_date(config.end_date, config.timezone)
    ).date()
    if start > end:
        raise ValueError(
            f"START_DATE must be before END_DATE. Current: {start} to {end}"
        )
    return [
        (start + timedelta(days=index)).isoformat()
        for index in range((end - start).days + 1)
    ]


def load_credentials(raw_or_path: str) -> Credentials:
    raw_or_path = str(raw_or_path).strip()
    if raw_or_path.startswith("{"):
        info = json.loads(raw_or_path)
        return service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
    path = Path(raw_or_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            "Service-account JSON must be JSON text or an existing JSON file path: "
            f"{path}"
        )
    return service_account.Credentials.from_service_account_file(
        str(path), scopes=SCOPES
    )


def credentials_project_id(credentials: Credentials, raw_or_path: str) -> str:
    project_id = str(getattr(credentials, "project_id", "") or "").strip()
    if project_id:
        return project_id
    try:
        if raw_or_path.strip().startswith("{"):
            return str(json.loads(raw_or_path).get("project_id", "")).strip()
        return str(
            json.loads(Path(raw_or_path).read_text(encoding="utf-8")).get(
                "project_id", ""
            )
        ).strip()
    except Exception:
        return ""


class GoogleRestClient:
    def __init__(self, credentials: Credentials, config: Config) -> None:
        self.session = AuthorizedSession(credentials)
        self.timeout = config.request_timeout_seconds
        self.max_retries = config.max_retries

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        expected_statuses: Sequence[int] = (200,),
    ) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            response: Response = self.session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
            if response.status_code in expected_statuses:
                if not response.content:
                    return {}
                return response.json()

            retryable = response.status_code in {408, 429, 500, 502, 503, 504}
            if retryable and attempt < self.max_retries:
                retry_after = response.headers.get("Retry-After", "")
                sleep_seconds = (
                    float(retry_after)
                    if retry_after.replace(".", "", 1).isdigit()
                    else min(2**attempt, 30)
                )
                time.sleep(sleep_seconds)
                continue

            raise RuntimeError(
                f"{method} {url} failed with HTTP {response.status_code}: "
                f"{response.text[:2500]}"
            )
        raise RuntimeError(f"{method} {url} failed after retries")

    def paginated_get(
        self,
        url: str,
        *,
        item_key: str,
        params: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        page_token = ""
        base_params = dict(params or {})
        while True:
            request_params = dict(base_params)
            if page_token:
                request_params["pageToken"] = page_token
            payload = self.request_json("GET", url, params=request_params)
            for item in payload.get(item_key, []) or []:
                if isinstance(item, dict):
                    yield item
            page_token = str(payload.get("nextPageToken", "") or "").strip()
            if not page_token:
                return


class Pipeline:
    def __init__(self, config: Config, credentials: Credentials) -> None:
        self.config = config
        self.credentials = credentials
        self.rest = GoogleRestClient(credentials, config)
        self.analytics = BetaAnalyticsDataClient(credentials=credentials)
        self.metadata_dimensions_cache: dict[str, set[str]] = {}
        self.remote_config_cache: dict[str, dict[str, Any]] = {}
        self.audience_definition_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self.fcm_delivery_cache: dict[tuple[str, str], dict[str, Any]] = {}

    # -------------------------
    # Account/app discovery
    # -------------------------
    def list_ga4_properties(self) -> list[dict[str, str]]:
        properties: list[dict[str, str]] = []
        url = f"{self.config.ga4_admin_api_base}/accountSummaries"
        for account in self.rest.paginated_get(
            url,
            item_key="accountSummaries",
            params={"pageSize": 200},
        ):
            account_name = str(account.get("displayName", "") or "").strip()
            for item in account.get("propertySummaries", []) or []:
                resource = str(item.get("property", "") or "").strip()
                property_id = resource.rsplit("/", 1)[-1]
                if property_id:
                    properties.append(
                        {
                            "property_id": property_id,
                            "property_name": str(
                                item.get("displayName", "") or ""
                            ).strip(),
                            "account_name": account_name,
                        }
                    )
        deduplicated = {item["property_id"]: item for item in properties}
        return sorted(
            deduplicated.values(),
            key=lambda item: (item["property_name"].lower(), item["property_id"]),
        )

    def list_ga4_android_streams(
        self, property_item: Mapping[str, str]
    ) -> list[dict[str, str]]:
        property_id = property_item["property_id"]
        url = f"{self.config.ga4_admin_api_base}/properties/{property_id}/dataStreams"
        streams: list[dict[str, str]] = []
        for item in self.rest.paginated_get(
            url,
            item_key="dataStreams",
            params={"pageSize": 200},
        ):
            android = item.get("androidAppStreamData", {}) or {}
            if not android:
                continue
            resource_name = str(item.get("name", "") or "").strip()
            stream_id = resource_name.rsplit("/", 1)[-1]
            package_name = str(android.get("packageName", "") or "").strip()
            if not stream_id or not package_name:
                continue
            streams.append(
                {
                    "property_id": property_id,
                    "property_name": str(
                        property_item.get("property_name", "") or ""
                    ).strip(),
                    "stream_id": stream_id,
                    "stream_name": str(item.get("displayName", "") or "").strip(),
                    "firebase_app_id": str(
                        android.get("firebaseAppId", "") or ""
                    ).strip(),
                    "package_name": package_name,
                }
            )
        return streams

    def list_firebase_projects(self) -> list[FirebaseProject]:
        projects: list[FirebaseProject] = []
        url = f"{self.config.firebase_management_api_base}/projects"
        for item in self.rest.paginated_get(
            url,
            item_key="results",
            params={"pageSize": 100},
        ):
            project_id = str(item.get("projectId", "") or "").strip()
            if project_id:
                projects.append(
                    FirebaseProject(
                        project_id=project_id,
                        display_name=str(item.get("displayName", "") or "").strip(),
                    )
                )
        return projects

    def list_firebase_apps(
        self, projects: Iterable[FirebaseProject]
    ) -> list[FirebaseApp]:
        apps: list[FirebaseApp] = []

        for project in projects:
            url = (
                f"{self.config.firebase_management_api_base}/projects/"
                f"{project.project_id}/androidApps"
            )

            LOGGER.info(
                "Checking Firebase Android apps in project: %s (%s)",
                project.project_id,
                project.display_name,
            )

            try:
                items = list(
                    self.rest.paginated_get(
                        url,
                        item_key="apps",
                        params={"pageSize": 100},
                    )
                )
            except Exception as exc:
                LOGGER.warning(
                    "Firebase Android apps unavailable for %s: %s",
                    project.project_id,
                    exc,
                )
                continue

            LOGGER.info(
                "Firebase project %s returned %d Android apps",
                project.project_id,
                len(items),
            )

            for item in items:
                app_id = str(item.get("appId", "") or "").strip()
                package_name = str(item.get("packageName", "") or "").strip()

                if not app_id:
                    continue

                apps.append(
                    FirebaseApp(
                        app_id=app_id,
                        project_id=project.project_id,
                        project_name=project.display_name,
                        display_name=str(
                            item.get("displayName", "") or ""
                        ).strip(),
                        platform="ANDROID",
                        namespace=package_name,
                    )
                )

        return apps

    def discover_apps(self) -> list[AppTarget]:
        LOGGER.info("Discovering every GA4 property accessible to the service account")
        properties = self.list_ga4_properties()
        LOGGER.info("Accessible GA4 properties: %d", len(properties))

        streams: list[dict[str, str]] = []
        for property_item in properties:
            try:
                streams.extend(self.list_ga4_android_streams(property_item))
            except Exception as exc:
                LOGGER.warning(
                    "GA4 data streams unavailable for property %s: %s",
                    property_item["property_id"],
                    exc,
                )
                if not self.config.continue_on_error:
                    raise

        LOGGER.info("Accessible Android GA4 data streams: %d", len(streams))
        if not streams:
            raise RuntimeError("No accessible Android GA4 data streams were found.")

        firebase_projects: list[FirebaseProject] = []
        firebase_apps: list[FirebaseApp] = []
        try:
            firebase_projects = self.list_firebase_projects()
            firebase_apps = self.list_firebase_apps(firebase_projects)
            LOGGER.info(
                "Accessible Firebase projects/apps: %d/%d",
                len(firebase_projects),
                len(firebase_apps),
            )
        except Exception as exc:
            LOGGER.warning(
                "Firebase discovery failed; GA4 data will still be processed: %s", exc
            )
            if not self.config.continue_on_error:
                raise

        by_app_id = {app.app_id: app for app in firebase_apps}
        by_package: dict[str, list[FirebaseApp]] = {}
        for app in firebase_apps:
            if app.namespace:
                by_package.setdefault(app.namespace.lower(), []).append(app)

        discovered: list[AppTarget] = []
        for stream in streams:
            firebase_app = by_app_id.get(stream["firebase_app_id"])
            if firebase_app is None:
                package_matches = by_package.get(stream["package_name"].lower(), [])
                if len(package_matches) == 1:
                    firebase_app = package_matches[0]

            app_name = (
                (firebase_app.display_name if firebase_app else "")
                or stream["stream_name"]
                or stream["property_name"]
                or stream["package_name"]
            )
            discovered.append(
                AppTarget(
                    app_name=app_name,
                    property_id=stream["property_id"],
                    ga4_stream_id=stream["stream_id"],
                    package_name=stream["package_name"],
                    firebase_project_id=firebase_app.project_id if firebase_app else "",
                    firebase_project_name=firebase_app.project_name
                    if firebase_app
                    else "",
                    firebase_app_id=(
                        firebase_app.app_id
                        if firebase_app
                        else stream["firebase_app_id"]
                    ),
                    home_screen_name=self.config.default_home_screen_name,
                    screen_field=self.config.default_screen_field,
                    app_open_event_names=self.config.app_open_event_names,
                    home_event_names="",
                )
            )

        unique: dict[tuple[str, str, str], AppTarget] = {}
        for app in discovered:
            unique[(app.property_id, app.ga4_stream_id, app.package_name)] = app
        return sorted(
            unique.values(),
            key=lambda app: (app.app_name.lower(), app.property_id, app.ga4_stream_id),
        )

    # -------------------------
    # GA4 helpers/config inference
    # -------------------------
    @staticmethod
    def exact_filter(field_name: str, value: str) -> FilterExpression:
        return FilterExpression(
            filter=Filter(
                field_name=field_name,
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.EXACT,
                    value=value,
                    case_sensitive=False,
                ),
            )
        )

    @staticmethod
    def contains_filter(field_name: str, value: str) -> FilterExpression:
        return FilterExpression(
            filter=Filter(
                field_name=field_name,
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.CONTAINS,
                    value=value,
                    case_sensitive=False,
                ),
            )
        )

    @staticmethod
    def in_list_filter(field_name: str, values: list[str]) -> FilterExpression:
        return FilterExpression(
            filter=Filter(
                field_name=field_name,
                in_list_filter=Filter.InListFilter(values=values, case_sensitive=False),
            )
        )

    @staticmethod
    def and_filter(expressions: list[FilterExpression]) -> FilterExpression:
        return FilterExpression(and_group=FilterExpressionList(expressions=expressions))

    def report_filter(
        self,
        app: AppTarget,
        base_filter: FilterExpression | None = None,
    ) -> dict[str, FilterExpression]:
        expressions = [self.exact_filter("streamId", app.ga4_stream_id)]
        if base_filter is not None:
            expressions.append(base_filter)
        expression = (
            expressions[0] if len(expressions) == 1 else self.and_filter(expressions)
        )
        return {"dimension_filter": expression}

    @staticmethod
    def date_order() -> OrderBy:
        return OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))

    @staticmethod
    def metric_order(metric_name: str, desc: bool = True) -> OrderBy:
        return OrderBy(
            metric=OrderBy.MetricOrderBy(metric_name=metric_name),
            desc=desc,
        )

    @staticmethod
    def parse_response_rows(response: Any) -> list[dict[str, str]]:
        dimension_headers = [header.name for header in response.dimension_headers]
        metric_headers = [header.name for header in response.metric_headers]
        rows: list[dict[str, str]] = []
        for response_row in response.rows:
            item: dict[str, str] = {}
            for index, value in enumerate(response_row.dimension_values):
                if index < len(dimension_headers):
                    item[dimension_headers[index]] = value.value
            for index, value in enumerate(response_row.metric_values):
                if index < len(metric_headers):
                    item[metric_headers[index]] = value.value
            rows.append(item)
        return rows

    def get_metadata_dimensions(self, property_id: str) -> set[str]:
        if property_id in self.metadata_dimensions_cache:
            return self.metadata_dimensions_cache[property_id]
        url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}/metadata"
        payload = self.rest.request_json("GET", url)
        dimensions = {
            str(item.get("apiName", "") or "").strip()
            for item in payload.get("dimensions", []) or []
            if item.get("apiName")
        }
        self.metadata_dimensions_cache[property_id] = dimensions
        return dimensions

    def infer_ga4_app_configuration(self, app: AppTarget) -> None:
        try:
            available_dimensions = self.get_metadata_dimensions(app.property_id)
            for candidate in split_csv(self.config.screen_field_candidates):
                if candidate in available_dimensions:
                    app.screen_field = candidate
                    break

            event_request = RunReportRequest(
                property=f"properties/{app.property_id}",
                date_ranges=[
                    DateRange(
                        start_date=self.config.start_date,
                        end_date=self.config.end_date,
                    )
                ],
                dimensions=[Dimension(name="eventName")],
                metrics=[Metric(name="eventCount")],
                order_bys=[self.metric_order("eventCount")],
                **self.report_filter(app),
                limit=100000,
            )
            event_rows = self.parse_response_rows(
                self.analytics.run_report(event_request)
            )
            observed_names = [
                str(row.get("eventName", "") or "").strip()
                for row in event_rows
                if str(row.get("eventName", "") or "").strip()
            ]
            observed_set = set(observed_names)

            app_open_names = [
                name
                for name in split_csv(self.config.app_open_event_names)
                if name in observed_set
            ]
            if app_open_names:
                app.app_open_event_names = ",".join(app_open_names)

            home_keywords = [
                normalize(item) for item in split_csv(self.config.home_event_keywords)
            ]
            custom_home_events = [
                name
                for name in observed_names
                if name not in SYSTEM_EVENTS
                and any(keyword in normalize(name) for keyword in home_keywords)
            ]
            app.home_event_names = ",".join(dict.fromkeys(custom_home_events))

            screen_filter = self.and_filter(
                [
                    self.exact_filter("streamId", app.ga4_stream_id),
                    self.exact_filter("eventName", "screen_view"),
                ]
            )
            screen_request = RunReportRequest(
                property=f"properties/{app.property_id}",
                date_ranges=[
                    DateRange(
                        start_date=self.config.start_date,
                        end_date=self.config.end_date,
                    )
                ],
                dimensions=[Dimension(name=app.screen_field)],
                metrics=[Metric(name="eventCount")],
                dimension_filter=screen_filter,
                order_bys=[self.metric_order("eventCount")],
                keep_empty_rows=False,
                limit=100000,
            )
            screen_rows = self.parse_response_rows(
                self.analytics.run_report(screen_request)
            )
            candidates: list[tuple[str, int]] = []
            for row in screen_rows:
                screen_name = str(row.get(app.screen_field, "") or "").strip()
                if not screen_name or screen_name.lower() in {
                    "(not set)",
                    "not set",
                    "unknown",
                }:
                    continue
                candidates.append(
                    (screen_name, int(to_float(row.get("eventCount", 0))))
                )

            if candidates:
                screen_keywords = [
                    normalize(item)
                    for item in split_csv(self.config.home_screen_keywords)
                ]

                def screen_score(item: tuple[str, int]) -> tuple[int, int]:
                    screen_name, count = item
                    normalized = normalize(screen_name)
                    keyword_score = sum(
                        1 for keyword in screen_keywords if keyword in normalized
                    )
                    return keyword_score, count

                app.home_screen_name = max(candidates, key=screen_score)[0]
        except Exception as exc:
            LOGGER.warning(
                "Could not fully infer GA4 config for %s (%s/%s): %s",
                app.package_name,
                app.property_id,
                app.ga4_stream_id,
                exc,
            )
            if not self.config.continue_on_error:
                raise

    # -------------------------
    # GA4 reports
    # -------------------------
    def run_time_analysis_report(
        self, app: AppTarget
    ) -> dict[str, list[dict[str, Any]]]:
        by_date: dict[str, list[dict[str, Any]]] = {}
        page_size = 100000
        offset = 0
        while True:
            request = RunReportRequest(
                property=f"properties/{app.property_id}",
                date_ranges=[
                    DateRange(
                        start_date=self.config.start_date,
                        end_date=self.config.end_date,
                    )
                ],
                dimensions=[Dimension(name="date"), Dimension(name="country")],
                metrics=[
                    Metric(name="activeUsers"),
                    Metric(name="newUsers"),
                    Metric(name="sessions"),
                    Metric(name="engagedSessions"),
                    Metric(name="averageSessionDuration"),
                    Metric(name="userEngagementDuration"),
                    Metric(name="engagementRate"),
                ],
                order_bys=[self.date_order(), self.metric_order("activeUsers")],
                **self.report_filter(app),
                keep_empty_rows=False,
                limit=page_size,
                offset=offset,
            )
            response = self.analytics.run_report(request)
            page_rows = self.parse_response_rows(response)
            if not page_rows:
                break
            for row in page_rows:
                report_date = ga4_date_to_iso(row.get("date", ""))
                active_users = to_number(row.get("activeUsers", 0))
                sessions = to_number(row.get("sessions", 0))
                by_date.setdefault(report_date, []).append(
                    {
                        "Country": row.get("country", "") or "(not set)",
                        "Active Users": active_users,
                        "New Users": to_number(row.get("newUsers", 0)),
                        "Sessions": sessions,
                        "Engaged Sessions": to_number(row.get("engagedSessions", 0)),
                        "Engagement Rate": percent(row.get("engagementRate", 0)),
                        "Avg Session Duration": format_seconds(
                            row.get("averageSessionDuration", 0)
                        ),
                        "Sessions Per Active User": (
                            round(to_float(sessions) / to_float(active_users), 2)
                            if to_float(active_users)
                            else 0
                        ),
                        "Total Engagement Time": format_seconds(
                            row.get("userEngagementDuration", 0)
                        ),
                    }
                )
            offset += len(page_rows)
            total_rows = int(getattr(response, "row_count", 0) or 0)
            if len(page_rows) < page_size or (total_rows and offset >= total_rows):
                break
        return by_date

    def run_event_report(
        self,
        app: AppTarget,
        event_names: list[str],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        if not event_names:
            return {}
        request = RunReportRequest(
            property=f"properties/{app.property_id}",
            date_ranges=[
                DateRange(
                    start_date=self.config.start_date,
                    end_date=self.config.end_date,
                )
            ],
            dimensions=[Dimension(name="date"), Dimension(name="eventName")],
            metrics=[Metric(name="activeUsers"), Metric(name="eventCount")],
            **self.report_filter(app, self.in_list_filter("eventName", event_names)),
            order_bys=[self.date_order()],
            keep_empty_rows=False,
            limit=100000,
        )
        response = self.analytics.run_report(request)
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self.parse_response_rows(response):
            report_date = ga4_date_to_iso(row.get("date", ""))
            event_name = str(row.get("eventName", "") or "").strip()
            result[(report_date, event_name)] = {
                "active_users": to_number(row.get("activeUsers", 0)),
                "event_count": to_number(row.get("eventCount", 0)),
            }
        return result

    def run_home_screen_report(self, app: AppTarget) -> dict[str, dict[str, Any]]:
        request = RunReportRequest(
            property=f"properties/{app.property_id}",
            date_ranges=[
                DateRange(
                    start_date=self.config.start_date,
                    end_date=self.config.end_date,
                )
            ],
            dimensions=[Dimension(name="date")],
            metrics=[Metric(name="activeUsers"), Metric(name="eventCount")],
            **self.report_filter(
                app,
                self.and_filter(
                    [
                        self.exact_filter("eventName", "screen_view"),
                        self.contains_filter(app.screen_field, app.home_screen_name),
                    ]
                ),
            ),
            order_bys=[self.date_order()],
            keep_empty_rows=True,
            limit=100000,
        )
        response = self.analytics.run_report(request)
        result: dict[str, dict[str, Any]] = {}
        for row in self.parse_response_rows(response):
            report_date = ga4_date_to_iso(row.get("date", ""))
            result[report_date] = {
                "active_users": to_number(row.get("activeUsers", 0)),
                "event_count": to_number(row.get("eventCount", 0)),
            }
        return result

    @staticmethod
    def parse_cohort_day(value: Any) -> int:
        digits = re.sub(r"[^0-9]", "", str(value or "0"))
        return int(digits or 0)

    @staticmethod
    def retention_ready(
        cohort_date: str,
        day_offset: int,
        observation_end_date: str,
    ) -> bool:
        cohort_day = datetime.fromisoformat(cohort_date).date()
        target_day = cohort_day + timedelta(days=day_offset)
        report_end_day = datetime.fromisoformat(observation_end_date).date()
        return target_day <= report_end_day

    def run_retention_report(
        self,
        app: AppTarget,
        report_dates: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        retention_by_date = {report_date: [] for report_date in report_dates}
        if not report_dates:
            return retention_by_date
        observation_end_date = max(report_dates)

        # One cohort per request is slower but maps every result exactly to the
        # requested cohort date and avoids cross-cohort ambiguity.
        for cohort_date in report_dates:
            country_data: dict[str, dict[str, Any]] = {}
            page_size = 100000
            offset = 0
            while True:
                request = RunReportRequest(
                    property=f"properties/{app.property_id}",
                    dimensions=[
                        Dimension(name="cohort"),
                        Dimension(name="cohortNthDay"),
                        Dimension(name="country"),
                    ],
                    metrics=[
                        Metric(name="cohortActiveUsers"),
                        Metric(name="cohortTotalUsers"),
                    ],
                    cohort_spec=CohortSpec(
                        cohorts=[
                            Cohort(
                                name=cohort_date,
                                dimension="firstSessionDate",
                                date_range=DateRange(
                                    start_date=cohort_date,
                                    end_date=cohort_date,
                                ),
                            )
                        ],
                        cohorts_range=CohortsRange(
                            granularity=CohortsRange.Granularity.DAILY,
                            start_offset=0,
                            end_offset=max(RETENTION_DAY_OFFSETS),
                        ),
                    ),
                    **self.report_filter(app),
                    keep_empty_rows=True,
                    limit=page_size,
                    offset=offset,
                )
                response = self.analytics.run_report(request)
                page_rows = self.parse_response_rows(response)
                if not page_rows:
                    break
                for row in page_rows:
                    returned_cohort = str(row.get("cohort", "") or "").strip()
                    if returned_cohort not in {cohort_date, "cohort_0"}:
                        continue
                    country = str(row.get("country", "") or "").strip() or "(not set)"
                    day_offset = self.parse_cohort_day(row.get("cohortNthDay", 0))
                    active_users = to_float(row.get("cohortActiveUsers", 0))
                    total_users = to_float(row.get("cohortTotalUsers", 0))
                    item = country_data.setdefault(
                        country,
                        {"cohort_total_users": 0.0, "days": {}},
                    )
                    if day_offset == 0 and total_users > 0:
                        item["cohort_total_users"] = total_users
                    item["days"][day_offset] = active_users
                offset += len(page_rows)
                total_rows = int(getattr(response, "row_count", 0) or 0)
                if len(page_rows) < page_size or (total_rows and offset >= total_rows):
                    break

            sorted_countries = sorted(
                country_data.items(),
                key=lambda item: (
                    -to_float(item[1].get("cohort_total_users", 0)),
                    item[0].lower(),
                ),
            )
            for country, metrics in sorted_countries:
                cohort_total = to_float(metrics.get("cohort_total_users", 0))
                if cohort_total <= 0:
                    continue
                output_item: dict[str, Any] = {
                    "Cohort Date": cohort_date,
                    "Country": country,
                }
                for day_offset in RETENTION_DAY_OFFSETS:
                    key = f"D{day_offset} Retention"
                    if not self.retention_ready(
                        cohort_date,
                        day_offset,
                        observation_end_date,
                    ):
                        output_item[key] = "Not available"
                    else:
                        active = to_float(metrics.get("days", {}).get(day_offset, 0))
                        output_item[key] = f"{(active / cohort_total) * 100:.2f}%"
                retention_by_date[cohort_date].append(output_item)
        return retention_by_date

    @staticmethod
    def _append_unique(values: list[str], value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)

    def _collect_audience_fields(
        self,
        node: Any,
        event_names: list[str],
        countries: list[str],
    ) -> None:
        if isinstance(node, list):
            for item in node:
                self._collect_audience_fields(item, event_names, countries)
            return
        if not isinstance(node, dict):
            return
        event_filter = node.get("eventFilter")
        if isinstance(event_filter, dict):
            self._append_unique(event_names, event_filter.get("eventName", ""))
        dimension_filter = node.get("dimensionOrMetricFilter")
        if isinstance(dimension_filter, dict):
            field_name = str(dimension_filter.get("fieldName", "") or "").lower()
            if field_name in {"country", "countryid", "country_id"}:
                string_filter = dimension_filter.get("stringFilter", {}) or {}
                self._append_unique(countries, string_filter.get("value", ""))
                in_list = dimension_filter.get("inListFilter", {}) or {}
                for item in in_list.get("values", []) or []:
                    self._append_unique(countries, item)
        for value in node.values():
            self._collect_audience_fields(value, event_names, countries)

    def fetch_audience_definitions(self, app: AppTarget) -> dict[str, dict[str, Any]]:
        if app.property_id in self.audience_definition_cache:
            return self.audience_definition_cache[app.property_id]
        definitions: dict[str, dict[str, Any]] = {}
        url = (
            f"{self.config.ga4_admin_audience_api_base}/properties/"
            f"{app.property_id}/audiences"
        )
        try:
            for audience in self.rest.paginated_get(
                url,
                item_key="audiences",
                params={"pageSize": 200},
            ):
                resource_name = str(audience.get("name", "") or "").strip()
                display_name = str(audience.get("displayName", "") or "").strip()
                if not resource_name and not display_name:
                    continue
                event_names: list[str] = []
                countries: list[str] = []
                self._collect_audience_fields(
                    audience.get("filterClauses", []) or [],
                    event_names,
                    countries,
                )
                event_trigger = audience.get("eventTrigger", {}) or {}
                self._append_unique(event_names, event_trigger.get("eventName", ""))
                definition = {
                    "resource_name": resource_name,
                    "display_name": display_name,
                    "events_name": ", ".join(event_names),
                    "countries": ", ".join(countries),
                    "create_time": str(audience.get("createTime", "") or "").strip(),
                }
                definitions[resource_name or display_name] = definition
        except Exception as exc:
            LOGGER.warning(
                "Audience definitions unavailable for %s: %s", app.package_name, exc
            )
            if not self.config.continue_on_error:
                raise
        self.audience_definition_cache[app.property_id] = definitions
        return definitions

    def run_audience_report(
        self,
        app: AppTarget,
        report_dates: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        definitions = self.fetch_audience_definitions(app)
        by_date = {report_date: [] for report_date in report_dates}
        if not definitions:
            return by_date

        definitions_by_resource = {
            str(item.get("resource_name", "") or "").strip(): item
            for item in definitions.values()
            if str(item.get("resource_name", "") or "").strip()
        }
        definitions_by_name = {
            str(item.get("display_name", "") or "").strip(): item
            for item in definitions.values()
            if str(item.get("display_name", "") or "").strip()
        }
        totals_by_key: dict[str, float] = {}
        page_size = 100000
        offset = 0
        while True:
            request = RunReportRequest(
                property=f"properties/{app.property_id}",
                date_ranges=[
                    DateRange(
                        start_date=self.config.start_date,
                        end_date=self.config.end_date,
                    )
                ],
                dimensions=[
                    Dimension(name="audienceResourceName"),
                    Dimension(name="audienceName"),
                ],
                metrics=[Metric(name="totalUsers")],
                **self.report_filter(app),
                order_bys=[self.metric_order("totalUsers")],
                keep_empty_rows=False,
                limit=page_size,
                offset=offset,
            )
            response = self.analytics.run_report(request)
            page_rows = self.parse_response_rows(response)
            if not page_rows:
                break
            for row in page_rows:
                resource_name = str(row.get("audienceResourceName", "") or "").strip()
                reported_name = str(row.get("audienceName", "") or "").strip()
                if reported_name in {"", "(not set)"}:
                    continue
                definition = definitions_by_resource.get(resource_name)
                if definition is None:
                    definition = definitions_by_name.get(reported_name)
                if definition is None:
                    continue
                key = (
                    str(definition.get("resource_name", "") or "").strip()
                    or str(definition.get("display_name", "") or "").strip()
                )
                totals_by_key[key] = totals_by_key.get(key, 0.0) + to_float(
                    row.get("totalUsers", 0)
                )
            offset += len(page_rows)
            total_rows = int(getattr(response, "row_count", 0) or 0)
            if len(page_rows) < page_size or (total_rows and offset >= total_rows):
                break

        summary_rows: list[dict[str, Any]] = []
        for definition in definitions.values():
            audience_name = str(definition.get("display_name", "") or "").strip()
            if not audience_name:
                continue
            key = (
                str(definition.get("resource_name", "") or "").strip() or audience_name
            )
            summary_rows.append(
                {
                    "Audience_Name": audience_name,
                    "Events_Name": definition.get("events_name", ""),
                    "Countries": definition.get("countries", ""),
                    "Total_Users": to_number(totals_by_key.get(key, 0)),
                    "_create_time": definition.get("create_time", ""),
                }
            )
        summary_rows.sort(
            key=lambda item: (
                str(item.get("_create_time", "")),
                str(item.get("Audience_Name", "")).lower(),
            ),
            reverse=True,
        )
        for item in summary_rows:
            item.pop("_create_time", None)
        for report_date in report_dates:
            by_date[report_date] = [dict(item) for item in summary_rows]
        return by_date

    @staticmethod
    def personalized_dimensions(app: AppTarget) -> list[tuple[str, str]]:
        return [
            ("Country", "country"),
            ("Language", "language"),
            ("Device Category", "deviceCategory"),
            ("Operating System", "operatingSystem"),
            ("App Version", "appVersion"),
            ("First User Medium", "firstUserMedium"),
            ("Top Screens / Screen Class", app.screen_field),
        ]

    def run_dimension_session_report(
        self,
        app: AppTarget,
        label: str,
        dimension_name: str,
        report_dates: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        by_date = {report_date: [] for report_date in report_dates}
        page_size = 100000
        offset = 0
        while True:
            request = RunReportRequest(
                property=f"properties/{app.property_id}",
                date_ranges=[
                    DateRange(
                        start_date=self.config.start_date,
                        end_date=self.config.end_date,
                    )
                ],
                dimensions=[Dimension(name="date"), Dimension(name=dimension_name)],
                metrics=[
                    Metric(name="activeUsers"),
                    Metric(name="sessions"),
                    Metric(name="averageSessionDuration"),
                    Metric(name="engagementRate"),
                ],
                **self.report_filter(app),
                order_bys=[self.date_order(), self.metric_order("activeUsers")],
                limit=page_size,
                offset=offset,
            )
            response = self.analytics.run_report(request)
            page_rows = self.parse_response_rows(response)
            if not page_rows:
                break
            for row in page_rows:
                report_date = ga4_date_to_iso(row.get("date", ""))
                if report_date not in by_date:
                    continue
                if len(by_date[report_date]) >= self.config.personalized_top_n:
                    continue
                by_date[report_date].append(
                    {
                        "label": label,
                        "value": row.get(dimension_name, "") or "(not set)",
                        "active": to_number(row.get("activeUsers", 0)),
                        "sessions": to_number(row.get("sessions", 0)),
                        "avg": format_seconds(row.get("averageSessionDuration", 0)),
                        "engagement": percent(row.get("engagementRate", 0)),
                    }
                )
            offset += len(page_rows)
            total_rows = int(getattr(response, "row_count", 0) or 0)
            if len(page_rows) < page_size or (total_rows and offset >= total_rows):
                break
        return by_date

    def run_personalized_ux(
        self,
        app: AppTarget,
        report_dates: list[str],
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        result: dict[str, dict[str, list[dict[str, Any]]]] = {
            report_date: {} for report_date in report_dates
        }
        for label, dimension_name in self.personalized_dimensions(app):
            try:
                by_date = self.run_dimension_session_report(
                    app,
                    label,
                    dimension_name,
                    report_dates,
                )
                for report_date in report_dates:
                    result[report_date][label] = by_date.get(report_date, [])
            except Exception as exc:
                LOGGER.warning(
                    "Personalized UX %s unavailable for %s: %s",
                    label,
                    app.package_name,
                    exc,
                )
                if not self.config.continue_on_error:
                    raise
                for report_date in report_dates:
                    result[report_date][label] = []
        return result

    # -------------------------
    # Firebase Remote Config
    # -------------------------
    def get_remote_config_template(self, project_id: str) -> dict[str, Any]:
        project_id = str(project_id or "").strip()
        if not project_id:
            raise ValueError("Firebase Project ID is unavailable for this GA4 stream.")
        if project_id in self.remote_config_cache:
            return self.remote_config_cache[project_id]

        project_path = f"projects/{project_id}"
        legacy_url = (
            f"{self.config.firebase_remote_config_api_base}/{project_path}/remoteConfig"
        )
        namespace_name = f"{project_path}/namespaces/{self.config.remote_config_namespace}/remoteConfig"
        try:
            template = self.rest.request_json(
                "GET",
                legacy_url,
                params={"name": namespace_name},
            )
        except Exception:
            namespace_url = (
                f"{self.config.firebase_remote_config_api_base}/{namespace_name}"
            )
            template = self.rest.request_json("GET", namespace_url)
        self.remote_config_cache[project_id] = template
        return template

    @staticmethod
    def iter_remote_parameters(
        template: Mapping[str, Any],
    ) -> Iterator[tuple[str, dict[str, Any], str]]:
        for key, parameter in (template.get("parameters", {}) or {}).items():
            if isinstance(parameter, dict):
                yield str(key), parameter, ""
        for group_name, group_data in (
            template.get("parameterGroups", {}) or {}
        ).items():
            if not isinstance(group_data, Mapping):
                continue
            for key, parameter in (group_data.get("parameters", {}) or {}).items():
                if isinstance(parameter, dict):
                    yield str(key), parameter, str(group_name)

    @staticmethod
    def format_remote_value(value_object: Any) -> str:
        if value_object is None or value_object == "":
            return ""
        if isinstance(value_object, dict):
            if "value" in value_object:
                return str(value_object.get("value", ""))
            if value_object.get("useInAppDefault") is True:
                return "Use in-app default"
        return json.dumps(value_object, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def remote_condition_map(template: Mapping[str, Any]) -> dict[str, str]:
        return {
            str(item.get("name", "") or ""): str(item.get("expression", "") or "")
            for item in template.get("conditions", []) or []
            if item.get("name")
        }

    @staticmethod
    def extract_fetch_percent(condition_expression: str) -> str:
        expression = str(condition_expression or "")
        if not expression:
            return ""
        range_match = re.search(
            r"percent\s+in\s*\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]",
            expression,
            flags=re.IGNORECASE,
        )
        if range_match:
            start = float(range_match.group(1))
            end = float(range_match.group(2))
            return f"{max(end - start, 0):g}%"
        threshold_match = re.search(
            r"percent\s*(?:<=|<|==)\s*(\d+(?:\.\d+)?)",
            expression,
            flags=re.IGNORECASE,
        )
        if threshold_match:
            return f"{float(threshold_match.group(1)):g}%"
        return ""

    @staticmethod
    def format_last_published(version: Mapping[str, Any]) -> str:
        update_time = str(version.get("updateTime", "") or "").strip()
        update_user = version.get("updateUser", {}) or {}
        publisher = str(
            update_user.get("email", "") or update_user.get("name", "") or ""
        ).strip()
        if publisher and update_time:
            return f"{publisher} | {update_time}"
        return publisher or update_time

    def find_remote_parameters(
        self,
        template: Mapping[str, Any],
        keywords: str,
    ) -> list[tuple[str, dict[str, Any], str]]:
        keyword_values = [normalize(item) for item in split_csv(keywords)]
        exact: list[tuple[str, dict[str, Any], str]] = []
        fuzzy: list[tuple[str, dict[str, Any], str]] = []
        for key, parameter, group_name in self.iter_remote_parameters(template):
            normalized_key = normalize(key)
            item = (key, parameter, group_name)
            if normalized_key in keyword_values:
                exact.append(item)
            elif any(
                keyword and keyword in normalized_key for keyword in keyword_values
            ):
                fuzzy.append(item)
        matches = exact + [item for item in fuzzy if item not in exact]
        return matches[: self.config.remote_parameter_limit]

    def build_remote_parameter_rows(
        self,
        key: str,
        parameter: Mapping[str, Any],
        group_name: str,
        condition_map: Mapping[str, str],
        last_published: str,
    ) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        display_name = key if not group_name else f"{group_name}/{key}"
        if "defaultValue" in parameter:
            value = self.format_remote_value(parameter.get("defaultValue"))
            if value != "":
                rows.append(
                    {
                        "name": display_name,
                        "condition": "Default value",
                        "value": value,
                        "fetch_percent": "100%",
                        "last_published": last_published,
                    }
                )
        for condition_name, value_object in (
            parameter.get("conditionalValues", {}) or {}
        ).items():
            value = self.format_remote_value(value_object)
            if value == "":
                continue
            rows.append(
                {
                    "name": display_name,
                    "condition": str(condition_name),
                    "value": value,
                    "fetch_percent": self.extract_fetch_percent(
                        condition_map.get(str(condition_name), "")
                    ),
                    "last_published": last_published,
                }
            )
        if not rows:
            rows.append(
                {
                    "name": display_name,
                    "condition": "",
                    "value": "No configured value",
                    "fetch_percent": "",
                    "last_published": last_published,
                }
            )
        return rows

    def get_remote_config_rows(self, app: AppTarget) -> dict[str, list[dict[str, str]]]:
        empty = {"time_capping_rows": [], "iap_screen_rows": []}
        if not app.firebase_project_id:
            return empty
        try:
            template = self.get_remote_config_template(app.firebase_project_id)
            condition_map = self.remote_condition_map(template)
            last_published = self.format_last_published(
                template.get("version", {}) or {}
            )

            time_rows: list[dict[str, str]] = []
            for key, parameter, group in self.find_remote_parameters(
                template,
                self.config.time_capping_parameter_keywords,
            ):
                time_rows.extend(
                    self.build_remote_parameter_rows(
                        key,
                        parameter,
                        group,
                        condition_map,
                        last_published,
                    )
                )

            iap_rows: list[dict[str, str]] = []
            for key, parameter, group in self.find_remote_parameters(
                template,
                self.config.iap_screen_parameter_keywords,
            ):
                iap_rows.extend(
                    self.build_remote_parameter_rows(
                        key,
                        parameter,
                        group,
                        condition_map,
                        last_published,
                    )
                )

            return {
                "time_capping_rows": time_rows,
                "iap_screen_rows": iap_rows,
            }
        except Exception as exc:
            LOGGER.warning(
                "Remote Config unavailable for %s: %s", app.package_name, exc
            )
            if not self.config.continue_on_error:
                raise
            return empty

    # -------------------------
    # FCM delivery data
    # -------------------------
    @staticmethod
    def format_fcm_date(date_data: Mapping[str, Any]) -> str:
        year = int(date_data.get("year", 0) or 0)
        month = int(date_data.get("month", 0) or 0)
        day = int(date_data.get("day", 0) or 0)
        if year and month and day:
            return f"{year:04d}-{month:02d}-{day:02d}"
        return ""

    def request_fcm_delivery_data(self, project_id: str, app_id: str) -> dict[str, Any]:
        cache_key = (project_id, app_id)
        if cache_key in self.fcm_delivery_cache:
            return self.fcm_delivery_cache[cache_key]

        def request(encode_colons: bool) -> dict[str, Any]:
            project_part = quote(project_id, safe="-")
            app_part = quote(app_id, safe="" if encode_colons else ":")
            url = (
                f"{self.config.fcm_data_api_base}/projects/{project_part}/"
                f"androidApps/{app_part}/deliveryData"
            )
            rows: list[dict[str, Any]] = []
            for item in self.rest.paginated_get(
                url,
                item_key="androidDeliveryData",
                params={"pageSize": self.config.fcm_data_page_size},
            ):
                rows.append(item)
            return {"androidDeliveryData": rows}

        try:
            payload = request(False)
        except RuntimeError as exc:
            if "400" not in str(exc) and "INVALID_ARGUMENT" not in str(exc):
                raise
            payload = request(True)
        self.fcm_delivery_cache[cache_key] = payload
        return payload

    def build_fcm_fields_by_date(
        self,
        app: AppTarget,
        report_dates: list[str],
    ) -> dict[str, dict[str, Any]]:
        result = {report_date: {} for report_date in report_dates}
        if not app.firebase_project_id or not app.firebase_app_id:
            return result
        try:
            payload = self.request_fcm_delivery_data(
                app.firebase_project_id,
                app.firebase_app_id,
            )
            grouped: dict[str, list[dict[str, Any]]] = {
                report_date: [] for report_date in report_dates
            }
            for delivery in payload.get("androidDeliveryData", []) or []:
                delivery_date = self.format_fcm_date(delivery.get("date", {}) or {})
                if delivery_date in grouped:
                    grouped[delivery_date].append(delivery)

            for report_date, items in grouped.items():
                accepted_total = 0.0
                delivered_weighted = 0.0
                pending_weighted = 0.0
                for item in items:
                    data = item.get("data", {}) or {}
                    accepted = max(to_float(data.get("countMessagesAccepted", 0)), 0.0)
                    if accepted <= 0:
                        continue
                    outcomes = data.get("messageOutcomePercents", {}) or {}
                    accepted_total += accepted
                    delivered_weighted += accepted * to_float(
                        outcomes.get("delivered", 0)
                    )
                    pending_weighted += accepted * to_float(outcomes.get("pending", 0))
                if accepted_total <= 0:
                    continue
                result[report_date] = {
                    "firebase_notifications_accepted": to_number(accepted_total),
                    "firebase_delivered": f"{round(delivered_weighted / accepted_total, 2)}%",
                    "firebase_pending": f"{round(pending_weighted / accepted_total, 2)}%",
                }
            return result
        except Exception as exc:
            LOGGER.warning(
                "FCM delivery data unavailable for %s: %s", app.package_name, exc
            )
            if not self.config.continue_on_error:
                raise
            return result

    # -------------------------
    # Row assembly
    # -------------------------
    @staticmethod
    def get_event_metric(
        event_data: Mapping[tuple[str, str], Mapping[str, Any]],
        report_date: str,
        event_name: str,
    ) -> Mapping[str, Any]:
        return event_data.get((report_date, event_name), {}) or {}

    def pick_first_available_event(
        self,
        event_data: Mapping[tuple[str, str], Mapping[str, Any]],
        report_date: str,
        event_names: list[str],
    ) -> tuple[str, Mapping[str, Any]]:
        for event_name in event_names:
            data = self.get_event_metric(event_data, report_date, event_name)
            if to_float(data.get("active_users", 0)) or to_float(
                data.get("event_count", 0)
            ):
                return event_name, data
        if event_names:
            first = event_names[0]
            return first, self.get_event_metric(event_data, report_date, first)
        return "", {}

    def get_home_metrics_for_date(
        self,
        report_date: str,
        app: AppTarget,
        event_data: Mapping[tuple[str, str], Mapping[str, Any]],
        home_data: Mapping[str, Mapping[str, Any]],
    ) -> tuple[str, int | float, int | float]:
        home_events = split_csv(app.home_event_names)
        if home_events:
            home_event, data = self.pick_first_available_event(
                event_data,
                report_date,
                home_events,
            )
            home_users = to_number(data.get("active_users", 0))
            home_views = to_number(data.get("event_count", 0))
            if to_float(home_users) or to_float(home_views):
                return home_event, home_users, home_views

        # With no Apps Config sheet, home-event inference is necessarily
        # heuristic. Fall back to the inferred home screen when no custom home
        # event fired on this date.
        data = home_data.get(report_date, {}) or {}
        return (
            "screen_view",
            to_number(data.get("active_users", 0)),
            to_number(data.get("event_count", 0)),
        )

    @staticmethod
    def set_audience_columns(row: dict[str, Any], item: Mapping[str, Any]) -> None:
        row["Audience_Name"] = item.get("Audience_Name", "")
        row["Events_Name"] = item.get("Events_Name", "")
        row["Countries"] = item.get("Countries", "")
        row["Total_Users"] = item.get("Total_Users", 0)

    def set_funnel_columns(
        self,
        row: dict[str, Any],
        report_date: str,
        app: AppTarget,
        event_data: Mapping[tuple[str, str], Mapping[str, Any]],
        home_data: Mapping[str, Mapping[str, Any]],
    ) -> None:
        _, app_open_data = self.pick_first_available_event(
            event_data,
            report_date,
            split_csv(app.app_open_event_names),
        )
        app_open_users = to_number(app_open_data.get("active_users", 0))
        app_open_events = to_number(app_open_data.get("event_count", 0))
        _, home_users, home_views = self.get_home_metrics_for_date(
            report_date,
            app,
            event_data,
            home_data,
        )

        row["funnel_app_open_users"] = app_open_users
        row["funnel_app_open_events"] = app_open_events
        row["funnel_home_users"] = home_users
        row["funnel_home_events_views"] = home_views
        row["funnel_possible_drop_off"] = max(
            int(to_float(app_open_users) - to_float(home_users)),
            0,
        )
        row["funnel_home_reach_rate"] = rate(home_users, app_open_users)

        for event_name, slug in (
            ("ad_impression", "ad_impression"),
            ("in_app_purchase", "in_app_purchase"),
        ):
            data = self.get_event_metric(event_data, report_date, event_name)
            row[f"funnel_{slug}_events"] = to_number(data.get("event_count", 0))
            row[f"funnel_{slug}_users"] = to_number(data.get("active_users", 0))

    @staticmethod
    def set_remote_parameter_columns(
        row: dict[str, Any],
        prefix: str,
        item: Mapping[str, Any],
    ) -> None:
        row[f"{prefix}_name"] = item.get("name", "")
        row[f"{prefix}_condition"] = item.get("condition", "")
        row[f"{prefix}_value"] = item.get("value", "")
        row[f"{prefix}_fetch_percent"] = item.get("fetch_percent", "")
        row[f"{prefix}_last_published"] = item.get("last_published", "")

    @staticmethod
    def set_time_analysis_columns(
        row: dict[str, Any],
        metrics: Mapping[str, Any],
    ) -> None:
        row["time_analysis_country"] = metrics.get("Country", "")
        row["time_analysis_active_users"] = metrics.get("Active Users", 0)
        row["time_analysis_new_users"] = metrics.get("New Users", 0)
        row["time_analysis_sessions"] = metrics.get("Sessions", 0)
        row["time_analysis_engaged_sessions"] = metrics.get("Engaged Sessions", 0)
        row["time_analysis_engagement_rate"] = metrics.get("Engagement Rate", "0%")
        row["time_analysis_avg_session_duration"] = metrics.get(
            "Avg Session Duration", "0m 0s"
        )
        row["time_analysis_sessions_per_active_user"] = metrics.get(
            "Sessions Per Active User", 0
        )
        row["time_analysis_total_engagement_time"] = metrics.get(
            "Total Engagement Time", "0m 0s"
        )

    @staticmethod
    def set_retention_columns(
        row: dict[str, Any],
        retention: Mapping[str, Any],
    ) -> None:
        row["retention_cohort_date"] = retention.get("Cohort Date", "")
        row["retention_country"] = retention.get("Country", "")
        row["retention_d1_first_session_retention"] = retention.get(
            "D1 Retention", "Not available"
        )
        row["retention_d3_first_session_retention"] = retention.get(
            "D3 Retention", "Not available"
        )
        row["retention_d7_first_session_retention"] = retention.get(
            "D7 Retention", "Not available"
        )
        row["retention_d30_first_session_retention"] = retention.get(
            "D30 Retention", "Not available"
        )

    @staticmethod
    def set_personalized_columns(
        row: dict[str, Any],
        slug: str,
        item: Mapping[str, Any],
    ) -> None:
        row[f"personalized_category_{slug}"] = item.get("value", "")
        row[f"personalized_{slug}_users"] = item.get("active", 0)
        row[f"personalized_{slug}_sessions"] = item.get("sessions", 0)
        row[f"personalized_{slug}_er"] = item.get("engagement", "")
        row[f"personalized_{slug}_avg"] = item.get("avg", "")

    def build_rows_for_app(
        self,
        app: AppTarget,
        report_dates: list[str],
    ) -> list[dict[str, Any]]:
        LOGGER.info(
            "Processing %s | property=%s | stream=%s | dates=%s..%s",
            app.package_name,
            app.property_id,
            app.ga4_stream_id,
            report_dates[0],
            report_dates[-1],
        )

        app_open_events = split_csv(app.app_open_event_names)
        home_events = split_csv(app.home_event_names)
        event_names = [
            event_name
            for event_name in dict.fromkeys(
                app_open_events + home_events + ["ad_impression", "in_app_purchase"]
            )
            if event_name and event_name.lower() not in EXCLUDED_GA4_EVENT_NAMES
        ]

        time_analysis: dict[str, list[dict[str, Any]]] = {
            report_date: [] for report_date in report_dates
        }
        event_data: dict[tuple[str, str], dict[str, Any]] = {}
        home_data: dict[str, dict[str, Any]] = {}
        retention_data: dict[str, list[dict[str, Any]]] = {
            report_date: [] for report_date in report_dates
        }
        audience_rows: dict[str, list[dict[str, Any]]] = {
            report_date: [] for report_date in report_dates
        }
        personalized_ux: dict[str, dict[str, list[dict[str, Any]]]] = {
            report_date: {} for report_date in report_dates
        }
        fcm_delivery: dict[str, dict[str, Any]] = {
            report_date: {} for report_date in report_dates
        }
        remote_config_rows = {"time_capping_rows": [], "iap_screen_rows": []}

        tasks: list[tuple[str, Any]] = [
            ("time analysis", lambda: self.run_time_analysis_report(app)),
            ("events", lambda: self.run_event_report(app, event_names)),
            ("home screen", lambda: self.run_home_screen_report(app)),
            ("retention", lambda: self.run_retention_report(app, report_dates)),
            ("audiences", lambda: self.run_audience_report(app, report_dates)),
            ("personalized UX", lambda: self.run_personalized_ux(app, report_dates)),
            ("FCM delivery", lambda: self.build_fcm_fields_by_date(app, report_dates)),
            ("Remote Config", lambda: self.get_remote_config_rows(app)),
        ]
        for task_name, task in tasks:
            try:
                result = task()
                if task_name == "time analysis":
                    time_analysis = result
                elif task_name == "events":
                    event_data = result
                elif task_name == "home screen":
                    home_data = result
                elif task_name == "retention":
                    retention_data = result
                elif task_name == "audiences":
                    audience_rows = result
                elif task_name == "personalized UX":
                    personalized_ux = result
                elif task_name == "FCM delivery":
                    fcm_delivery = result
                elif task_name == "Remote Config":
                    remote_config_rows = result
            except Exception as exc:
                LOGGER.warning(
                    "%s failed for %s (%s/%s): %s",
                    task_name,
                    app.package_name,
                    app.property_id,
                    app.ga4_stream_id,
                    exc,
                )
                if not self.config.continue_on_error:
                    raise

        rows: list[dict[str, Any]] = []
        for report_date in report_dates:
            personalized_for_date = personalized_ux.get(report_date, {}) or {}
            personalized_groups: dict[str, list[dict[str, Any]]] = {
                slug: personalized_for_date.get(category, []) or []
                for category, slug in PERSONALIZED_COLUMN_SPECS
            }

            time_capping_items = remote_config_rows.get("time_capping_rows", []) or []
            iap_screen_items = remote_config_rows.get("iap_screen_rows", []) or []
            audience_items = audience_rows.get(report_date, []) or []
            time_analysis_items = time_analysis.get(report_date, []) or []
            retention_items = retention_data.get(report_date, []) or []

            row_count = max(
                [
                    1,
                    len(time_capping_items),
                    len(iap_screen_items),
                    len(audience_items),
                    len(time_analysis_items),
                    len(retention_items),
                ]
                + [len(items) for items in personalized_groups.values()]
            )

            for index in range(row_count):
                row: dict[str, Any] = {header: None for header in OUTPUT_HEADERS}
                row["Package_Name"] = app.package_name
                row["Date"] = report_date

                # Date-level metrics appear once per package/date. The other
                # datasets are independent lists compacted side-by-side.
                if index == 0:
                    row.update(fcm_delivery.get(report_date, {}) or {})
                    self.set_funnel_columns(
                        row,
                        report_date,
                        app,
                        event_data,
                        home_data,
                    )

                if index < len(audience_items):
                    self.set_audience_columns(row, audience_items[index])
                if index < len(time_analysis_items):
                    self.set_time_analysis_columns(row, time_analysis_items[index])
                if index < len(retention_items):
                    self.set_retention_columns(row, retention_items[index])
                if index < len(time_capping_items):
                    self.set_remote_parameter_columns(
                        row,
                        "time_capping",
                        time_capping_items[index],
                    )
                if index < len(iap_screen_items):
                    self.set_remote_parameter_columns(
                        row,
                        "iap_screen",
                        iap_screen_items[index],
                    )
                for _, slug in PERSONALIZED_COLUMN_SPECS:
                    items = personalized_groups.get(slug, [])
                    if index < len(items):
                        self.set_personalized_columns(row, slug, items[index])

                rows.append(row)
        return rows


class BigQueryWriter:
    """Stage all rows first, then publish the complete table in one copy job."""

    def __init__(
        self,
        config: Config,
        credentials: Credentials,
        project_id: str,
    ) -> None:
        self.config = config
        self.project_id = project_id
        self.client = bigquery.Client(
            project=project_id,
            credentials=credentials,
            location=config.bigquery_location,
        )
        self.dataset_ref = bigquery.DatasetReference(
            project_id,
            config.bigquery_dataset_id,
        )
        self.table_ref = self.dataset_ref.table(config.bigquery_table_id)
        self.table_id = (
            f"{project_id}.{config.bigquery_dataset_id}.{config.bigquery_table_id}"
        )

        # A unique staging table allows every app to be processed without
        # changing the live target table during the run.
        run_suffix = (
            f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_"
            f"{uuid4().hex[:8]}"
        )
        self.staging_table_name = (
            f"{config.bigquery_table_id}__staging_{run_suffix}"
        )
        self.staging_table_ref = self.dataset_ref.table(self.staging_table_name)
        self.staging_table_id = (
            f"{project_id}.{config.bigquery_dataset_id}.{self.staging_table_name}"
        )

    @staticmethod
    def _new_table(
        table_ref: bigquery.TableReference,
        description: str,
    ) -> bigquery.Table:
        table = bigquery.Table(table_ref, schema=BIGQUERY_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="Date",
        )
        table.description = description
        return table

    def prepare(self) -> None:
        """Create only the staging table; leave the live table unchanged."""
        dataset = bigquery.Dataset(self.dataset_ref)
        dataset.location = self.config.bigquery_location
        self.client.create_dataset(dataset, exists_ok=True)

        self.client.delete_table(self.staging_table_ref, not_found_ok=True)
        staging_table = self._new_table(
            self.staging_table_ref,
            "Temporary staging table for an atomic GA4/Firebase refresh.",
        )
        self.client.create_table(staging_table)
        LOGGER.info("Created staging table %s", self.staging_table_id)
        LOGGER.info(
            "Live table %s will remain unchanged until every app is processed",
            self.table_id,
        )

    @staticmethod
    def normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for field in BIGQUERY_SCHEMA:
            value = row.get(field.name)
            if value in {"", None}:
                normalized[field.name] = None
                continue
            if field.field_type in {"INTEGER", "INT64"}:
                normalized[field.name] = int(round(to_float(value)))
            elif field.field_type in {"FLOAT", "FLOAT64"}:
                normalized[field.name] = float(value)
            else:
                normalized[field.name] = str(value)
        return normalized

    def write_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        chunk_size: int = 5000,
    ) -> int:
        """Append rows to staging only; this never changes the live table."""
        buffer: list[dict[str, Any]] = []
        total = 0

        def flush() -> None:
            nonlocal total
            if not buffer:
                return
            job_config = bigquery.LoadJobConfig(
                schema=BIGQUERY_SCHEMA,
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
            )
            load_job = self.client.load_table_from_json(
                list(buffer),
                self.staging_table_ref,
                job_config=job_config,
                location=self.config.bigquery_location,
            )
            load_job.result()
            if load_job.errors:
                raise RuntimeError(f"BigQuery staging load errors: {load_job.errors}")
            total += len(buffer)
            LOGGER.info(
                "Staged %d row(s) in this app batch into %s",
                total,
                self.staging_table_id,
            )
            buffer.clear()

        for row in rows:
            buffer.append(self.normalize_row(row))
            if len(buffer) >= chunk_size:
                flush()
        flush()
        return total

    def publish(self) -> None:
        """Atomically replace the live table after all staging loads succeed."""
        job_config = bigquery.CopyJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        )
        copy_job = self.client.copy_table(
            self.staging_table_ref,
            self.table_ref,
            job_config=job_config,
            location=self.config.bigquery_location,
        )
        copy_job.result()
        if copy_job.errors:
            raise RuntimeError(f"BigQuery publish errors: {copy_job.errors}")
        LOGGER.info(
            "Published the complete staging table to %s in one final update",
            self.table_id,
        )

    def cleanup(self) -> None:
        try:
            self.client.delete_table(self.staging_table_ref, not_found_ok=True)
            LOGGER.info("Removed staging table %s", self.staging_table_id)
        except Exception as exc:
            LOGGER.warning(
                "Could not remove staging table %s: %s",
                self.staging_table_id,
                exc,
            )

def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def main() -> int:
    configure_logging()
    try:
        config = load_config()
        credentials = load_credentials(config.service_account_json)
        project_id = config.bigquery_project_id or credentials_project_id(
            credentials,
            config.service_account_json,
        )
        if not project_id:
            raise ValueError(
                "BIGQUERY_PROJECT_ID is required because a project_id could not "
                "be derived from the service-account JSON."
            )

        report_dates = get_report_dates(config)
        LOGGER.info("Report date range: %s to %s", report_dates[0], report_dates[-1])

        pipeline = Pipeline(config, credentials)
        apps = pipeline.discover_apps()
        LOGGER.info("Total accessible Android app streams: %d", len(apps))

        writer = BigQueryWriter(config, credentials, project_id)
        writer.prepare()

        total_rows = 0
        failed_apps: list[str] = []
        try:
            for app in apps:
                try:
                    pipeline.infer_ga4_app_configuration(app)
                    app_rows = pipeline.build_rows_for_app(app, report_dates)
                    total_rows += writer.write_rows(app_rows)
                except Exception as exc:
                    failed_apps.append(
                        f"{app.package_name} ({app.property_id}/{app.ga4_stream_id})"
                    )
                    LOGGER.exception(
                        "App failed: %s | property=%s | stream=%s | %s",
                        app.package_name,
                        app.property_id,
                        app.ga4_stream_id,
                        exc,
                    )
                    if not config.continue_on_error:
                        raise

            if failed_apps:
                raise RuntimeError(
                    f"{len(failed_apps)} app(s) failed. The live table was not "
                    "updated. Failed apps: " + "; ".join(failed_apps[:20])
                )
            if total_rows <= 0:
                raise RuntimeError(
                    "No rows were generated. The live table was not updated."
                )

            # Only this final copy changes the live table. Until this point, all
            # data exists exclusively in the staging table.
            writer.publish()
        finally:
            writer.cleanup()

        LOGGER.info(
            "Completed: %d rows published together to %s",
            total_rows,
            writer.table_id,
        )
        return 0
    except Exception as exc:
        LOGGER.exception("Pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote

from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

from google.analytics.data_v1alpha import AlphaAnalyticsDataClient
from google.analytics.data_v1alpha.types import (
    DateRange as AlphaDateRange,
    Funnel,
    FunnelStep,
    FunnelEventFilter,
    FunnelFieldFilter,
    FunnelFilterExpression,
    FunnelFilterExpressionList,
    RunFunnelReportRequest,
    StringFilter,
)

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange as BetaDateRange,
    Dimension,
    Metric,
    RunReportRequest,
    Cohort,
    CohortSpec,
    CohortsRange,
    OrderBy,
    Filter as BetaFilter,
    FilterExpression as BetaFilterExpression,
    FilterExpressionList as BetaFilterExpressionList,
)

from googleapiclient.discovery import build

from config import SCOPES, load_config


config = load_config()


@dataclass
class AppConfig:
    app_name: str
    property_id: str
    home_screen_name: str
    screen_field: str
    firebase_project_id: str
    firebase_project_name: str
    firebase_app_id: str
    time_capping_parameter: str
    daily_notification_parameters: str
    iap_screen_parameter: str


def get_credentials():
    service_account_info = json.loads(config.service_account_json)

    return service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )


credentials = get_credentials()

alpha_client = AlphaAnalyticsDataClient(credentials=credentials)
beta_client = BetaAnalyticsDataClient(credentials=credentials)
remote_config_session = None
analytics_admin_session = None
package_name_cache = {}


def get_sheets_service():
    return build(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )


def ensure_sheet_exists(service, sheet_name: str):
    spreadsheet = service.spreadsheets().get(
        spreadsheetId=config.spreadsheet_id
    ).execute()

    existing_sheets = [
        sheet["properties"]["title"]
        for sheet in spreadsheet.get("sheets", [])
    ]

    if sheet_name not in existing_sheets:
        service.spreadsheets().batchUpdate(
            spreadsheetId=config.spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": sheet_name
                            }
                        }
                    }
                ]
            },
        ).execute()


def write_sheet(sheet_name: str, rows: list[list]):
    service = get_sheets_service()
    ensure_sheet_exists(service, sheet_name)

    service.spreadsheets().values().clear(
        spreadsheetId=config.spreadsheet_id,
        range=f"{sheet_name}!A:ZZ",
        body={},
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=config.spreadsheet_id,
        range=f"{sheet_name}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()



def get_apps_config_headers() -> list[str]:
    return [
        "Enabled",
        "App Name",
        "Property ID",
        "Home Screen Name",
        "Screen Field",
        "Firebase Project ID",
        "Firebase Project Name",
        "Firebase App ID",
        "Time Capping Parameter",
        "Daily Notification Parameters",
        "IAP Screen Parameter",
    ]


def ensure_apps_config_headers(service, values: list[list]):
    expected_headers = get_apps_config_headers()
    current_headers = values[0] if values else []

    if current_headers[: len(expected_headers)] == expected_headers:
        return

    service.spreadsheets().values().update(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.apps_config_sheet}!A1:K1",
        valueInputOption="USER_ENTERED",
        body={"values": [expected_headers]},
    ).execute()

def create_apps_config_template(service):
    ensure_sheet_exists(service, config.apps_config_sheet)

    rows = [
        get_apps_config_headers(),
        [
            "TRUE",
            "ai-voice-generator-b2073",
            "498019838",
            "MainActivity",
            "unifiedPagePathScreen",
            "your-firebase-project-id",
            "Your Firebase Project Name",
            "1:1234567890:android:abcdef123456",
            "ad_time_capping",
            "",
            "iap_screen",
        ],
        [
            "TRUE",
            "antivirus-vibrant-soft",
            "504100281",
            "MainActivity",
            "unifiedPagePathScreen",
            "antivirus-vibrant-soft",
            "Antivirus vibrant soft",
            "1:1234567890:android:abcdef123456",
            "ad_time_capping",
            "",
            "iap_screen",
        ],
    ]

    service.spreadsheets().values().update(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.apps_config_sheet}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()


def read_apps_config() -> list[AppConfig]:
    service = get_sheets_service()
    ensure_sheet_exists(service, config.apps_config_sheet)

    response = service.spreadsheets().values().get(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.apps_config_sheet}!A:K",
    ).execute()

    values = response.get("values", [])

    if len(values) <= 1:
        create_apps_config_template(service)
        raise SystemExit(
            "Apps Config sheet was empty. Template created. Fill apps and run again."
        )

    ensure_apps_config_headers(service, values)

    apps = []

    for index, row in enumerate(values[1:], start=2):
        enabled = row[0].strip().upper() if len(row) > 0 else ""
        app_name = row[1].strip() if len(row) > 1 else ""
        property_id = row[2].strip() if len(row) > 2 else ""

        home_screen_name = (
            row[3].strip()
            if len(row) > 3 and row[3].strip()
            else config.default_home_screen_name
        )

        screen_field = (
            row[4].strip()
            if len(row) > 4 and row[4].strip()
            else config.default_screen_field
        )

        firebase_project_id = row[5].strip() if len(row) > 5 else ""
        firebase_project_name = row[6].strip() if len(row) > 6 else ""
        firebase_app_id = row[7].strip() if len(row) > 7 else ""
        time_capping_parameter = (
            row[8].strip()
            if len(row) > 8 and row[8].strip()
            else config.time_capping_parameter
        )
        daily_notification_parameters = (
            row[9].strip()
            if len(row) > 9 and row[9].strip()
            else config.daily_notification_parameters
        )
        iap_screen_parameter = (
            row[10].strip()
            if len(row) > 10 and row[10].strip()
            else config.iap_screen_parameter
        )

        if enabled not in ["TRUE", "YES", "1", "Y"]:
            continue

        if not app_name or not property_id:
            print(f"Skipping row {index}: app name or property ID missing.")
            continue

        apps.append(
            AppConfig(
                app_name=app_name,
                property_id=property_id,
                home_screen_name=home_screen_name,
                screen_field=screen_field,
                firebase_project_id=firebase_project_id,
                firebase_project_name=firebase_project_name,
                firebase_app_id=firebase_app_id,
                time_capping_parameter=time_capping_parameter,
                daily_notification_parameters=daily_notification_parameters,
                iap_screen_parameter=iap_screen_parameter,
            )
        )

    if not apps:
        raise SystemExit("No enabled apps found in Apps Config sheet.")

    return apps


def now_text() -> str:
    return datetime.now(
        ZoneInfo(config.timezone)
    ).strftime("%Y-%m-%d %I:%M:%S %p")


def resolve_ga4_date(value: str) -> str:
    value = str(value).strip()
    today = datetime.now(ZoneInfo(config.timezone)).date()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value

    if value.lower() == "today":
        return today.isoformat()

    if value.lower() == "yesterday":
        return (today - timedelta(days=1)).isoformat()

    match = re.fullmatch(r"(\d+)daysAgo", value, re.IGNORECASE)

    if match:
        days = int(match.group(1))
        return (today - timedelta(days=days)).isoformat()

    return value


def get_report_date_range_display() -> str:
    start = resolve_ga4_date(config.start_date)
    end = resolve_ga4_date(config.end_date)

    return f"{start} to {end}"


def get_retention_cohort_date_range() -> tuple[str, str]:
    report_start = datetime.fromisoformat(
        resolve_ga4_date(config.start_date)
    ).date()

    report_end = datetime.fromisoformat(
        resolve_ga4_date(config.end_date)
    ).date()

    cohort_end = report_end - timedelta(days=config.retention_days)

    if cohort_end < report_start:
        cohort_end = report_start

    return report_start.isoformat(), cohort_end.isoformat()


def to_number(value):
    if value in [None, ""]:
        return 0

    try:
        return int(float(value))
    except Exception:
        return value


def to_float(value):
    if value in [None, ""]:
        return 0.0

    try:
        return float(value)
    except Exception:
        return 0.0


def to_percent(value):
    if value in [None, ""]:
        return ""

    try:
        number = float(value)

        if number <= 1:
            number = number * 100

        return f"{round(number, 2)}%"
    except Exception:
        return value


def make_rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0%"

    return f"{round((numerator / denominator) * 100, 2)}%"


def format_seconds(seconds_value) -> str:
    seconds = int(round(to_float(seconds_value)))

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60

    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"

    return f"{minutes}m {seconds}s"


def get_metric(row_data: dict, possible_names: list[str], default=""):
    for name in possible_names:
        if name in row_data:
            return row_data[name]

    return default


def classify_api_error(error) -> tuple[str, str]:
    error_text = str(error)
    error_lower = error_text.lower()

    api_not_enabled_keywords = [
        "service_disabled",
        "api has not been used",
        "has not been enabled",
        "it is disabled",
        "api not enabled",
        "enable it by visiting",
        "service disabled",
        "api disabled",
    ]

    no_access_keywords = [
        "403",
        "permission denied",
        "does not have sufficient permissions",
        "user does not have sufficient permissions",
        "access denied",
        "permission",
    ]

    invalid_property_keywords = [
        "404",
        "not found",
        "property not found",
        "invalid property",
    ]

    if any(keyword in error_lower for keyword in api_not_enabled_keywords):
        return "API NOT ENABLED", error_text

    if any(keyword in error_lower for keyword in no_access_keywords):
        return "NO ACCESS", error_text

    if any(keyword in error_lower for keyword in invalid_property_keywords):
        return "INVALID PROPERTY ID", error_text

    return "ERROR", error_text


# =========================
# GA4 APP PACKAGE NAME
# =========================


def get_analytics_admin_session():
    global analytics_admin_session

    if analytics_admin_session is None:
        analytics_admin_session = AuthorizedSession(credentials)

    return analytics_admin_session


def get_android_package_name_from_stream(stream: dict) -> str:
    android_data = stream.get("androidAppStreamData", {}) or {}
    return str(android_data.get("packageName", "")).strip()


def get_firebase_app_id_from_stream(stream: dict) -> str:
    android_data = stream.get("androidAppStreamData", {}) or {}
    return str(android_data.get("firebaseAppId", "")).strip()


def fetch_ga4_package_name(property_id: str, firebase_app_id: str = "") -> str:
    property_id = str(property_id).strip()
    firebase_app_id = str(firebase_app_id).strip()

    if not property_id:
        return ""

    cache_key = f"{property_id}|{firebase_app_id}"

    if cache_key in package_name_cache:
        return package_name_cache[cache_key]

    try:
        streams = fetch_ga4_data_streams(property_id)

        android_streams = [
            stream
            for stream in streams
            if get_android_package_name_from_stream(stream)
        ]

        if firebase_app_id:
            for stream in android_streams:
                if get_firebase_app_id_from_stream(stream) == firebase_app_id:
                    package_name = get_android_package_name_from_stream(stream)
                    package_name_cache[cache_key] = package_name
                    return package_name

        package_names = []
        seen = set()

        for stream in android_streams:
            package_name = get_android_package_name_from_stream(stream)
            if package_name and package_name not in seen:
                package_names.append(package_name)
                seen.add(package_name)

        package_name = ", ".join(package_names)
        package_name_cache[cache_key] = package_name
        return package_name

    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"PACKAGE NAME {status} for property {property_id}: {error_text}")
        package_name_cache[cache_key] = ""
        return ""


# =========================
# AUTO DISCOVER ACCESSIBLE APPS
# =========================


def extract_id_from_resource_name(resource_name: str) -> str:
    value = str(resource_name or "").strip()
    if "/" not in value:
        return value
    return value.rstrip("/").split("/")[-1]


def fetch_ga4_data_streams(property_id: str) -> list[dict]:
    property_id = str(property_id or "").strip()
    if not property_id:
        return []

    url = f"{config.ga4_admin_api_base}/properties/{property_id}/dataStreams"
    params = {"pageSize": 200}
    streams = []

    while True:
        response = get_analytics_admin_session().get(
            url,
            params=params,
            timeout=30,
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"GA4 Admin API error {response.status_code}: {response.text}"
            )

        payload = response.json()
        streams.extend(payload.get("dataStreams", []) or [])

        next_page_token = payload.get("nextPageToken", "")
        if not next_page_token:
            break

        params["pageToken"] = next_page_token

    return streams


def list_accessible_ga4_properties() -> list[dict]:
    url = f"{config.ga4_admin_api_base}/accountSummaries"
    params = {"pageSize": 200}
    properties = []
    seen = set()

    while True:
        response = get_analytics_admin_session().get(
            url,
            params=params,
            timeout=30,
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"GA4 Admin API error {response.status_code}: {response.text}"
            )

        payload = response.json()

        for account_summary in payload.get("accountSummaries", []) or []:
            account_name = account_summary.get("name", "")
            account_display_name = account_summary.get("displayName", "")

            for property_summary in account_summary.get("propertySummaries", []) or []:
                property_resource = property_summary.get("property", "")
                property_id = extract_id_from_resource_name(property_resource)

                if not property_id or property_id in seen:
                    continue

                seen.add(property_id)
                properties.append(
                    {
                        "property_id": property_id,
                        "property_resource": property_resource,
                        "property_display_name": property_summary.get("displayName", ""),
                        "property_type": property_summary.get("propertyType", ""),
                        "account_name": account_name,
                        "account_display_name": account_display_name,
                    }
                )

        next_page_token = payload.get("nextPageToken", "")
        if not next_page_token:
            break

        params["pageToken"] = next_page_token

    return properties


def list_accessible_firebase_projects() -> list[dict]:
    url = f"{config.firebase_management_api_base}/projects"
    params = {"pageSize": 100}
    projects = []
    seen = set()

    while True:
        response = get_notification_api_session().get(
            url,
            params=params,
            timeout=config.firebase_remote_config_timeout,
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"Firebase Management API error {response.status_code}: {response.text}"
            )

        payload = response.json()
        project_rows = payload.get("results", []) or payload.get("projects", []) or []

        for project in project_rows:
            project_id = project.get("projectId", "") or extract_id_from_resource_name(project.get("name", ""))
            if not project_id or project_id in seen:
                continue

            seen.add(project_id)
            projects.append(project)

        next_page_token = payload.get("nextPageToken", "")
        if not next_page_token:
            break

        params["pageToken"] = next_page_token

    return projects


def normalise_lookup_key(value: str) -> str:
    return str(value or "").strip().lower()


def add_firebase_app_to_index(index: dict[str, dict], key: str, app: dict):
    key = normalise_lookup_key(key)
    if key and key not in index:
        index[key] = app


def build_accessible_firebase_android_app_index() -> dict[str, dict]:
    index = {}

    try:
        projects = list_accessible_firebase_projects()
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"FIREBASE PROJECT DISCOVERY {status}: {error_text}")
        return index

    print(f"Accessible Firebase projects found: {len(projects)}")

    for project in projects:
        project_id = project.get("projectId", "") or extract_id_from_resource_name(project.get("name", ""))
        project_display_name = project.get("displayName", "") or project_id

        if not project_id:
            continue

        try:
            android_apps = list_firebase_android_apps(project_id)
        except Exception as error:
            status, error_text = classify_api_error(error)
            print(f"FIREBASE ANDROID APP DISCOVERY {status} for {project_id}: {error_text}")
            continue

        for android_app in android_apps:
            enriched_app = dict(android_app)
            enriched_app["_firebase_project_id"] = project_id
            enriched_app["_firebase_project_name"] = project_display_name

            add_firebase_app_to_index(index, enriched_app.get("appId", ""), enriched_app)
            add_firebase_app_to_index(index, enriched_app.get("packageName", ""), enriched_app)
            add_firebase_app_to_index(index, enriched_app.get("displayName", ""), enriched_app)

    return index


def is_android_stream(stream: dict) -> bool:
    return bool(get_android_package_name_from_stream(stream) or get_firebase_app_id_from_stream(stream))


def get_stream_display_name(stream: dict) -> str:
    return str(stream.get("displayName", "") or "").strip()


def choose_firebase_app_for_ga4_streams(android_streams: list[dict], firebase_app_index: dict[str, dict]) -> dict | None:
    if not android_streams or not firebase_app_index:
        return None

    # Best match: Firebase App ID from the GA4 Android data stream.
    for stream in android_streams:
        firebase_app_id = get_firebase_app_id_from_stream(stream)
        match = firebase_app_index.get(normalise_lookup_key(firebase_app_id))
        if match:
            return match

    # Next best match: Android package name.
    for stream in android_streams:
        package_name = get_android_package_name_from_stream(stream)
        match = firebase_app_index.get(normalise_lookup_key(package_name))
        if match:
            return match

    # Last fallback: stream display name.
    for stream in android_streams:
        display_name = get_stream_display_name(stream)
        match = firebase_app_index.get(normalise_lookup_key(display_name))
        if match:
            return match

    return None


def build_auto_discovered_app_config(
    property_summary: dict,
    android_streams: list[dict],
    firebase_app: dict | None,
) -> AppConfig:
    property_id = property_summary.get("property_id", "")
    property_display_name = str(property_summary.get("property_display_name", "") or "").strip()

    stream_names = [get_stream_display_name(stream) for stream in android_streams if get_stream_display_name(stream)]
    package_names = [get_android_package_name_from_stream(stream) for stream in android_streams if get_android_package_name_from_stream(stream)]

    app_name = property_display_name
    if not app_name and stream_names:
        app_name = stream_names[0]
    if not app_name and package_names:
        app_name = package_names[0]
    if not app_name:
        app_name = f"GA4 Property {property_id}"

    firebase_app_id = ""
    if firebase_app:
        firebase_app_id = str(firebase_app.get("appId", "") or "").strip()

    if not firebase_app_id and android_streams:
        firebase_app_id = get_firebase_app_id_from_stream(android_streams[0])

    return AppConfig(
        app_name=app_name,
        property_id=property_id,
        home_screen_name=config.default_home_screen_name,
        screen_field=config.default_screen_field,
        firebase_project_id=str(firebase_app.get("_firebase_project_id", "") if firebase_app else "").strip(),
        firebase_project_name=str(firebase_app.get("_firebase_project_name", "") if firebase_app else "").strip(),
        firebase_app_id=firebase_app_id,
        time_capping_parameter=config.time_capping_parameter,
        daily_notification_parameters=config.daily_notification_parameters,
        iap_screen_parameter=config.iap_screen_parameter,
    )


def discover_accessible_apps() -> list[AppConfig]:
    print("Discovering GA4 properties accessible to the configured account...")
    properties = list_accessible_ga4_properties()
    print(f"Accessible GA4 properties found: {len(properties)}")

    firebase_app_index = build_accessible_firebase_android_app_index()
    print(f"Accessible Firebase Android app lookup keys found: {len(firebase_app_index)}")

    apps = []
    skipped_non_android = 0

    for property_summary in properties:
        property_id = property_summary.get("property_id", "")
        property_display_name = property_summary.get("property_display_name", "") or property_id

        try:
            streams = fetch_ga4_data_streams(property_id)
        except Exception as error:
            status, error_text = classify_api_error(error)
            print(f"GA4 DATA STREAM DISCOVERY {status} for {property_display_name} / {property_id}: {error_text}")
            continue

        android_streams = [stream for stream in streams if is_android_stream(stream)]

        if not android_streams:
            skipped_non_android += 1
            continue

        firebase_app = choose_firebase_app_for_ga4_streams(android_streams, firebase_app_index)
        app = build_auto_discovered_app_config(property_summary, android_streams, firebase_app)
        apps.append(app)

    if skipped_non_android:
        print(f"Skipped non-Android or streamless GA4 properties: {skipped_non_android}")

    if not apps:
        raise SystemExit(
            "No accessible Android GA4 app properties were found. "
            "Check that the service account has access to GA4 properties and Analytics Admin API is enabled."
        )

    return apps


def add_package_name_column(rows: list[list], package_name_lookup: dict[str, str]) -> list[list]:
    if not rows:
        return rows

    header = list(rows[0])

    if "Package Name" in header:
        return rows

    if "App Name" not in header or "Property ID" not in header:
        return rows

    app_name_col = header.index("App Name")
    property_id_col = header.index("Property ID")
    insert_col = app_name_col + 1

    updated_rows = []
    header.insert(insert_col, "Package Name")
    updated_rows.append(header)

    for row in rows[1:]:
        updated_row = list(row)
        app_name = ""
        property_id = ""

        if len(updated_row) > app_name_col:
            app_name = str(updated_row[app_name_col]).strip()

        if len(updated_row) > property_id_col:
            property_id = str(updated_row[property_id_col]).strip()

        package_name = package_name_lookup.get(
            f"{app_name}|{property_id}",
            package_name_lookup.get(property_id, ""),
        )

        while len(updated_row) < insert_col:
            updated_row.append("")

        updated_row.insert(insert_col, package_name)
        updated_rows.append(updated_row)

    return updated_rows


def write_report_sheet(
    sheet_name: str,
    rows: list[list],
    package_name_lookup: dict[str, str],
):
    write_sheet(
        sheet_name,
        add_package_name_column(rows, package_name_lookup),
    )


# =========================
# FUNNEL REPORT
# =========================


def funnel_event_filter(event_name: str) -> FunnelFilterExpression:
    return FunnelFilterExpression(
        funnel_event_filter=FunnelEventFilter(
            event_name=event_name
        )
    )


def funnel_contains_filter(field_name: str, value: str) -> FunnelFilterExpression:
    return FunnelFilterExpression(
        funnel_field_filter=FunnelFieldFilter(
            field_name=field_name,
            string_filter=StringFilter(
                match_type=StringFilter.MatchType.CONTAINS,
                value=value,
                case_sensitive=False,
            ),
        )
    )


def run_first_open_to_home_funnel(app: AppConfig):
    request = RunFunnelReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[
            AlphaDateRange(
                start_date=config.start_date,
                end_date=config.end_date,
            )
        ],
        funnel=Funnel(
            is_open_funnel=False,
            steps=[
                FunnelStep(
                    name="First Open",
                    filter_expression=funnel_event_filter("first_open"),
                ),
                FunnelStep(
                    name="Home Users",
                    filter_expression=FunnelFilterExpression(
                        and_group=FunnelFilterExpressionList(
                            expressions=[
                                funnel_event_filter("screen_view"),
                                funnel_contains_filter(
                                    app.screen_field,
                                    app.home_screen_name,
                                ),
                            ]
                        )
                    ),
                ),
            ],
        ),
    )

    return alpha_client.run_funnel_report(request)


def parse_funnel_rows(app: AppConfig, response):
    table = response.funnel_table

    dimension_headers = [header.name for header in table.dimension_headers]
    metric_headers = [header.name for header in table.metric_headers]

    updated_at = now_text()
    date_range_display = get_report_date_range_display()

    detail_rows = []

    first_open_users = 0
    home_users = 0
    first_open_abandonments = 0

    for row in table.rows:
        row_data = {}

        for index, dimension_value in enumerate(row.dimension_values):
            row_data[dimension_headers[index]] = dimension_value.value

        for index, metric_value in enumerate(row.metric_values):
            row_data[metric_headers[index]] = metric_value.value

        funnel_step = row_data.get("funnelStepName", "")

        if not funnel_step and dimension_headers:
            funnel_step = row.dimension_values[0].value

        active_users_raw = get_metric(row_data, ["activeUsers"], "0")

        completion_rate_raw = get_metric(
            row_data,
            [
                "completionRate",
                "funnelStepCompletionRate",
                "funnelCompletionRate",
            ],
            "",
        )

        abandonments_raw = get_metric(
            row_data,
            [
                "abandonments",
                "funnelStepAbandonments",
            ],
            "0",
        )

        abandonment_rate_raw = get_metric(
            row_data,
            [
                "abandonmentRate",
                "funnelStepAbandonmentRate",
            ],
            "",
        )

        active_users = to_number(active_users_raw)
        abandonments = to_number(abandonments_raw)

        if "First Open" in funnel_step:
            event_name = "first_open"
            screen_condition = ""
            first_open_users = int(active_users)
            first_open_abandonments = int(abandonments)

        elif "Home Users" in funnel_step:
            event_name = "screen_view"
            screen_condition = f"{app.screen_field} contains {app.home_screen_name}"
            home_users = int(active_users)

        else:
            event_name = ""
            screen_condition = ""

        detail_rows.append(
            [
                app.app_name,
                app.property_id,
                date_range_display,
                funnel_step,
                event_name,
                screen_condition,
                active_users,
                to_percent(completion_rate_raw),
                abandonments,
                to_percent(abandonment_rate_raw),
                "SUCCESS",
                "",
                updated_at,
            ]
        )

    if first_open_users > 0:
        conversion_rate = make_rate(home_users, first_open_users)
        drop_off = first_open_users - home_users
        abandonment_rate = make_rate(drop_off, first_open_users)
    else:
        conversion_rate = "0%"
        drop_off = 0
        abandonment_rate = "0%"

    if first_open_abandonments > 0:
        drop_off = first_open_abandonments

    if first_open_users == 0 and home_users == 0:
        status = "NO GA4 DATA"
        error_message = "GA4 property has no matching data in this date range."
    elif first_open_users > 0 and home_users == 0:
        status = "NO FUNNEL MATCH"
        error_message = (
            "first_open exists, but no users reached the selected home screen "
            "inside the closed funnel. Screen name may be correct, but funnel path has no match."
        )
    else:
        status = "SUCCESS"
        error_message = ""

    summary_row = [
        app.app_name,
        app.property_id,
        date_range_display,
        first_open_users,
        home_users,
        drop_off,
        conversion_rate,
        abandonment_rate,
        app.home_screen_name,
        app.screen_field,
        status,
        error_message,
        updated_at,
    ]

    return summary_row, detail_rows


# =========================
# USER + SESSION REPORT
# =========================


def run_user_session_report(app: AppConfig):
    request = RunReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[
            BetaDateRange(
                start_date=config.start_date,
                end_date=config.end_date,
            )
        ],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="newUsers"),
            Metric(name="sessions"),
            Metric(name="engagedSessions"),
            Metric(name="averageSessionDuration"),
            Metric(name="userEngagementDuration"),
            Metric(name="engagementRate"),
        ],
    )

    return beta_client.run_report(request)


def parse_user_session_report(response) -> dict:
    if not response.rows:
        return {
            "active_users": 0,
            "new_users": 0,
            "sessions": 0,
            "engaged_sessions": 0,
            "average_session_duration_seconds": 0,
            "average_session_duration": "0m 0s",
            "total_engagement_seconds": 0,
            "total_engagement_time": "0m 0s",
            "sessions_per_active_user": 0,
            "engagement_rate": "0%",
        }

    metric_headers = [header.name for header in response.metric_headers]
    row = response.rows[0]

    row_data = {}

    for index, metric_value in enumerate(row.metric_values):
        row_data[metric_headers[index]] = metric_value.value

    active_users = to_number(row_data.get("activeUsers", 0))
    new_users = to_number(row_data.get("newUsers", 0))
    sessions = to_number(row_data.get("sessions", 0))
    engaged_sessions = to_number(row_data.get("engagedSessions", 0))

    avg_session_seconds = to_float(row_data.get("averageSessionDuration", 0))
    total_engagement_seconds = to_float(row_data.get("userEngagementDuration", 0))

    if active_users > 0:
        sessions_per_active_user = round(sessions / active_users, 2)
    else:
        sessions_per_active_user = 0

    return {
        "active_users": active_users,
        "new_users": new_users,
        "sessions": sessions,
        "engaged_sessions": engaged_sessions,
        "average_session_duration_seconds": round(avg_session_seconds, 2),
        "average_session_duration": format_seconds(avg_session_seconds),
        "total_engagement_seconds": round(total_engagement_seconds, 2),
        "total_engagement_time": format_seconds(total_engagement_seconds),
        "sessions_per_active_user": sessions_per_active_user,
        "engagement_rate": to_percent(row_data.get("engagementRate", 0)),
    }


# =========================
# RETENTION REPORT
# =========================


def run_retention_report(app: AppConfig):
    cohort_start, cohort_end = get_retention_cohort_date_range()

    request = RunReportRequest(
        property=f"properties/{app.property_id}",
        dimensions=[
            Dimension(name="cohort"),
            Dimension(name="cohortNthDay"),
        ],
        metrics=[
            Metric(name="cohortActiveUsers"),
            Metric(name="cohortTotalUsers"),
        ],
        cohort_spec=CohortSpec(
            cohorts=[
                Cohort(
                    name="Acquired Users",
                    dimension="firstSessionDate",
                    date_range=BetaDateRange(
                        start_date=cohort_start,
                        end_date=cohort_end,
                    ),
                )
            ],
            cohorts_range=CohortsRange(
                granularity=CohortsRange.Granularity.DAILY,
                start_offset=0,
                end_offset=config.retention_days,
            ),
        ),
        keep_empty_rows=True,
    )

    return beta_client.run_report(request)


def parse_cohort_day(value: str) -> int:
    value = str(value).strip()

    if value == "":
        return 0

    try:
        return int(value)
    except ValueError:
        digits = re.sub(r"\D", "", value)

        if digits == "":
            return 0

        return int(digits)


def parse_retention_report(app: AppConfig, response):
    dimension_headers = [header.name for header in response.dimension_headers]
    metric_headers = [header.name for header in response.metric_headers]

    cohort_start, cohort_end = get_retention_cohort_date_range()
    cohort_date_range = f"{cohort_start} to {cohort_end}"
    report_date_range = get_report_date_range_display()
    updated_at = now_text()

    rows_by_day = {}

    for row in response.rows:
        row_data = {}

        for index, dimension_value in enumerate(row.dimension_values):
            row_data[dimension_headers[index]] = dimension_value.value

        for index, metric_value in enumerate(row.metric_values):
            row_data[metric_headers[index]] = metric_value.value

        cohort_name = row_data.get("cohort", "Acquired Users")
        day_number = parse_cohort_day(row_data.get("cohortNthDay", "0"))

        active_users = to_number(row_data.get("cohortActiveUsers", 0))
        total_users = to_number(row_data.get("cohortTotalUsers", 0))

        rows_by_day[day_number] = {
            "cohort_name": cohort_name,
            "active_users": active_users,
            "total_users": total_users,
            "retention_rate": make_rate(active_users, total_users),
        }

    detail_rows = []

    for day in range(0, config.retention_days + 1):
        data = rows_by_day.get(
            day,
            {
                "cohort_name": "Acquired Users",
                "active_users": 0,
                "total_users": 0,
                "retention_rate": "0%",
            },
        )

        detail_rows.append(
            [
                app.app_name,
                app.property_id,
                report_date_range,
                cohort_date_range,
                data["cohort_name"],
                f"D{day}",
                day,
                data["active_users"],
                data["total_users"],
                data["retention_rate"],
                "SUCCESS",
                "",
                updated_at,
            ]
        )

    cohort_total_users = 0

    for data in rows_by_day.values():
        if data["total_users"] > cohort_total_users:
            cohort_total_users = data["total_users"]

    d1_data = rows_by_day.get(
        1,
        {
            "active_users": 0,
            "retention_rate": "0%",
        },
    )

    d7_data = rows_by_day.get(
        7,
        {
            "active_users": 0,
            "retention_rate": "0%",
        },
    )

    summary = {
        "cohort_date_range": cohort_date_range,
        "cohort_total_users": cohort_total_users,
        "d1_active_users": d1_data["active_users"],
        "d1_retention": d1_data["retention_rate"],
        "d7_active_users": d7_data["active_users"],
        "d7_retention": d7_data["retention_rate"],
    }

    return summary, detail_rows


def empty_session_data() -> dict:
    return {
        "active_users": "",
        "new_users": "",
        "sessions": "",
        "engaged_sessions": "",
        "average_session_duration_seconds": "",
        "average_session_duration": "",
        "total_engagement_seconds": "",
        "total_engagement_time": "",
        "sessions_per_active_user": "",
        "engagement_rate": "",
    }


def empty_retention_summary() -> dict:
    cohort_start, cohort_end = get_retention_cohort_date_range()

    return {
        "cohort_date_range": f"{cohort_start} to {cohort_end}",
        "cohort_total_users": "",
        "d1_active_users": "",
        "d1_retention": "",
        "d7_active_users": "",
        "d7_retention": "",
    }


def append_error_retention_detail(
    retention_details_rows: list[list],
    app: AppConfig,
    report_date_range: str,
    retention_summary: dict,
    status: str,
    error_text: str,
):
    retention_details_rows.append(
        [
            app.app_name,
            app.property_id,
            report_date_range,
            retention_summary["cohort_date_range"],
            "",
            "",
            "",
            "",
            "",
            "",
            status,
            error_text,
            now_text(),
        ]
    )


# =========================
# AUDIENCE SEGMENTS REPORT
# =========================


def beta_exact_filter(field_name: str, value: str) -> BetaFilterExpression:
    return BetaFilterExpression(
        filter=BetaFilter(
            field_name=field_name,
            string_filter=BetaFilter.StringFilter(
                match_type=BetaFilter.StringFilter.MatchType.EXACT,
                value=value,
                case_sensitive=False,
            ),
        )
    )


def beta_or_filter(expressions: list[BetaFilterExpression]) -> BetaFilterExpression:
    return BetaFilterExpression(
        or_group=BetaFilterExpressionList(
            expressions=expressions
        )
    )


def get_audience_segments():
    paid_channel_groups = [
        "Paid Search",
        "Paid Social",
        "Paid Video",
        "Paid Shopping",
        "Cross-network",
        "Display",
        "Paid Other",
    ]

    return [
        {
            "name": "All Users",
            "rule": "No filter",
            "filter": None,
        },
        {
            "name": "US Users",
            "rule": "country = United States",
            "filter": beta_exact_filter("country", "United States"),
        },
        {
            "name": "Direct Traffic",
            "rule": "sessionDefaultChannelGroup = Direct",
            "filter": beta_exact_filter("sessionDefaultChannelGroup", "Direct"),
        },
        {
            "name": "Paid Traffic",
            "rule": "sessionDefaultChannelGroup in paid channel groups",
            "filter": beta_or_filter(
                [
                    beta_exact_filter("sessionDefaultChannelGroup", channel)
                    for channel in paid_channel_groups
                ]
            ),
        },
        {
            "name": "Mobile Traffic",
            "rule": "deviceCategory = mobile",
            "filter": beta_exact_filter("deviceCategory", "mobile"),
        },
        {
            "name": "Tablet Traffic",
            "rule": "deviceCategory = tablet",
            "filter": beta_exact_filter("deviceCategory", "tablet"),
        },
    ]


def run_audience_segment_report(app: AppConfig, segment_filter):
    request_params = {
        "property": f"properties/{app.property_id}",
        "date_ranges": [
            BetaDateRange(
                start_date=config.start_date,
                end_date=config.end_date,
            )
        ],
        "metrics": [
            Metric(name="activeUsers"),
            Metric(name="newUsers"),
            Metric(name="sessions"),
            Metric(name="engagedSessions"),
            Metric(name="averageSessionDuration"),
            Metric(name="userEngagementDuration"),
            Metric(name="engagementRate"),
        ],
    }

    if segment_filter is not None:
        request_params["dimension_filter"] = segment_filter

    request = RunReportRequest(**request_params)

    return beta_client.run_report(request)


def build_audience_segment_rows_for_app(app: AppConfig) -> list[list]:
    rows = []
    report_date_range = get_report_date_range_display()

    for segment in get_audience_segments():
        segment_name = segment["name"]
        segment_rule = segment["rule"]

        try:
            response = run_audience_segment_report(
                app=app,
                segment_filter=segment["filter"],
            )

            data = parse_user_session_report(response)

            if data["active_users"] == 0:
                status = "NO SEGMENT DATA"
                error_text = "No users found for this segment in selected date range."
            else:
                status = "SUCCESS"
                error_text = ""

            rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    segment_name,
                    segment_rule,
                    data["active_users"],
                    data["new_users"],
                    data["sessions"],
                    data["engaged_sessions"],
                    data["average_session_duration_seconds"],
                    data["average_session_duration"],
                    data["total_engagement_seconds"],
                    data["total_engagement_time"],
                    data["sessions_per_active_user"],
                    data["engagement_rate"],
                    status,
                    error_text,
                    now_text(),
                ]
            )

        except Exception as error:
            status, error_text = classify_api_error(error)

            rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    segment_name,
                    segment_rule,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    status,
                    error_text,
                    now_text(),
                ]
            )

    return rows


# =========================
# PERSONALIZED USER EXPERIENCE REPORT
# =========================


def parse_session_metric_values(metric_headers: list[str], metric_values) -> dict:
    row_data = {}

    for index, metric_value in enumerate(metric_values):
        row_data[metric_headers[index]] = metric_value.value

    active_users = to_number(row_data.get("activeUsers", 0))
    new_users = to_number(row_data.get("newUsers", 0))
    sessions = to_number(row_data.get("sessions", 0))
    engaged_sessions = to_number(row_data.get("engagedSessions", 0))

    avg_session_seconds = to_float(row_data.get("averageSessionDuration", 0))
    total_engagement_seconds = to_float(row_data.get("userEngagementDuration", 0))

    if active_users > 0:
        sessions_per_active_user = round(sessions / active_users, 2)
    else:
        sessions_per_active_user = 0

    return {
        "active_users": active_users,
        "new_users": new_users,
        "sessions": sessions,
        "engaged_sessions": engaged_sessions,
        "average_session_duration_seconds": round(avg_session_seconds, 2),
        "average_session_duration": format_seconds(avg_session_seconds),
        "total_engagement_seconds": round(total_engagement_seconds, 2),
        "total_engagement_time": format_seconds(total_engagement_seconds),
        "sessions_per_active_user": sessions_per_active_user,
        "engagement_rate": to_percent(row_data.get("engagementRate", 0)),
    }


def run_dimension_session_report(app: AppConfig, dimension_name: str, limit: int):
    request = RunReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[
            BetaDateRange(
                start_date=config.start_date,
                end_date=config.end_date,
            )
        ],
        dimensions=[
            Dimension(name=dimension_name),
        ],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="newUsers"),
            Metric(name="sessions"),
            Metric(name="engagedSessions"),
            Metric(name="averageSessionDuration"),
            Metric(name="userEngagementDuration"),
            Metric(name="engagementRate"),
        ],
        order_bys=[
            OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="activeUsers"),
                desc=True,
            )
        ],
        limit=limit,
    )

    return beta_client.run_report(request)


def get_personalized_ux_dimensions() -> list[dict]:
    return [
        {
            "breakdown": "Country",
            "dimension": "country",
            "recommendation_type": "country",
        },
        {
            "breakdown": "Language",
            "dimension": "language",
            "recommendation_type": "language",
        },
        {
            "breakdown": "Device Category",
            "dimension": "deviceCategory",
            "recommendation_type": "device",
        },
        {
            "breakdown": "Operating System",
            "dimension": "operatingSystem",
            "recommendation_type": "os",
        },
        {
            "breakdown": "App Version",
            "dimension": "appVersion",
            "recommendation_type": "app_version",
        },
        {
            "breakdown": "First User Medium",
            "dimension": "firstUserMedium",
            "recommendation_type": "traffic",
        },
        {
            "breakdown": "Top Screens / Screen Class",
            "dimension": "unifiedPagePathScreen",
            "recommendation_type": "screen",
        },
    ]


def build_personalized_recommendation(
    recommendation_type: str,
    dimension_value: str,
    data: dict,
) -> str:
    active_users = data.get("active_users", 0)
    engagement_rate = data.get("engagement_rate", "")
    avg_session = data.get("average_session_duration", "")

    if active_users == 0:
        return "No meaningful traffic for this segment in the selected date range."

    value = dimension_value or "(not set)"

    if recommendation_type == "country":
        return (
            f"Personalize store creatives, language, and offers for {value}. "
            f"Review retention and engagement for this country."
        )

    if recommendation_type == "language":
        return (
            f"Localize onboarding, paywall, and core UI copy for language {value}. "
            "Prioritize this language if users and engagement are strong."
        )

    if recommendation_type == "device":
        return (
            f"Optimize layout and performance for {value} users. "
            "Check screenshots, button sizes, loading time, and ad placement."
        )

    if recommendation_type == "os":
        return (
            f"Review OS-specific crashes, permissions, and UX issues for {value}. "
            "Compare session duration and engagement against other OS values."
        )

    if recommendation_type == "app_version":
        return (
            f"Monitor app version {value}. If engagement or retention is low, "
            "review release changes, bugs, onboarding, and remote config rules."
        )

    if recommendation_type == "traffic":
        return (
            f"Traffic medium {value} should be compared with paid/organic quality. "
            "Optimize campaigns if sessions per user or engagement rate is weak."
        )

    if recommendation_type == "screen":
        return (
            f"Screen {value} is important for user experience. "
            f"Check UX, loading, exits, and feature usage. Avg session: {avg_session}, engagement: {engagement_rate}."
        )

    return "Review this segment for personalization opportunities."


def build_personalized_ux_rows_for_app(app: AppConfig) -> list[list]:
    rows = []
    report_date_range = get_report_date_range_display()
    top_n = getattr(config, "personalized_top_n", 10)

    for item in get_personalized_ux_dimensions():
        breakdown = item["breakdown"]
        dimension_name = item["dimension"]
        recommendation_type = item["recommendation_type"]

        try:
            response = run_dimension_session_report(
                app=app,
                dimension_name=dimension_name,
                limit=top_n,
            )

            if not response.rows:
                rows.append(
                    [
                        app.app_name,
                        app.property_id,
                        report_date_range,
                        breakdown,
                        dimension_name,
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "NO UX DATA",
                        "No rows returned for this personalization breakdown.",
                        now_text(),
                    ]
                )
                continue

            metric_headers = [header.name for header in response.metric_headers]

            for row in response.rows:
                dimension_value = row.dimension_values[0].value if row.dimension_values else ""
                data = parse_session_metric_values(metric_headers, row.metric_values)
                recommendation = build_personalized_recommendation(
                    recommendation_type=recommendation_type,
                    dimension_value=dimension_value,
                    data=data,
                )

                rows.append(
                    [
                        app.app_name,
                        app.property_id,
                        report_date_range,
                        breakdown,
                        dimension_name,
                        dimension_value,
                        data["active_users"],
                        data["new_users"],
                        data["sessions"],
                        data["engaged_sessions"],
                        data["average_session_duration_seconds"],
                        data["average_session_duration"],
                        data["total_engagement_seconds"],
                        data["total_engagement_time"],
                        data["sessions_per_active_user"],
                        data["engagement_rate"],
                        recommendation,
                        "SUCCESS",
                        "",
                        now_text(),
                    ]
                )

        except Exception as error:
            status, error_text = classify_api_error(error)
            rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    breakdown,
                    dimension_name,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    status,
                    error_text,
                    now_text(),
                ]
            )

    return rows


# =========================
# REMOTE CONFIGURATION REPORT
# =========================


def beta_contains_filter(field_name: str, value: str) -> BetaFilterExpression:
    return BetaFilterExpression(
        filter=BetaFilter(
            field_name=field_name,
            string_filter=BetaFilter.StringFilter(
                match_type=BetaFilter.StringFilter.MatchType.CONTAINS,
                value=value,
                case_sensitive=False,
            ),
        )
    )


def get_remote_config_event_filter() -> BetaFilterExpression:
    remote_config_keywords = [
        "remote_config",
        "remote config",
        "config",
        "experiment",
        "variant",
        "feature_flag",
        "featureflag",
        "ab_test",
        "abtest",
        "firebase_exp",
        "rc_",
    ]

    return beta_or_filter(
        [
            beta_contains_filter("eventName", keyword)
            for keyword in remote_config_keywords
        ]
    )


def run_remote_config_events_report(app: AppConfig):
    limit = getattr(config, "remote_config_event_limit", 25)

    request = RunReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[
            BetaDateRange(
                start_date=config.start_date,
                end_date=config.end_date,
            )
        ],
        dimensions=[
            Dimension(name="eventName"),
        ],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="eventCount"),
        ],
        dimension_filter=get_remote_config_event_filter(),
        order_bys=[
            OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="eventCount"),
                desc=True,
            )
        ],
        limit=limit,
    )

    return beta_client.run_report(request)


def run_remote_config_app_version_report(app: AppConfig):
    limit = getattr(config, "remote_config_app_version_limit", 10)

    request = RunReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[
            BetaDateRange(
                start_date=config.start_date,
                end_date=config.end_date,
            )
        ],
        dimensions=[
            Dimension(name="appVersion"),
        ],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="newUsers"),
            Metric(name="sessions"),
            Metric(name="engagedSessions"),
            Metric(name="averageSessionDuration"),
            Metric(name="userEngagementDuration"),
            Metric(name="engagementRate"),
        ],
        order_bys=[
            OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="activeUsers"),
                desc=True,
            )
        ],
        limit=limit,
    )

    return beta_client.run_report(request)


def get_remote_event_type(event_name: str) -> str:
    event_lower = str(event_name).lower()

    if "experiment" in event_lower or "firebase_exp" in event_lower or "ab" in event_lower:
        return "Experiment / A-B Test"

    if "variant" in event_lower:
        return "Variant"

    if "feature" in event_lower or "flag" in event_lower:
        return "Feature Flag"

    if "config" in event_lower or event_lower.startswith("rc_"):
        return "Remote Config"

    return "Config Related Event"


def build_remote_config_recommendation(row_type: str, value: str, data: dict | None = None) -> str:
    if row_type == "Remote Config Event":
        return (
            f"Event {value} is being logged. Compare users and event count with app versions, "
            "retention, and funnel conversion to understand config impact."
        )

    if row_type == "App Version Impact":
        engagement = data.get("engagement_rate", "") if data else ""
        avg_session = data.get("average_session_duration", "") if data else ""
        return (
            f"Use app version {value} as a Remote Config impact check. "
            f"Compare engagement {engagement} and avg session {avg_session} against other versions."
        )

    return "Review this row for Remote Config impact."


def build_remote_config_rows_for_app(app: AppConfig) -> list[list]:
    rows = []
    report_date_range = get_report_date_range_display()

    # 1) Remote Config / experiment / variant event signals.
    try:
        response = run_remote_config_events_report(app)
        metric_headers = [header.name for header in response.metric_headers]

        if not response.rows:
            rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    "Remote Config Event",
                    "eventName contains remote/config/experiment/variant/feature flag keywords",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "NO REMOTE CONFIG EVENTS",
                    "No remote config / experiment / variant events found in GA4 for this date range. Log custom events or register custom dimensions if you need deeper tracking.",
                    now_text(),
                ]
            )
        else:
            for row in response.rows:
                event_name = row.dimension_values[0].value if row.dimension_values else ""
                row_data = {}

                for index, metric_value in enumerate(row.metric_values):
                    row_data[metric_headers[index]] = metric_value.value

                active_users = to_number(row_data.get("activeUsers", 0))
                event_count = to_number(row_data.get("eventCount", 0))

                rows.append(
                    [
                        app.app_name,
                        app.property_id,
                        report_date_range,
                        "Remote Config Event",
                        get_remote_event_type(event_name),
                        event_name,
                        active_users,
                        "",
                        "",
                        "",
                        event_count,
                        "SUCCESS",
                        build_remote_config_recommendation("Remote Config Event", event_name),
                        now_text(),
                    ]
                )

    except Exception as error:
        status, error_text = classify_api_error(error)
        rows.append(
            [
                app.app_name,
                app.property_id,
                report_date_range,
                "Remote Config Event",
                "eventName contains remote/config/experiment/variant/feature flag keywords",
                "",
                "",
                "",
                "",
                "",
                "",
                status,
                error_text,
                now_text(),
            ]
        )

    # 2) App version impact rows. Useful when configs are rolled out by app version.
    try:
        response = run_remote_config_app_version_report(app)
        metric_headers = [header.name for header in response.metric_headers]

        if not response.rows:
            rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    "App Version Impact",
                    "appVersion breakdown",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "NO VERSION DATA",
                    "No app version rows found for selected date range.",
                    now_text(),
                ]
            )
        else:
            for row in response.rows:
                app_version = row.dimension_values[0].value if row.dimension_values else ""
                data = parse_session_metric_values(metric_headers, row.metric_values)

                rows.append(
                    [
                        app.app_name,
                        app.property_id,
                        report_date_range,
                        "App Version Impact",
                        "appVersion breakdown",
                        app_version,
                        data["active_users"],
                        data["new_users"],
                        data["sessions"],
                        data["average_session_duration"],
                        "",
                        "SUCCESS",
                        build_remote_config_recommendation("App Version Impact", app_version, data),
                        now_text(),
                    ]
                )

    except Exception as error:
        status, error_text = classify_api_error(error)
        rows.append(
            [
                app.app_name,
                app.property_id,
                report_date_range,
                "App Version Impact",
                "appVersion breakdown",
                "",
                "",
                "",
                "",
                "",
                "",
                status,
                error_text,
                now_text(),
            ]
        )

    return rows



# =========================
# FIREBASE A/B TEST - TIME CAPPING
# =========================


def get_remote_config_session():
    global remote_config_session

    if remote_config_session is None:
        remote_config_session = AuthorizedSession(credentials)

    return remote_config_session


def get_firebase_remote_config_template(firebase_project_id: str) -> tuple[dict, str]:
    project_id = str(firebase_project_id).strip()

    if not project_id:
        raise ValueError("Firebase Project ID could not be auto-resolved for this app.")

    project_path = f"projects/{project_id}"
    url = f"{config.firebase_remote_config_api_base}/{project_path}/remoteConfig"
    params = {}

    namespace = str(config.remote_config_namespace).strip()
    if namespace:
        params["name"] = f"{project_path}/namespaces/{namespace}/remoteConfig"

    response = get_remote_config_session().get(
        url,
        params=params,
        headers={"Accept-Encoding": "gzip"},
        timeout=config.firebase_remote_config_timeout,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Firebase Remote Config API error {response.status_code}: {response.text}"
        )

    return response.json(), response.headers.get("ETag", "")


def find_remote_config_parameter(template: dict, parameter_key: str) -> tuple[dict | None, str, str]:
    key = str(parameter_key).strip()

    if not key:
        return None, "", ""

    parameters = template.get("parameters", {}) or {}

    if key in parameters:
        return parameters[key], "", key

    parameter_groups = template.get("parameterGroups", {}) or {}

    for group_name, group_data in parameter_groups.items():
        group_parameters = group_data.get("parameters", {}) or {}
        if key in group_parameters:
            return group_parameters[key], group_name, key

    # Helpful fallback: find capping-like parameters if the exact key is not found.
    key_lower = key.lower()
    for candidate_key, parameter in parameters.items():
        if key_lower in candidate_key.lower() or candidate_key.lower() in key_lower:
            return parameter, "", candidate_key

    for group_name, group_data in parameter_groups.items():
        group_parameters = group_data.get("parameters", {}) or {}
        for candidate_key, parameter in group_parameters.items():
            if key_lower in candidate_key.lower() or candidate_key.lower() in key_lower:
                return parameter, group_name, candidate_key

    return None, "", ""


def get_condition_lookup(template: dict) -> tuple[dict, dict]:
    condition_lookup = {}
    condition_priority = {}

    for index, condition in enumerate(template.get("conditions", []) or [], start=1):
        name = condition.get("name", "")
        if not name:
            continue

        condition_lookup[name] = condition
        condition_priority[name] = index

    return condition_lookup, condition_priority


def format_remote_config_value(value_object: dict | None) -> str:
    if not value_object:
        return ""

    if "value" in value_object:
        return str(value_object.get("value", ""))

    if value_object.get("useInAppDefault") is True:
        return "Use in-app default"

    if "personalizationValue" in value_object:
        personalization = value_object.get("personalizationValue", {}) or {}
        personalization_id = personalization.get("personalizationId", "")
        return f"Personalization value: {personalization_id}" if personalization_id else "Personalization value"

    if "experimentValue" in value_object:
        experiment = value_object.get("experimentValue", {}) or {}
        experiment_id = experiment.get("experimentId", "")
        variants = []

        for variant in experiment.get("variantValue", []) or []:
            variant_id = variant.get("variantId", "")
            if "value" in variant:
                variant_value = variant.get("value", "")
            elif variant.get("noChange") is True:
                variant_value = "No change"
            else:
                variant_value = ""

            variants.append(f"{variant_id}: {variant_value}" if variant_id else str(variant_value))

        prefix = f"Experiment {experiment_id}" if experiment_id else "Experiment value"
        return f"{prefix} | " + "; ".join(variants) if variants else prefix

    if "rolloutValue" in value_object:
        rollout = value_object.get("rolloutValue", {}) or {}
        rollout_id = rollout.get("rolloutId", "")
        rollout_value = rollout.get("value", "")
        rollout_percent = rollout.get("percent", "")
        return f"Rollout {rollout_id}: {rollout_value} to {rollout_percent}%"

    return json.dumps(value_object, ensure_ascii=False)


def extract_experiment_variant_rows(value_object: dict | None) -> list[dict]:
    if not value_object or "experimentValue" not in value_object:
        return []

    experiment = value_object.get("experimentValue", {}) or {}
    experiment_id = experiment.get("experimentId", "")
    rows = []

    for variant in experiment.get("variantValue", []) or []:
        if "value" in variant:
            variant_value = variant.get("value", "")
        elif variant.get("noChange") is True:
            variant_value = "No change"
        else:
            variant_value = ""

        rows.append(
            {
                "experiment_id": experiment_id,
                "variant_id": variant.get("variantId", ""),
                "value": variant_value,
            }
        )

    return rows


def build_time_capping_recommendation(
    value_source: str,
    parameter_key: str,
    value: str,
    experiment_id: str = "",
) -> str:
    if experiment_id:
        return (
            f"A/B Testing value detected for {parameter_key}. Compare this experiment ID "
            f"({experiment_id}) with GA4 funnel, retention, ARPU/ad revenue, and engagement."
        )

    if value_source == "Default value":
        return (
            f"Current default time capping for {parameter_key} is {value}. "
            "Use this as the control value when comparing new capping tests."
        )

    if value_source == "Conditional value":
        return (
            f"Conditional time capping value found for {parameter_key}. "
            "Check the condition audience before comparing performance."
        )

    return "Review this Remote Config time capping row."


def build_time_capping_ab_rows_for_app(app: AppConfig) -> list[list]:
    rows = []
    report_date_range = get_report_date_range_display()
    updated_at = now_text()
    parameter_key = app.time_capping_parameter or config.time_capping_parameter

    base_prefix = [
        app.app_name,
        app.property_id,
        app.firebase_project_id,
        app.firebase_project_name,
        app.firebase_app_id,
        report_date_range,
        parameter_key,
    ]

    if not app.firebase_project_id:
        rows.append(
            base_prefix
            + [
                "",
                "A/B Test on Time Capping",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "MISSING FIREBASE PROJECT ID",
                "Add Firebase Project ID in Apps Config column F.",
                updated_at,
            ]
        )
        return rows

    try:
        template, etag = get_firebase_remote_config_template(app.firebase_project_id)
        condition_lookup, condition_priority = get_condition_lookup(template)
        parameter, group_name, matched_key = find_remote_config_parameter(template, parameter_key)
        version = template.get("version", {}) or {}

        version_number = version.get("versionNumber", "")
        update_time = version.get("updateTime", "")
        update_user = (version.get("updateUser", {}) or {}).get("email", "")

        if parameter is None:
            rows.append(
                base_prefix
                + [
                    "",
                    "A/B Test on Time Capping",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    version_number,
                    update_time,
                    update_user,
                    "PARAMETER NOT FOUND",
                    f"Parameter {parameter_key} was not found in Firebase Remote Config.",
                    updated_at,
                ]
            )
            return rows

        value_type = parameter.get("valueType", "STRING")
        default_value_object = parameter.get("defaultValue")
        default_value = format_remote_config_value(default_value_object)

        rows.append(
            [
                app.app_name,
                app.property_id,
                app.firebase_project_id,
                app.firebase_project_name,
                app.firebase_app_id,
                report_date_range,
                matched_key,
                group_name,
                "Default value",
                "Default",
                default_value,
                value_type,
                "",
                "",
                "",
                "",
                version_number,
                update_time,
                update_user,
                "SUCCESS",
                build_time_capping_recommendation("Default value", matched_key, default_value),
                updated_at,
            ]
        )

        for experiment_row in extract_experiment_variant_rows(default_value_object):
            rows.append(
                [
                    app.app_name,
                    app.property_id,
                    app.firebase_project_id,
                    app.firebase_project_name,
                    app.firebase_app_id,
                    report_date_range,
                    matched_key,
                    group_name,
                    "A/B Testing experiment value",
                    "Default experiment",
                    experiment_row["value"],
                    value_type,
                    "",
                    experiment_row["experiment_id"],
                    experiment_row["variant_id"],
                    "",
                    version_number,
                    update_time,
                    update_user,
                    "SUCCESS",
                    build_time_capping_recommendation(
                        "A/B Testing experiment value",
                        matched_key,
                        experiment_row["value"],
                        experiment_row["experiment_id"],
                    ),
                    updated_at,
                ]
            )

        conditional_values = parameter.get("conditionalValues", {}) or {}

        for condition_name, value_object in conditional_values.items():
            condition = condition_lookup.get(condition_name, {}) or {}
            condition_expression = condition.get("expression", "")
            condition_value = format_remote_config_value(value_object)
            priority = condition_priority.get(condition_name, "")

            rows.append(
                [
                    app.app_name,
                    app.property_id,
                    app.firebase_project_id,
                    app.firebase_project_name,
                    app.firebase_app_id,
                    report_date_range,
                    matched_key,
                    group_name,
                    "Conditional value",
                    condition_name,
                    condition_value,
                    value_type,
                    priority,
                    condition_expression,
                    "",
                    "",
                    version_number,
                    update_time,
                    update_user,
                    "SUCCESS",
                    build_time_capping_recommendation("Conditional value", matched_key, condition_value),
                    updated_at,
                ]
            )

            for experiment_row in extract_experiment_variant_rows(value_object):
                rows.append(
                    [
                        app.app_name,
                        app.property_id,
                        app.firebase_project_id,
                        app.firebase_project_name,
                        app.firebase_app_id,
                        report_date_range,
                        matched_key,
                        group_name,
                        "A/B Testing experiment value",
                        condition_name,
                        experiment_row["value"],
                        value_type,
                        priority,
                        condition_expression,
                        experiment_row["experiment_id"],
                        experiment_row["variant_id"],
                        version_number,
                        update_time,
                        update_user,
                        "SUCCESS",
                        build_time_capping_recommendation(
                            "A/B Testing experiment value",
                            matched_key,
                            experiment_row["value"],
                            experiment_row["experiment_id"],
                        ),
                        updated_at,
                    ]
                )

    except Exception as error:
        status, error_text = classify_api_error(error)
        rows.append(
            base_prefix
            + [
                "",
                "A/B Test on Time Capping",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                status,
                error_text,
                updated_at,
            ]
        )

    return rows




# =========================
# FIREBASE A/B TEST - IAP SCREEN / PAYWALL
# =========================


def get_iap_screen_keywords() -> list[str]:
    keywords = split_csv(getattr(config, "iap_screen_parameter_keywords", ""))
    parameter_key = str(getattr(config, "iap_screen_parameter", "") or "").strip()

    if parameter_key:
        keywords.insert(0, parameter_key)

    normalized = []
    for keyword in keywords:
        keyword = str(keyword).strip().lower()
        if keyword and keyword not in normalized:
            normalized.append(keyword)

    return normalized


def remote_config_key_matches_keywords(parameter_key: str, keywords: list[str]) -> bool:
    key_lower = str(parameter_key or "").lower()
    return any(keyword and keyword in key_lower for keyword in keywords)


def find_iap_screen_remote_config_parameters(
    template: dict,
    parameter_key: str,
) -> list[dict]:
    matches = []
    seen = set()

    explicit_key = str(parameter_key or "").strip()
    if explicit_key:
        parameter, group_name, matched_key = find_remote_config_parameter(template, explicit_key)
        if parameter is not None and matched_key:
            matches.append(
                {
                    "parameter_key": matched_key,
                    "parameter": parameter,
                    "group_name": group_name,
                    "match_rule": f"Exact / fallback match for {explicit_key}",
                }
            )
            seen.add((group_name, matched_key))

    keywords = get_iap_screen_keywords()
    if explicit_key:
        explicit_lower = explicit_key.lower()
        if explicit_lower not in keywords:
            keywords.insert(0, explicit_lower)

    for candidate_key, parameter, group_name in iter_remote_config_parameters(template):
        unique_key = (group_name, candidate_key)
        if unique_key in seen:
            continue

        if remote_config_key_matches_keywords(candidate_key, keywords):
            matches.append(
                {
                    "parameter_key": candidate_key,
                    "parameter": parameter,
                    "group_name": group_name,
                    "match_rule": "IAP/paywall keyword match",
                }
            )
            seen.add(unique_key)

    return matches


def build_iap_screen_recommendation(
    value_source: str,
    parameter_key: str,
    value: str,
    experiment_id: str = "",
) -> str:
    if experiment_id:
        return (
            f"IAP/paywall screen A/B value detected for {parameter_key}. Compare experiment ID "
            f"({experiment_id}) with paywall views, purchase starts, purchases, trial starts, "
            "ARPU/LTV, retention, and refund or cancellation signals."
        )

    if value_source == "Default value":
        return (
            f"Current default IAP/paywall screen value for {parameter_key} is {value}. "
            "Use this as the control value when comparing new IAP screen tests."
        )

    if value_source == "Conditional value":
        return (
            f"Conditional IAP/paywall screen value found for {parameter_key}. "
            "Check the condition audience before comparing purchase performance."
        )

    return "Review this Remote Config IAP/paywall screen row."


def append_iap_screen_parameter_rows(
    rows: list[list],
    app: AppConfig,
    report_date_range: str,
    parameter_key: str,
    parameter: dict,
    group_name: str,
    condition_lookup: dict,
    condition_priority: dict,
    version_number: str,
    update_time: str,
    update_user: str,
    updated_at: str,
):
    value_type = parameter.get("valueType", "STRING")
    default_value_object = parameter.get("defaultValue")
    default_value = format_remote_config_value(default_value_object)

    rows.append(
        [
            app.app_name,
            app.property_id,
            app.firebase_project_id,
            app.firebase_project_name,
            app.firebase_app_id,
            report_date_range,
            parameter_key,
            group_name,
            "Default value",
            "Default",
            default_value,
            value_type,
            "",
            "",
            "",
            "",
            version_number,
            update_time,
            update_user,
            "SUCCESS",
            build_iap_screen_recommendation("Default value", parameter_key, default_value),
            updated_at,
        ]
    )

    for experiment_row in extract_experiment_variant_rows(default_value_object):
        rows.append(
            [
                app.app_name,
                app.property_id,
                app.firebase_project_id,
                app.firebase_project_name,
                app.firebase_app_id,
                report_date_range,
                parameter_key,
                group_name,
                "A/B Testing experiment value",
                "Default experiment",
                experiment_row["value"],
                value_type,
                "",
                "",
                experiment_row["experiment_id"],
                experiment_row["variant_id"],
                version_number,
                update_time,
                update_user,
                "SUCCESS",
                build_iap_screen_recommendation(
                    "A/B Testing experiment value",
                    parameter_key,
                    experiment_row["value"],
                    experiment_row["experiment_id"],
                ),
                updated_at,
            ]
        )

    conditional_values = parameter.get("conditionalValues", {}) or {}

    for condition_name, value_object in conditional_values.items():
        condition = condition_lookup.get(condition_name, {}) or {}
        condition_expression = condition.get("expression", "")
        condition_value = format_remote_config_value(value_object)
        priority = condition_priority.get(condition_name, "")

        rows.append(
            [
                app.app_name,
                app.property_id,
                app.firebase_project_id,
                app.firebase_project_name,
                app.firebase_app_id,
                report_date_range,
                parameter_key,
                group_name,
                "Conditional value",
                condition_name,
                condition_value,
                value_type,
                priority,
                condition_expression,
                "",
                "",
                version_number,
                update_time,
                update_user,
                "SUCCESS",
                build_iap_screen_recommendation("Conditional value", parameter_key, condition_value),
                updated_at,
            ]
        )

        for experiment_row in extract_experiment_variant_rows(value_object):
            rows.append(
                [
                    app.app_name,
                    app.property_id,
                    app.firebase_project_id,
                    app.firebase_project_name,
                    app.firebase_app_id,
                    report_date_range,
                    parameter_key,
                    group_name,
                    "A/B Testing experiment value",
                    condition_name,
                    experiment_row["value"],
                    value_type,
                    priority,
                    condition_expression,
                    experiment_row["experiment_id"],
                    experiment_row["variant_id"],
                    version_number,
                    update_time,
                    update_user,
                    "SUCCESS",
                    build_iap_screen_recommendation(
                        "A/B Testing experiment value",
                        parameter_key,
                        experiment_row["value"],
                        experiment_row["experiment_id"],
                    ),
                    updated_at,
                ]
            )


def build_iap_screen_ab_rows_for_app(app: AppConfig) -> list[list]:
    rows = []
    report_date_range = get_report_date_range_display()
    updated_at = now_text()
    parameter_key = app.iap_screen_parameter or config.iap_screen_parameter

    base_prefix = [
        app.app_name,
        app.property_id,
        app.firebase_project_id,
        app.firebase_project_name,
        app.firebase_app_id,
        report_date_range,
        parameter_key,
    ]

    if not app.firebase_project_id:
        rows.append(
            base_prefix
            + [
                "",
                "A/B Test on IAPs Screen",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "MISSING FIREBASE PROJECT ID",
                "Add Firebase Project ID in Apps Config column F.",
                updated_at,
            ]
        )
        return rows

    try:
        template, etag = get_firebase_remote_config_template(app.firebase_project_id)
        condition_lookup, condition_priority = get_condition_lookup(template)
        version = template.get("version", {}) or {}
        version_number = version.get("versionNumber", "")
        update_time = version.get("updateTime", "")
        update_user = (version.get("updateUser", {}) or {}).get("email", "")

        matches = find_iap_screen_remote_config_parameters(template, parameter_key)

        if not matches:
            keywords_used = ", ".join(get_iap_screen_keywords())
            rows.append(
                base_prefix
                + [
                    "",
                    "A/B Test on IAPs Screen",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    version_number,
                    update_time,
                    update_user,
                    "NO IAP SCREEN CONFIG FOUND",
                    (
                        f"Parameter {parameter_key} was not found in Firebase Remote Config, "
                        f"and no parameter matched IAP/paywall keywords: {keywords_used}. "
                        "Add the exact key in Apps Config column K if your app uses a different Remote Config key."
                    ),
                    updated_at,
                ]
            )
            return rows

        for match in matches:
            append_iap_screen_parameter_rows(
                rows=rows,
                app=app,
                report_date_range=report_date_range,
                parameter_key=match["parameter_key"],
                parameter=match["parameter"],
                group_name=match["group_name"],
                condition_lookup=condition_lookup,
                condition_priority=condition_priority,
                version_number=version_number,
                update_time=update_time,
                update_user=update_user,
                updated_at=updated_at,
            )

    except Exception as error:
        status, error_text = classify_api_error(error)
        rows.append(
            base_prefix
            + [
                "",
                "A/B Test on IAPs Screen",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                status,
                error_text,
                updated_at,
            ]
        )

    return rows

# =========================
# DAILY NOTIFICATIONS
# =========================

notification_api_session = None


def split_csv(value: str) -> list[str]:
    if value is None:
        return []

    parts = []
    for item in str(value).split(","):
        item = item.strip()
        if item:
            parts.append(item)

    return parts


def get_notification_keywords() -> list[str]:
    return [item.lower() for item in split_csv(config.notification_parameter_keywords)]


def get_app_notification_parameter_keys(app: AppConfig) -> list[str]:
    return split_csv(app.daily_notification_parameters or config.daily_notification_parameters)


def iter_remote_config_parameters(template: dict):
    parameters = template.get("parameters", {}) or {}

    for parameter_key, parameter in parameters.items():
        yield parameter_key, parameter, ""

    parameter_groups = template.get("parameterGroups", {}) or {}

    for group_name, group_data in parameter_groups.items():
        group_parameters = group_data.get("parameters", {}) or {}
        for parameter_key, parameter in group_parameters.items():
            yield parameter_key, parameter, group_name


def is_notification_parameter_key(parameter_key: str, explicit_keys: list[str]) -> bool:
    key_lower = str(parameter_key).lower()

    if explicit_keys:
        explicit_lower = [item.lower() for item in explicit_keys]
        return any(
            key_lower == item or item in key_lower or key_lower in item
            for item in explicit_lower
        )

    keywords = get_notification_keywords()
    return any(keyword and keyword in key_lower for keyword in keywords)


def get_parameter_value_rows(parameter: dict, condition_lookup: dict, condition_priority: dict) -> list[dict]:
    value_rows = []

    default_value_object = parameter.get("defaultValue")
    if default_value_object:
        value_rows.append(
            {
                "value_source": "Default value",
                "condition_name": "Default",
                "value_object": default_value_object,
                "condition_priority": "",
                "condition_expression": "",
            }
        )

    conditional_values = parameter.get("conditionalValues", {}) or {}

    for condition_name, value_object in conditional_values.items():
        condition = condition_lookup.get(condition_name, {}) or {}
        value_rows.append(
            {
                "value_source": "Conditional value",
                "condition_name": condition_name,
                "value_object": value_object,
                "condition_priority": condition_priority.get(condition_name, ""),
                "condition_expression": condition.get("expression", ""),
            }
        )

    return value_rows


def value_object_to_plain_values(value_object: dict | None) -> list[dict]:
    if not value_object:
        return []

    if "value" in value_object:
        return [
            {
                "value": str(value_object.get("value", "")),
                "experiment_id": "",
                "variant_id": "",
                "variant_label": "",
            }
        ]

    if "experimentValue" in value_object:
        experiment = value_object.get("experimentValue", {}) or {}
        experiment_id = experiment.get("experimentId", "")
        values = []

        for variant in experiment.get("variantValue", []) or []:
            if "value" in variant:
                variant_value = str(variant.get("value", ""))
            elif variant.get("noChange") is True:
                variant_value = "No change"
            else:
                variant_value = ""

            values.append(
                {
                    "value": variant_value,
                    "experiment_id": experiment_id,
                    "variant_id": variant.get("variantId", ""),
                    "variant_label": "Experiment variant",
                }
            )

        return values

    if value_object.get("useInAppDefault") is True:
        return [
            {
                "value": "Use in-app default",
                "experiment_id": "",
                "variant_id": "",
                "variant_label": "",
            }
        ]

    return [
        {
            "value": format_remote_config_value(value_object),
            "experiment_id": "",
            "variant_id": "",
            "variant_label": "",
        }
    ]


def first_value(data: dict, keys: list[str]) -> str:
    for key in keys:
        if key in data and data[key] not in [None, ""]:
            value = data[key]
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False)
            return str(value)
    return ""


def find_time_in_text(text_value: str) -> str:
    value = str(text_value or "")

    patterns = [
        r"\b(?:[01]?\d|2[0-3])[:.][0-5]\d\s*(?:AM|PM|am|pm)?\b",
        r"\b(?:1[0-2]|0?[1-9])\s*(?:AM|PM|am|pm)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(0).strip()

    return ""


def parameter_key_has_token(parameter_key: str, token: str) -> bool:
    key = str(parameter_key).lower()
    token = str(token).lower()

    if token in ["title", "heading", "headline", "subject", "body", "content", "text", "message", "description", "desc", "time", "hour", "schedule"]:
        return token in key

    return re.search(rf"(^|[_\-.\s]){re.escape(token)}($|[_\-.\s])", key) is not None


def get_field_type_from_parameter_key(parameter_key: str) -> str:
    if any(parameter_key_has_token(parameter_key, token) for token in ["title", "heading", "headline", "subject"]):
        return "title"

    if any(parameter_key_has_token(parameter_key, token) for token in ["body", "content", "text", "message", "description", "desc"]):
        return "body"

    if any(parameter_key_has_token(parameter_key, token) for token in ["time", "hour", "schedule", "daily_at", "send_at"]):
        return "time"

    if any(parameter_key_has_token(parameter_key, token) for token in ["days", "weekday", "weekdays"]):
        return "days"

    return ""


def get_group_key_from_parameter_key(parameter_key: str, field_type: str) -> str:
    key = str(parameter_key).lower()
    field_tokens = {
        "title": ["title", "heading", "headline", "subject"],
        "body": ["body", "content", "text", "message", "description", "desc"],
        "time": ["time", "hour", "schedule", "daily_at", "send_at"],
        "days": ["day", "days", "weekday"],
    }.get(field_type, [])

    for token in field_tokens:
        key = re.sub(rf"(^|[_\-.\s]){re.escape(token)}($|[_\-.\s])", "_", key)
        key = key.replace(token, "")

    key = re.sub(r"[_\-.\s]+", "_", key).strip("_")
    return key or str(parameter_key).lower()


def extract_notification_fields(item, raw_value: str, parameter_key: str) -> dict:
    title_keys = [
        "title",
        "notification_title",
        "heading",
        "headline",
        "subject",
        "name",
    ]
    body_keys = [
        "body",
        "notification_body",
        "content",
        "text",
        "message",
        "description",
        "desc",
    ]
    time_keys = [
        "time",
        "send_time",
        "schedule_time",
        "notification_time",
        "daily_time",
        "hour",
        "at",
        "trigger_time",
        "send_at",
    ]
    schedule_keys = ["schedule", "schedule_type", "frequency", "repeat", "type"]
    days_keys = ["days", "day", "weekday", "weekdays", "repeat_days"]
    timezone_keys = ["timezone", "time_zone", "tz"]

    if isinstance(item, dict):
        raw_item = json.dumps(item, ensure_ascii=False)
        title = first_value(item, title_keys)
        body = first_value(item, body_keys)
        send_time = first_value(item, time_keys) or find_time_in_text(raw_item)
        schedule_type = first_value(item, schedule_keys)
        days = first_value(item, days_keys)
        timezone = first_value(item, timezone_keys)
        notification_id = first_value(item, ["id", "notification_id", "key", "name"])
    else:
        raw_item = str(item)
        title = ""
        body = ""
        send_time = find_time_in_text(raw_item)
        schedule_type = ""
        days = ""
        timezone = ""
        notification_id = ""

    field_type = get_field_type_from_parameter_key(parameter_key)

    if field_type == "title" and not title:
        title = str(raw_value)
    elif field_type == "body" and not body:
        body = str(raw_value)
    elif field_type == "time" and not send_time:
        send_time = str(raw_value)
    elif field_type == "days" and not days:
        days = str(raw_value)

    if not send_time:
        send_time = find_time_in_text(str(raw_value))

    return {
        "notification_id": notification_id,
        "title": title,
        "body": body,
        "send_time": send_time,
        "schedule_type": schedule_type,
        "days": days,
        "timezone": timezone,
        "raw_value": raw_item if raw_item else str(raw_value),
        "field_type": field_type,
    }


def extract_notification_items_from_value(raw_value: str, parameter_key: str) -> list[dict]:
    value = str(raw_value or "").strip()

    if value == "":
        return []

    parsed = None
    try:
        parsed = json.loads(value)
    except Exception:
        parsed = None

    items = []

    if isinstance(parsed, list):
        for item in parsed:
            items.append(extract_notification_fields(item, value, parameter_key))
        return items

    if isinstance(parsed, dict):
        list_keys = [
            "notifications",
            "daily_notifications",
            "dailyNotification",
            "dailyNotifications",
            "push_notifications",
            "pushNotifications",
            "messages",
            "items",
        ]

        for list_key in list_keys:
            nested = parsed.get(list_key)
            if isinstance(nested, list):
                for item in nested:
                    items.append(extract_notification_fields(item, value, parameter_key))
                return items

        if any(key in parsed for key in ["title", "body", "content", "text", "message", "time", "send_time", "schedule_time"]):
            return [extract_notification_fields(parsed, value, parameter_key)]

        nested_items = []
        for nested_key, nested_value in parsed.items():
            if isinstance(nested_value, dict):
                nested_value = dict(nested_value)
                nested_value.setdefault("key", nested_key)
                nested_items.append(extract_notification_fields(nested_value, value, parameter_key))
            elif isinstance(nested_value, list):
                for item in nested_value:
                    nested_items.append(extract_notification_fields(item, value, parameter_key))

        if nested_items:
            return nested_items

    return [extract_notification_fields(value, value, parameter_key)]


def build_daily_notification_recommendation(title: str, body: str, send_time: str, raw_value: str) -> str:
    missing = []

    if not title:
        missing.append("title")
    if not body:
        missing.append("body/content")
    if not send_time:
        missing.append("send time")

    if missing:
        return "Review raw Firebase value. Missing: " + ", ".join(missing) + "."

    return "Daily notification text and time found from Firebase configuration. Verify this against the app UI and Firebase Messaging schedule."


def make_daily_notification_empty_row(
    base: list,
    status: str,
    message: str,
    updated_at: str,
    version_number: str = "",
    update_time: str = "",
    update_user: str = "",
) -> list:
    return base + [
        "Remote Config",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        version_number,
        update_time,
        update_user,
        status,
        message,
        updated_at,
    ]




def parsed_json_or_none(value: str):
    try:
        return json.loads(str(value or "").strip())
    except Exception:
        return None


def flatten_json_keys(data, prefix: str = "") -> list[str]:
    keys = []
    if isinstance(data, dict):
        for key, value in data.items():
            key_text = f"{prefix}.{key}" if prefix else str(key)
            keys.append(key_text)
            keys.extend(flatten_json_keys(value, key_text))
    elif isinstance(data, list):
        for item in data:
            keys.extend(flatten_json_keys(item, prefix))
    return keys


def value_has_notification_content_signal(parameter_key: str, raw_value: str) -> bool:
    key_lower = str(parameter_key or "").lower()
    raw_lower = str(raw_value or "").lower()
    notification_words = [
        "notification", "notifications", "notif", "notify", "push", "reminder", "fcm",
    ]
    message_words = [
        "title", "heading", "headline", "subject", "body", "content", "text", "message", "description", "desc",
    ]
    time_words = [
        "time", "hour", "schedule", "daily", "repeat", "day", "weekday", "send_at", "trigger",
    ]

    if any(word in key_lower for word in notification_words):
        return True

    # If developers used separated keys such as title_1/body_1/time_1, allow content scan to pick them.
    if get_field_type_from_parameter_key(parameter_key) in ["title", "body", "time", "days"]:
        return True

    parsed = parsed_json_or_none(raw_value)
    if isinstance(parsed, (dict, list)):
        keys_text = " ".join(flatten_json_keys(parsed)).lower()
        has_notification_word = any(word in keys_text for word in notification_words)
        has_message_word = any(word in keys_text for word in message_words)
        has_time_word = any(word in keys_text for word in time_words)
        if has_notification_word or (has_message_word and has_time_word):
            return True

    if any(word in raw_lower for word in ["notification", "push", "reminder"]):
        return True

    return False


def append_daily_notification_rows_from_parameter(
    rows: list[list],
    base: list,
    parameter_key: str,
    parameter: dict,
    group_name: str,
    condition_lookup: dict,
    condition_priority: dict,
    version_number: str,
    update_time: str,
    update_user: str,
    updated_at: str,
    source_label: str = "Remote Config",
    status_label: str = "SUCCESS",
    recommendation_prefix: str = "",
) -> tuple[int, dict]:
    value_type = parameter.get("valueType", "STRING")
    grouped = {}
    direct_count = 0

    for value_row in get_parameter_value_rows(parameter, condition_lookup, condition_priority):
        for plain_value in value_object_to_plain_values(value_row["value_object"]):
            raw_value = plain_value["value"]
            experiment_id = plain_value["experiment_id"]
            variant_id = plain_value["variant_id"]
            variant_suffix = f" / Variant {variant_id}" if variant_id else ""
            value_source = value_row["value_source"] + variant_suffix
            items = extract_notification_items_from_value(raw_value, parameter_key)

            if not items:
                continue

            for item_index, item in enumerate(items, start=1):
                field_type = item.get("field_type", "")
                if field_type == "time":
                    simple_field_value = item.get("send_time", "")
                elif field_type in ["title", "body", "days"]:
                    simple_field_value = item.get(field_type, "")
                else:
                    simple_field_value = ""

                if field_type and simple_field_value and len(items) == 1:
                    group_key = (
                        value_row["condition_name"],
                        experiment_id,
                        variant_id,
                        get_group_key_from_parameter_key(parameter_key, field_type),
                    )
                    grouped.setdefault(
                        group_key,
                        {
                            "parameter_keys": set(),
                            "group_name": group_name,
                            "value_source": value_source,
                            "condition_name": value_row["condition_name"],
                            "condition_priority": value_row["condition_priority"],
                            "condition_expression": value_row["condition_expression"],
                            "value_type": value_type,
                            "experiment_id": experiment_id,
                            "variant_id": variant_id,
                            "title": "",
                            "body": "",
                            "send_time": "",
                            "days": "",
                            "schedule_type": "Daily / Firebase configured",
                            "timezone": "",
                            "raw_values": [],
                        },
                    )
                    grouped[group_key]["parameter_keys"].add(parameter_key)
                    grouped[group_key]["raw_values"].append(f"{parameter_key}: {raw_value}")

                    if field_type == "title":
                        grouped[group_key]["title"] = simple_field_value
                    elif field_type == "body":
                        grouped[group_key]["body"] = simple_field_value
                    elif field_type == "time":
                        grouped[group_key]["send_time"] = simple_field_value
                    elif field_type == "days":
                        grouped[group_key]["days"] = simple_field_value
                    continue

                notification_no = item.get("notification_id") or item_index
                title = item.get("title", "")
                body = item.get("body", "")
                send_time = item.get("send_time", "")
                schedule_type = item.get("schedule_type", "") or "Daily / Firebase configured"
                days = item.get("days", "")
                timezone = item.get("timezone", "")
                raw_item = item.get("raw_value", raw_value)
                recommendation = build_daily_notification_recommendation(title, body, send_time, raw_item)
                if recommendation_prefix:
                    recommendation = recommendation_prefix + " " + recommendation

                rows.append(
                    base
                    + [
                        source_label,
                        parameter_key,
                        group_name,
                        notification_no,
                        title,
                        body,
                        send_time,
                        schedule_type,
                        days,
                        timezone,
                        value_source,
                        value_type,
                        value_row["condition_name"],
                        value_row["condition_priority"],
                        value_row["condition_expression"],
                        experiment_id,
                        variant_id,
                        version_number,
                        update_time,
                        update_user,
                        status_label,
                        recommendation,
                        updated_at,
                    ]
                )
                direct_count += 1

    return direct_count, grouped

def build_daily_notification_rows_for_app(app: AppConfig) -> list[list]:
    rows = []
    report_date_range = get_report_date_range_display()
    updated_at = now_text()
    explicit_keys = get_app_notification_parameter_keys(app)

    base = [
        app.app_name,
        app.property_id,
        app.firebase_project_id,
        app.firebase_project_name,
        app.firebase_app_id,
        report_date_range,
    ]

    if not app.firebase_project_id:
        return [
            make_daily_notification_empty_row(
                base,
                "MISSING FIREBASE PROJECT ID",
                "Add Firebase Project ID in Apps Config column F.",
                updated_at,
            )
        ]

    try:
        template, etag = get_firebase_remote_config_template(app.firebase_project_id)
        condition_lookup, condition_priority = get_condition_lookup(template)
        version = template.get("version", {}) or {}
        version_number = version.get("versionNumber", "")
        update_time = version.get("updateTime", "")
        update_user = (version.get("updateUser", {}) or {}).get("email", "")
        matched_any = False
        grouped = {}

        # 1) Normal exact/keyword matching from Apps Config and notification keywords.
        for parameter_key, parameter, group_name in iter_remote_config_parameters(template):
            if not is_notification_parameter_key(parameter_key, explicit_keys):
                continue

            matched_any = True
            direct_count, parameter_grouped = append_daily_notification_rows_from_parameter(
                rows=rows,
                base=base,
                parameter_key=parameter_key,
                parameter=parameter,
                group_name=group_name,
                condition_lookup=condition_lookup,
                condition_priority=condition_priority,
                version_number=version_number,
                update_time=update_time,
                update_user=update_user,
                updated_at=updated_at,
                source_label="Remote Config",
                status_label="SUCCESS",
            )
            grouped.update(parameter_grouped)

        # 2) Fallback content scan. This catches names such as title_1/body_1/time_1
        # or JSON values that contain title/body/time even if the parameter key does not contain notification.
        if not matched_any and not explicit_keys:
            for parameter_key, parameter, group_name in iter_remote_config_parameters(template):
                scan_parameter = False
                for value_row in get_parameter_value_rows(parameter, condition_lookup, condition_priority):
                    for plain_value in value_object_to_plain_values(value_row["value_object"]):
                        if value_has_notification_content_signal(parameter_key, plain_value["value"]):
                            scan_parameter = True
                            break
                    if scan_parameter:
                        break

                if not scan_parameter:
                    continue

                matched_any = True
                direct_count, parameter_grouped = append_daily_notification_rows_from_parameter(
                    rows=rows,
                    base=base,
                    parameter_key=parameter_key,
                    parameter=parameter,
                    group_name=group_name,
                    condition_lookup=condition_lookup,
                    condition_priority=condition_priority,
                    version_number=version_number,
                    update_time=update_time,
                    update_user=update_user,
                    updated_at=updated_at,
                    source_label="Remote Config / Content Scan",
                    status_label="POSSIBLE MATCH",
                    recommendation_prefix="Parameter key did not match notification keywords; found by scanning value/content.",
                )
                grouped.update(parameter_grouped)

        for group_data in grouped.values():
            parameter_keys = ", ".join(sorted(group_data["parameter_keys"]))
            raw_values = " | ".join(group_data["raw_values"])
            title = group_data.get("title", "")
            body = group_data.get("body", "")
            send_time = group_data.get("send_time", "")
            recommendation = build_daily_notification_recommendation(title, body, send_time, raw_values)
            if "Remote Config / Content Scan" in str(group_data.get("source_label", "")):
                recommendation = "Possible match found by content scan. " + recommendation

            rows.append(
                base
                + [
                    "Remote Config",
                    parameter_keys,
                    group_data["group_name"],
                    "Grouped",
                    title,
                    body,
                    send_time,
                    group_data.get("schedule_type", ""),
                    group_data.get("days", ""),
                    group_data.get("timezone", ""),
                    group_data["value_source"],
                    group_data["value_type"],
                    group_data["condition_name"],
                    group_data["condition_priority"],
                    group_data["condition_expression"],
                    group_data["experiment_id"],
                    group_data["variant_id"],
                    version_number,
                    update_time,
                    update_user,
                    "SUCCESS",
                    recommendation,
                    updated_at,
                ]
            )

        if not matched_any:
            search_note = ", ".join(explicit_keys) if explicit_keys else config.notification_parameter_keywords
            rows.append(
                make_daily_notification_empty_row(
                    base,
                    "NO NOTIFICATION CONFIG FOUND",
                    (
                        "No Remote Config parameter matched notification keys/keywords or notification-like JSON/text content. "
                        f"Search used: {search_note}. Add exact Remote Config keys in Apps Config column J, "
                        "or ask developers to store/log notification title, body and send time."
                    ),
                    updated_at,
                    version_number,
                    update_time,
                    update_user,
                )
            )

    except Exception as error:
        status, error_text = classify_api_error(error)
        rows.append(
            make_daily_notification_empty_row(
                base,
                status,
                error_text,
                updated_at,
            )
        )

    return rows


# =========================
# GA4 NOTIFICATION EVENTS
# =========================


def get_notification_event_names() -> list[str]:
    return split_csv(config.notification_event_names)


def get_notification_event_filter() -> BetaFilterExpression:
    return beta_or_filter(
        [
            beta_exact_filter("eventName", event_name)
            for event_name in get_notification_event_names()
        ]
    )


def run_ga4_notification_events_report(app: AppConfig):
    request = RunReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[
            BetaDateRange(
                start_date=config.start_date,
                end_date=config.end_date,
            )
        ],
        dimensions=[
            Dimension(name="eventName"),
            Dimension(name="dateHourMinute"),
        ],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="eventCount"),
        ],
        dimension_filter=get_notification_event_filter(),
        limit=config.notification_event_limit,
    )

    return beta_client.run_report(request)


def build_ga4_notification_event_rows_for_app(app: AppConfig) -> list[list]:
    rows = []
    report_date_range = get_report_date_range_display()
    updated_at = now_text()

    try:
        response = run_ga4_notification_events_report(app)
        dimension_headers = [header.name for header in response.dimension_headers]
        metric_headers = [header.name for header in response.metric_headers]

        if not response.rows:
            rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    "",
                    "",
                    "",
                    "",
                    "NO NOTIFICATION EVENTS",
                    "No notification_receive / notification_open / notification_dismiss / notification_foreground events found in GA4 for this date range.",
                    updated_at,
                ]
            )
            return rows

        for row in response.rows:
            row_data = {}

            for index, dimension_value in enumerate(row.dimension_values):
                row_data[dimension_headers[index]] = dimension_value.value

            for index, metric_value in enumerate(row.metric_values):
                row_data[metric_headers[index]] = metric_value.value

            rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    row_data.get("eventName", ""),
                    row_data.get("dateHourMinute", ""),
                    to_number(row_data.get("activeUsers", 0)),
                    to_number(row_data.get("eventCount", 0)),
                    "SUCCESS",
                    "GA4 confirms FCM notification activity. Title/body text is not available here unless app logs it as registered custom dimensions.",
                    updated_at,
                ]
            )

    except Exception as error:
        status, error_text = classify_api_error(error)
        rows.append(
            [
                app.app_name,
                app.property_id,
                report_date_range,
                "",
                "",
                "",
                "",
                status,
                error_text,
                updated_at,
            ]
        )

    return rows


# =========================
# FCM DELIVERY DATA
# =========================


def get_notification_api_session():
    global notification_api_session

    if notification_api_session is None:
        notification_api_session = AuthorizedSession(credentials)

    return notification_api_session


def format_fcm_date(date_data: dict) -> str:
    year = int(date_data.get("year", 0) or 0)
    month = int(date_data.get("month", 0) or 0)
    day = int(date_data.get("day", 0) or 0)

    if year and month and day:
        return f"{year:04d}-{month:02d}-{day:02d}"

    return json.dumps(date_data, ensure_ascii=False)


def get_percent(data: dict, key: str):
    value = data.get(key, "") if data else ""
    if value in [None, ""]:
        return ""
    return f"{round(float(value), 2)}%"


def make_fcm_delivery_empty_row(
    base: list,
    status: str,
    message: str,
    updated_at: str,
) -> list:
    return base + [
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        status,
        message,
        updated_at,
    ]


def looks_like_firebase_app_id(value: str) -> bool:
    value = str(value or "").strip()
    return re.match(r"^1:\d+:(android|ios|web):", value) is not None


def list_firebase_android_apps(project_identifier: str) -> list[dict]:
    project_identifier = str(project_identifier or "").strip()
    if not project_identifier:
        return []

    apps = []
    page_token = ""

    while True:
        parent = f"projects/{quote(project_identifier, safe='-') }"
        url = f"{config.firebase_management_api_base}/{parent}/androidApps"
        params = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token

        response = get_notification_api_session().get(
            url,
            params=params,
            timeout=config.firebase_remote_config_timeout,
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"Firebase Management API error {response.status_code}: {response.text}"
            )

        payload = response.json()
        apps.extend(payload.get("apps", []) or [])
        page_token = payload.get("nextPageToken", "")
        if not page_token:
            break

    return apps


def choose_firebase_android_app(app: AppConfig, apps: list[dict]) -> dict | None:
    if not apps:
        return None

    wanted = str(app.firebase_app_id or "").strip().lower()
    app_name = str(app.app_name or "").strip().lower()

    # Exact Firebase app id match.
    if wanted:
        for item in apps:
            if str(item.get("appId", "")).lower() == wanted:
                return item

    # Package name match, if the user entered package name instead of Firebase App ID.
    if wanted:
        for item in apps:
            if str(item.get("packageName", "")).lower() == wanted:
                return item

    # Display/package fuzzy match with the App Name column.
    if app_name:
        for item in apps:
            display = str(item.get("displayName", "")).strip().lower()
            package = str(item.get("packageName", "")).strip().lower()
            if display and (display in app_name or app_name in display):
                return item
            if package and (package in app_name or app_name in package):
                return item

    if len(apps) == 1:
        return apps[0]

    return None


def resolve_fcm_project_and_app_id(app: AppConfig) -> tuple[str, str, str]:
    project_identifier = str(app.firebase_project_id or "").strip()
    configured_app_id = str(app.firebase_app_id or "").strip()

    if not project_identifier:
        raise ValueError("Firebase Project ID could not be auto-resolved for this app.")

    if not configured_app_id:
        # Try to auto-find the app when only one Android app exists in the project.
        android_apps = list_firebase_android_apps(project_identifier)
        selected = choose_firebase_android_app(app, android_apps)
        if selected:
            return (
                selected.get("projectId") or project_identifier,
                selected.get("appId", ""),
                "Firebase App ID was empty; auto-resolved from Firebase Android apps list.",
            )
        raise ValueError("Firebase App ID could not be auto-resolved from accessible Firebase Android apps.")

    # If the value already looks like a Firebase App ID, use it first. We still may retry using Management API on 400.
    if looks_like_firebase_app_id(configured_app_id):
        return project_identifier, configured_app_id, "Using auto-resolved Firebase App ID."

    # If the user entered a package name or display name, resolve it through Firebase Management API.
    android_apps = list_firebase_android_apps(project_identifier)
    selected = choose_firebase_android_app(app, android_apps)
    if selected:
        return (
            selected.get("projectId") or project_identifier,
            selected.get("appId", ""),
            f"Firebase App ID auto-resolved from configured value '{configured_app_id}'.",
        )

    raise ValueError(
        "Invalid Firebase App ID. It must be Android Firebase App ID like "
        "1:1234567890:android:abcdef, or a package name that can be resolved from Firebase Management API."
    )


def request_fcm_delivery_data(project_id: str, app_id: str, encode_colons: bool = False) -> dict:
    project_part = quote(str(project_id).strip(), safe="-")
    app_safe = "" if encode_colons else ":"
    app_part = quote(str(app_id).strip(), safe=app_safe)
    parent = f"projects/{project_part}/androidApps/{app_part}"
    url = f"{config.fcm_data_api_base}/{parent}/deliveryData"
    response = get_notification_api_session().get(
        url,
        params={"pageSize": config.fcm_data_page_size},
        timeout=config.firebase_remote_config_timeout,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"FCM Data API error {response.status_code}: {response.text}"
        )

    return response.json()


def get_fcm_delivery_data_for_app(app: AppConfig) -> dict:
    project_id, app_id, resolution_note = resolve_fcm_project_and_app_id(app)

    try:
        payload = request_fcm_delivery_data(project_id, app_id, encode_colons=False)
        payload["_resolution_note"] = resolution_note
        payload["_resolved_project_id"] = project_id
        payload["_resolved_app_id"] = app_id
        return payload
    except RuntimeError as error:
        error_text = str(error)

        # Fallback for API gateways that expect the Firebase app ID percent-encoded.
        if "400" in error_text or "INVALID_ARGUMENT" in error_text:
            try:
                payload = request_fcm_delivery_data(project_id, app_id, encode_colons=True)
                payload["_resolution_note"] = resolution_note + " Retried with encoded Firebase App ID."
                payload["_resolved_project_id"] = project_id
                payload["_resolved_app_id"] = app_id
                return payload
            except RuntimeError:
                pass

        # If direct app ID failed, try Management API to normalize numeric project number/package/display values.
        try:
            android_apps = list_firebase_android_apps(app.firebase_project_id)
            selected = choose_firebase_android_app(app, android_apps)
            if selected:
                normalized_project_id = selected.get("projectId") or project_id
                normalized_app_id = selected.get("appId") or app_id
                payload = request_fcm_delivery_data(normalized_project_id, normalized_app_id, encode_colons=False)
                payload["_resolution_note"] = "Retried after resolving app through Firebase Management API."
                payload["_resolved_project_id"] = normalized_project_id
                payload["_resolved_app_id"] = normalized_app_id
                return payload
        except Exception:
            pass

        raise RuntimeError(
            error_text
            + " | Check access/config: Firebase Project ID should be the real Firebase project ID, "
            "and Firebase App ID should be the Android appId from Firebase project settings, not only project number/package name."
        )


def get_fcm_delivery_data(firebase_project_id: str, firebase_app_id: str) -> dict:
    temp_app = AppConfig(
        app_name="",
        property_id="",
        home_screen_name="",
        screen_field="",
        firebase_project_id=firebase_project_id,
        firebase_project_name="",
        firebase_app_id=firebase_app_id,
        time_capping_parameter="",
        daily_notification_parameters="",
        iap_screen_parameter="",
    )
    return get_fcm_delivery_data_for_app(temp_app)


def build_fcm_delivery_rows_for_app(app: AppConfig) -> list[list]:
    rows = []
    report_date_range = get_report_date_range_display()
    updated_at = now_text()

    base = [
        app.app_name,
        app.property_id,
        app.firebase_project_id,
        app.firebase_project_name,
        app.firebase_app_id,
        report_date_range,
    ]

    try:
        response = get_fcm_delivery_data_for_app(app)
        delivery_rows = response.get("androidDeliveryData", []) or []

        if not delivery_rows:
            rows.append(
                make_fcm_delivery_empty_row(
                    base,
                    "NO FCM DELIVERY DATA",
                    "No aggregate FCM delivery rows returned for this Firebase Android app.",
                    updated_at,
                )
            )
            return rows

        for delivery in delivery_rows:
            data = delivery.get("data", {}) or {}
            outcome = data.get("messageOutcomePercents", {}) or {}
            performance = data.get("deliveryPerformancePercents", {}) or {}
            insight = data.get("messageInsightPercents", {}) or {}
            proxy = data.get("proxyNotificationInsightPercents", {}) or {}

            rows.append(
                base
                + [
                    format_fcm_date(delivery.get("date", {}) or {}),
                    delivery.get("analyticsLabel", ""),
                    data.get("countMessagesAccepted", ""),
                    data.get("countNotificationsAccepted", ""),
                    get_percent(outcome, "delivered"),
                    get_percent(outcome, "pending"),
                    get_percent(outcome, "collapsed"),
                    get_percent(outcome, "droppedTooManyPendingMessages"),
                    get_percent(outcome, "droppedAppForceStopped"),
                    get_percent(outcome, "droppedDeviceInactive"),
                    get_percent(outcome, "droppedTtlExpired"),
                    get_percent(performance, "deliveredNoDelay"),
                    get_percent(performance, "delayedDeviceOffline"),
                    get_percent(performance, "delayedDeviceDoze"),
                    get_percent(performance, "delayedMessageThrottled"),
                    get_percent(insight, "priorityLowered"),
                    get_percent(proxy, "proxied"),
                    "SUCCESS",
                    "Aggregate FCM delivery data only. It does not include notification title/body text.",
                    updated_at,
                ]
            )

    except Exception as error:
        status, error_text = classify_api_error(error)
        rows.append(
            make_fcm_delivery_empty_row(
                base,
                status,
                error_text,
                updated_at,
            )
        )

    return rows

# =========================
# MAIN
# =========================


def main():
    print("Auto-discovering accessible apps from GA4/Firebase...")

    apps = discover_accessible_apps()

    print(f"Total accessible Android apps found: {len(apps)}")

    package_name_lookup = {}

    for app in apps:
        app_name_key = str(app.app_name).strip()
        property_id_key = str(app.property_id).strip()
        package_name = fetch_ga4_package_name(
            app.property_id,
            app.firebase_app_id,
        )
        package_name_lookup[f"{app_name_key}|{property_id_key}"] = package_name

        if property_id_key and property_id_key not in package_name_lookup:
            package_name_lookup[property_id_key] = package_name

        if package_name:
            print(f"Package name found for {app.app_name}: {package_name}")
        else:
            print(f"Package name not found for {app.app_name} / {app.property_id}.")

    funnel_summary_rows = [
        [
            "App Name",
            "Property ID",
            "Date Range",
            "First Open Users",
            "Home Users",
            "Drop Off",
            "Conversion Rate",
            "Abandonment Rate",
            "Home Screen Name",
            "Screen Field",
            "Status",
            "Error",
            "Updated At",
        ]
    ]

    funnel_details_rows = [
        [
            "App Name",
            "Property ID",
            "Date Range",
            "Funnel Step",
            "Event Name",
            "Screen Condition",
            "Active Users",
            "Completion Rate",
            "Abandonments",
            "Abandonment Rate",
            "Status",
            "Error",
            "Updated At",
        ]
    ]

    user_session_rows = [
        [
            "App Name",
            "Property ID",
            "Report Date Range",
            "Active Users",
            "New Users",
            "Sessions",
            "Engaged Sessions",
            "Avg Session Duration Seconds",
            "Avg Session Duration",
            "Total Engagement Seconds",
            "Total Engagement Time",
            "Sessions Per Active User",
            "Engagement Rate",
            "Retention Cohort Date Range",
            "Cohort Total Users",
            "D1 Active Users",
            "D1 Retention",
            "D7 Active Users",
            "D7 Retention",
            "Status",
            "Error",
            "Updated At",
        ]
    ]

    retention_details_rows = [
        [
            "App Name",
            "Property ID",
            "Report Date Range",
            "Retention Cohort Date Range",
            "Cohort Name",
            "Retention Day",
            "Day Number",
            "Cohort Active Users",
            "Cohort Total Users",
            "Retention Rate",
            "Status",
            "Error",
            "Updated At",
        ]
    ]

    audience_segment_rows = [
        [
            "App Name",
            "Property ID",
            "Report Date Range",
            "Audience Segment",
            "Segment Rule",
            "Active Users",
            "New Users",
            "Sessions",
            "Engaged Sessions",
            "Avg Session Duration Seconds",
            "Avg Session Duration",
            "Total Engagement Seconds",
            "Total Engagement Time",
            "Sessions Per Active User",
            "Engagement Rate",
            "Status",
            "Error",
            "Updated At",
        ]
    ]

    personalized_ux_rows = [
        [
            "App Name",
            "Property ID",
            "Report Date Range",
            "Personalization Breakdown",
            "GA4 Dimension",
            "Dimension Value",
            "Active Users",
            "New Users",
            "Sessions",
            "Engaged Sessions",
            "Avg Session Duration Seconds",
            "Avg Session Duration",
            "Total Engagement Seconds",
            "Total Engagement Time",
            "Sessions Per Active User",
            "Engagement Rate",
            "Recommendation",
            "Status",
            "Error",
            "Updated At",
        ]
    ]

    remote_config_rows = [
        [
            "App Name",
            "Property ID",
            "Report Date Range",
            "Remote Config Area",
            "Rule / Type",
            "Value",
            "Active Users",
            "New Users",
            "Sessions",
            "Avg Session Duration",
            "Event Count",
            "Status",
            "Recommendation / Error",
            "Updated At",
        ]
    ]

    time_capping_ab_rows = [
        [
            "App Name",
            "Property ID",
            "Firebase Project ID",
            "Firebase Project Name",
            "Firebase App ID",
            "Report Date Range",
            "Remote Config Parameter",
            "Parameter Group",
            "Value Source",
            "Condition / Variant",
            "Remote Config Value",
            "Value Type",
            "Condition Priority",
            "Condition Expression",
            "Experiment ID",
            "Variant ID",
            "Template Version",
            "Last Published At",
            "Last Published By",
            "Status",
            "Recommendation / Error",
            "Updated At",
        ]
    ]

    iap_screen_ab_rows = [
        [
            "App Name",
            "Property ID",
            "Firebase Project ID",
            "Firebase Project Name",
            "Firebase App ID",
            "Report Date Range",
            "Remote Config Parameter",
            "Parameter Group",
            "Value Source",
            "Condition / Variant",
            "Remote Config Value",
            "Value Type",
            "Condition Priority",
            "Condition Expression",
            "Experiment ID",
            "Variant ID",
            "Template Version",
            "Last Published At",
            "Last Published By",
            "Status",
            "Recommendation / Error",
            "Updated At",
        ]
    ]

    daily_notifications_rows = [
        [
            "App Name",
            "Property ID",
            "Firebase Project ID",
            "Firebase Project Name",
            "Firebase App ID",
            "Report Date Range",
            "Source",
            "Remote Config Parameter(s)",
            "Parameter Group",
            "Notification No",
            "Notification Title",
            "Notification Body / Content",
            "Send Time",
            "Schedule Type",
            "Days / Repeat",
            "Timezone",
            "Value Source",
            "Value Type",
            "Condition / Audience",
            "Condition Priority",
            "Condition Expression",
            "Experiment ID",
            "Variant ID",
            "Template Version",
            "Last Published At",
            "Last Published By",
            "Status",
            "Recommendation / Error",
            "Updated At",
        ]
    ]

    ga4_notification_event_rows = [
        [
            "App Name",
            "Property ID",
            "Report Date Range",
            "Notification Event",
            "GA4 Date Hour Minute",
            "Active Users",
            "Event Count",
            "Status",
            "Recommendation / Error",
            "Updated At",
        ]
    ]

    fcm_delivery_rows = [
        [
            "App Name",
            "Property ID",
            "Firebase Project ID",
            "Firebase Project Name",
            "Firebase App ID",
            "Report Date Range",
            "FCM Date",
            "Analytics Label",
            "Messages Accepted",
            "Notifications Accepted",
            "Delivered %",
            "Pending %",
            "Collapsed %",
            "Dropped: Too Many Pending %",
            "Dropped: App Force Stopped %",
            "Dropped: Device Inactive %",
            "Dropped: TTL Expired %",
            "Delivered No Delay %",
            "Delayed: Device Offline %",
            "Delayed: Device Doze %",
            "Delayed: Message Throttled %",
            "Priority Lowered %",
            "Proxied %",
            "Status",
            "Recommendation / Error",
            "Updated At",
        ]
    ]

    report_date_range = get_report_date_range_display()

    for app in apps:
        print(f"Processing: {app.app_name} / {app.property_id}")

        # Funnel
        try:
            funnel_response = run_first_open_to_home_funnel(app)
            funnel_summary_row, app_funnel_details = parse_funnel_rows(
                app,
                funnel_response,
            )

            funnel_summary_rows.append(funnel_summary_row)
            funnel_details_rows.extend(app_funnel_details)

        except Exception as error:
            status, error_text = classify_api_error(error)
            updated_at = now_text()

            print(f"FUNNEL {status} for {app.app_name}: {error_text}")

            funnel_summary_rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    "",
                    "",
                    "",
                    "",
                    "",
                    app.home_screen_name,
                    app.screen_field,
                    status,
                    error_text,
                    updated_at,
                ]
            )

            funnel_details_rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    status,
                    error_text,
                    updated_at,
                ]
            )

        # User/session + retention
        session_data = empty_session_data()
        retention_summary = empty_retention_summary()
        errors = []
        status_priority = []

        try:
            session_response = run_user_session_report(app)
            session_data = parse_user_session_report(session_response)

        except Exception as error:
            status, error_text = classify_api_error(error)
            errors.append(f"Session {status}: {error_text}")
            status_priority.append(status)
            print(f"SESSION {status} for {app.app_name}: {error_text}")

        try:
            retention_response = run_retention_report(app)
            retention_summary, app_retention_details = parse_retention_report(
                app,
                retention_response,
            )

            retention_details_rows.extend(app_retention_details)

        except Exception as error:
            status, error_text = classify_api_error(error)
            errors.append(f"Retention {status}: {error_text}")
            status_priority.append(status)
            print(f"RETENTION {status} for {app.app_name}: {error_text}")

            append_error_retention_detail(
                retention_details_rows=retention_details_rows,
                app=app,
                report_date_range=report_date_range,
                retention_summary=retention_summary,
                status=status,
                error_text=error_text,
            )

        if not errors:
            user_session_status = "SUCCESS"
            user_session_error = ""
        elif "NO ACCESS" in status_priority:
            user_session_status = "NO ACCESS"
            user_session_error = " | ".join(errors)
        elif "INVALID PROPERTY ID" in status_priority:
            user_session_status = "INVALID PROPERTY ID"
            user_session_error = " | ".join(errors)
        else:
            user_session_status = "ERROR"
            user_session_error = " | ".join(errors)

        updated_at = now_text()

        user_session_rows.append(
            [
                app.app_name,
                app.property_id,
                report_date_range,
                session_data["active_users"],
                session_data["new_users"],
                session_data["sessions"],
                session_data["engaged_sessions"],
                session_data["average_session_duration_seconds"],
                session_data["average_session_duration"],
                session_data["total_engagement_seconds"],
                session_data["total_engagement_time"],
                session_data["sessions_per_active_user"],
                session_data["engagement_rate"],
                retention_summary["cohort_date_range"],
                retention_summary["cohort_total_users"],
                retention_summary["d1_active_users"],
                retention_summary["d1_retention"],
                retention_summary["d7_active_users"],
                retention_summary["d7_retention"],
                user_session_status,
                user_session_error,
                updated_at,
            ]
        )

        # Audience segments
        try:
            app_audience_rows = build_audience_segment_rows_for_app(app)
            audience_segment_rows.extend(app_audience_rows)

        except Exception as error:
            status, error_text = classify_api_error(error)

            audience_segment_rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    status,
                    error_text,
                    now_text(),
                ]
            )

        # Personalized user experience
        try:
            app_personalized_rows = build_personalized_ux_rows_for_app(app)
            personalized_ux_rows.extend(app_personalized_rows)

        except Exception as error:
            status, error_text = classify_api_error(error)

            personalized_ux_rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    status,
                    error_text,
                    now_text(),
                ]
            )

        # Remote configuration
        try:
            app_remote_config_rows = build_remote_config_rows_for_app(app)
            remote_config_rows.extend(app_remote_config_rows)

        except Exception as error:
            status, error_text = classify_api_error(error)

            remote_config_rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    status,
                    error_text,
                    now_text(),
                ]
            )

        # Firebase A/B test on time capping from Remote Config
        try:
            app_time_capping_rows = build_time_capping_ab_rows_for_app(app)
            time_capping_ab_rows.extend(app_time_capping_rows)

        except Exception as error:
            status, error_text = classify_api_error(error)

            time_capping_ab_rows.append(
                [
                    app.app_name,
                    app.property_id,
                    app.firebase_project_id,
                    app.firebase_project_name,
                    app.firebase_app_id,
                    report_date_range,
                    app.time_capping_parameter,
                    "",
                    "A/B Test on Time Capping",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    status,
                    error_text,
                    now_text(),
                ]
            )

        # Firebase A/B test on IAP/paywall screen from Remote Config
        try:
            app_iap_screen_rows = build_iap_screen_ab_rows_for_app(app)
            iap_screen_ab_rows.extend(app_iap_screen_rows)

        except Exception as error:
            status, error_text = classify_api_error(error)

            iap_screen_ab_rows.append(
                [
                    app.app_name,
                    app.property_id,
                    app.firebase_project_id,
                    app.firebase_project_name,
                    app.firebase_app_id,
                    report_date_range,
                    app.iap_screen_parameter,
                    "",
                    "A/B Test on IAPs Screen",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    status,
                    error_text,
                    now_text(),
                ]
            )

        # Firebase daily notifications from Remote Config
        try:
            app_daily_notification_rows = build_daily_notification_rows_for_app(app)
            daily_notifications_rows.extend(app_daily_notification_rows)

        except Exception as error:
            status, error_text = classify_api_error(error)

            daily_notifications_rows.append(
                make_daily_notification_empty_row(
                    [
                        app.app_name,
                        app.property_id,
                        app.firebase_project_id,
                        app.firebase_project_name,
                        app.firebase_app_id,
                        report_date_range,
                    ],
                    status,
                    error_text,
                    now_text(),
                )
            )

        # GA4 notification receive/open/dismiss events
        try:
            app_ga4_notification_rows = build_ga4_notification_event_rows_for_app(app)
            ga4_notification_event_rows.extend(app_ga4_notification_rows)

        except Exception as error:
            status, error_text = classify_api_error(error)

            ga4_notification_event_rows.append(
                [
                    app.app_name,
                    app.property_id,
                    report_date_range,
                    "",
                    "",
                    "",
                    "",
                    status,
                    error_text,
                    now_text(),
                ]
            )

        # FCM aggregate delivery data
        try:
            app_fcm_delivery_rows = build_fcm_delivery_rows_for_app(app)
            fcm_delivery_rows.extend(app_fcm_delivery_rows)

        except Exception as error:
            status, error_text = classify_api_error(error)

            fcm_delivery_rows.append(
                make_fcm_delivery_empty_row(
                    [
                        app.app_name,
                        app.property_id,
                        app.firebase_project_id,
                        app.firebase_project_name,
                        app.firebase_app_id,
                        report_date_range,
                    ],
                    status,
                    error_text,
                    now_text(),
                )
            )

    write_report_sheet(config.summary_sheet, funnel_summary_rows, package_name_lookup)
    write_report_sheet(config.details_sheet, funnel_details_rows, package_name_lookup)
    write_report_sheet(config.user_session_sheet, user_session_rows, package_name_lookup)
    write_report_sheet(config.retention_details_sheet, retention_details_rows, package_name_lookup)
    write_report_sheet(config.audience_segments_sheet, audience_segment_rows, package_name_lookup)
    write_report_sheet(config.personalized_ux_sheet, personalized_ux_rows, package_name_lookup)
    write_report_sheet(config.remote_config_sheet, remote_config_rows, package_name_lookup)
    write_report_sheet(config.time_capping_ab_sheet, time_capping_ab_rows, package_name_lookup)
    write_report_sheet(config.iap_screen_ab_sheet, iap_screen_ab_rows, package_name_lookup)
    write_report_sheet(config.daily_notifications_sheet, daily_notifications_rows, package_name_lookup)
    write_report_sheet(config.ga4_notification_events_sheet, ga4_notification_event_rows, package_name_lookup)
    write_report_sheet(config.fcm_delivery_sheet, fcm_delivery_rows, package_name_lookup)

    print("Done. All reports updated in Google Sheet.")
    print(f"Funnel Summary: {config.summary_sheet}")
    print(f"Funnel Details: {config.details_sheet}")
    print(f"User Session Summary: {config.user_session_sheet}")
    print(f"Retention Details: {config.retention_details_sheet}")
    print(f"Audience Segments: {config.audience_segments_sheet}")
    print(f"Personalized User Experience: {config.personalized_ux_sheet}")
    print(f"Remote Configuration: {config.remote_config_sheet}")
    print(f"Firebase A/B Time Capping: {config.time_capping_ab_sheet}")
    print(f"Firebase A/B IAP Screen: {config.iap_screen_ab_sheet}")
    print(f"Firebase Daily Notifications: {config.daily_notifications_sheet}")
    print(f"GA4 Notification Events: {config.ga4_notification_events_sheet}")
    print(f"Firebase Notification Delivery: {config.fcm_delivery_sheet}")
    print(f"Report Date Range: {report_date_range}")


if __name__ == "__main__":
    main()

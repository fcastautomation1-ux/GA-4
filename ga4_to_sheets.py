import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
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
    iap_screen_parameter: str
    app_open_event_names: str
    home_event_names: str
    feature_event_names: str


def get_credentials():
    service_account_info = json.loads(config.service_account_json)
    return service_account.Credentials.from_service_account_info(service_account_info, scopes=SCOPES)


credentials = get_credentials()
beta_client = BetaAnalyticsDataClient(credentials=credentials)
analytics_admin_session = None
remote_config_session = None
package_name_cache: dict[str, str] = {}
remote_config_template_cache: dict[str, dict] = {}

MAX_GOOGLE_SHEETS_CELL_CHARS = 49000
OLD_REPORT_SHEET_NAMES = {
    "GA4 Funnel Summary",
    "GA4 Funnel Details",
    "GA4 User Session Summary",
    "GA4 Retention Details",
    "GA4 Audience Segments",
    "GA4 Personalized User Experience",
    "GA4 Remote Configuration",
    "Firebase AB Time Capping",
    "Firebase AB IAP Screen",
    "GA4 Notification Events",
    "Firebase Notification Delivery",
    "Firebase Daily Notifications",
}


def trim_cell_value(value, max_chars: int = MAX_GOOGLE_SHEETS_CELL_CHARS):
    if value is None:
        return ""
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 40] + " ... [trimmed to fit Google Sheets cell limit]"


def sanitize_rows_for_google_sheets(rows: list[list]) -> list[list]:
    return [[trim_cell_value(value) for value in row] for row in rows]


def get_sheets_service():
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def ensure_sheet_exists(service, sheet_name: str):
    spreadsheet = service.spreadsheets().get(
        spreadsheetId=config.spreadsheet_id,
        fields="sheets.properties(title)",
    ).execute()
    existing = {sheet.get("properties", {}).get("title", "") for sheet in spreadsheet.get("sheets", [])}
    if sheet_name in existing:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=config.spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
    ).execute()


def cleanup_old_report_sheets(service):
    if not config.cleanup_old_tabs:
        return
    spreadsheet = service.spreadsheets().get(
        spreadsheetId=config.spreadsheet_id,
        fields="sheets.properties(sheetId,title)",
    ).execute()
    protected = {config.apps_config_sheet, config.merged_sheet}
    requests = []
    names = []
    for sheet in spreadsheet.get("sheets", []):
        props = sheet.get("properties", {})
        title = props.get("title", "")
        if title in OLD_REPORT_SHEET_NAMES and title not in protected:
            requests.append({"deleteSheet": {"sheetId": props["sheetId"]}})
            names.append(title)
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=config.spreadsheet_id,
            body={"requests": requests},
        ).execute()
        print("Deleted old report tabs: " + ", ".join(names))


def write_sheet(sheet_name: str, rows: list[list]):
    service = get_sheets_service()
    ensure_sheet_exists(service, sheet_name)
    cleanup_old_report_sheets(service)
    service.spreadsheets().values().clear(
        spreadsheetId=config.spreadsheet_id,
        range=f"{sheet_name}!A:ZZ",
        body={},
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=config.spreadsheet_id,
        range=f"{sheet_name}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": sanitize_rows_for_google_sheets(rows)},
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
        "App Open Event Names",
        "Home Event Names",
        "Feature Event Names",
    ]


def ensure_apps_config_headers(service, values: list[list]):
    expected_headers = get_apps_config_headers()
    current_headers = values[0] if values else []
    if current_headers[: len(expected_headers)] == expected_headers:
        return
    service.spreadsheets().values().update(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.apps_config_sheet}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [expected_headers]},
    ).execute()


def create_apps_config_template(service):
    ensure_sheet_exists(service, config.apps_config_sheet)
    service.spreadsheets().values().update(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.apps_config_sheet}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [get_apps_config_headers()]},
    ).execute()


def read_apps_config() -> list[AppConfig]:
    service = get_sheets_service()
    ensure_sheet_exists(service, config.apps_config_sheet)
    response = service.spreadsheets().values().get(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.apps_config_sheet}!A:N",
    ).execute()
    values = response.get("values", [])
    if len(values) <= 1:
        create_apps_config_template(service)
        raise SystemExit("Apps Config sheet was empty. Template created. Fill apps and run again.")
    ensure_apps_config_headers(service, values)

    apps: list[AppConfig] = []
    for index, row in enumerate(values[1:], start=2):
        enabled = row[0].strip().upper() if len(row) > 0 else ""
        app_name = row[1].strip() if len(row) > 1 else ""
        property_id = row[2].strip() if len(row) > 2 else ""
        if enabled not in {"TRUE", "YES", "1", "Y"}:
            continue
        if not app_name or not property_id:
            print(f"Skipping row {index}: App Name or Property ID is missing.")
            continue
        apps.append(
            AppConfig(
                app_name=app_name,
                property_id=property_id,
                home_screen_name=(row[3].strip() if len(row) > 3 and row[3].strip() else config.default_home_screen_name),
                screen_field=(row[4].strip() if len(row) > 4 and row[4].strip() else config.default_screen_field),
                firebase_project_id=(row[5].strip() if len(row) > 5 else ""),
                firebase_project_name=(row[6].strip() if len(row) > 6 else ""),
                firebase_app_id=(row[7].strip() if len(row) > 7 else ""),
                time_capping_parameter=(row[8].strip() if len(row) > 8 else ""),
                iap_screen_parameter=(row[10].strip() if len(row) > 10 else ""),
                app_open_event_names=(row[11].strip() if len(row) > 11 and row[11].strip() else config.app_open_event_names),
                home_event_names=(row[12].strip() if len(row) > 12 else ""),
                feature_event_names=(row[13].strip() if len(row) > 13 and row[13].strip() else config.feature_event_names),
            )
        )
    if not apps:
        raise SystemExit("No enabled apps found in Apps Config sheet.")
    return apps


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
        return (today - timedelta(days=int(match.group(1)))).isoformat()
    return value


def get_report_dates() -> list[str]:
    start = datetime.fromisoformat(resolve_ga4_date(config.start_date)).date()
    end = datetime.fromisoformat(resolve_ga4_date(config.end_date)).date()
    if start > end:
        raise ValueError(f"START_DATE must be on or before END_DATE. Current: {start} to {end}")
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def ga4_date_to_iso(value: str) -> str:
    value = str(value or "").strip()
    if re.fullmatch(r"\d{8}", value):
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    return value


def split_csv(value: str) -> list[str]:
    seen = set()
    result = []
    for item in str(value or "").split(","):
        item = item.strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def to_number(value):
    if value in {None, ""}:
        return 0
    try:
        number = float(value)
        return int(number) if number.is_integer() else round(number, 2)
    except Exception:
        return value


def to_float(value) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(value)
    except Exception:
        return 0.0


def percent(value) -> str:
    try:
        number = float(value)
        if number <= 1:
            number *= 100
        return f"{round(number, 2)}%"
    except Exception:
        return ""


def rate(numerator, denominator) -> str:
    denominator = to_float(denominator)
    if denominator == 0:
        return "0%"
    return f"{round((to_float(numerator) / denominator) * 100, 2)}%"


def format_seconds(seconds_value) -> str:
    total = int(round(to_float(seconds_value)))
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def classify_api_error(error) -> tuple[str, str]:
    text = str(error)
    lower = text.lower()
    if any(term in lower for term in ["service_disabled", "has not been enabled", "api disabled", "api not enabled"]):
        return "API NOT ENABLED", text
    if any(term in lower for term in ["403", "permission denied", "access denied", "insufficient permissions"]):
        return "NO ACCESS", text
    if any(term in lower for term in ["404", "not found", "invalid property"]):
        return "INVALID PROPERTY ID", text
    return "ERROR", text


def get_analytics_admin_session():
    global analytics_admin_session
    if analytics_admin_session is None:
        analytics_admin_session = AuthorizedSession(credentials)
    return analytics_admin_session


def fetch_ga4_package_name(app: AppConfig) -> str:
    if not config.fetch_package_name:
        return ""
    cache_key = f"{app.property_id}|{app.firebase_app_id}"
    if cache_key in package_name_cache:
        return package_name_cache[cache_key]
    try:
        url = f"{config.ga4_admin_api_base}/properties/{app.property_id}/dataStreams"
        params = {"pageSize": 200}
        streams = []
        while True:
            response = get_analytics_admin_session().get(url, params=params, timeout=30)
            if response.status_code >= 400:
                raise RuntimeError(f"GA4 Admin API error {response.status_code}: {response.text}")
            payload = response.json()
            streams.extend(payload.get("dataStreams", []) or [])
            token = payload.get("nextPageToken", "")
            if not token:
                break
            params["pageToken"] = token
        android_streams = []
        for stream in streams:
            android = stream.get("androidAppStreamData", {}) or {}
            package_name = str(android.get("packageName", "")).strip()
            firebase_app_id = str(android.get("firebaseAppId", "")).strip()
            if package_name:
                android_streams.append((package_name, firebase_app_id))
        if app.firebase_app_id:
            for package_name, firebase_app_id in android_streams:
                if firebase_app_id == app.firebase_app_id:
                    package_name_cache[cache_key] = package_name
                    return package_name
        package_names = []
        seen = set()
        for package_name, _ in android_streams:
            if package_name not in seen:
                package_names.append(package_name)
                seen.add(package_name)
        result = ", ".join(package_names)
        package_name_cache[cache_key] = result
        return result
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"PACKAGE NAME {status} for {app.app_name} / {app.property_id}: {error_text}")
        package_name_cache[cache_key] = ""
        return ""


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


def in_list_filter(field_name: str, values: list[str]) -> FilterExpression:
    return FilterExpression(
        filter=Filter(
            field_name=field_name,
            in_list_filter=Filter.InListFilter(values=values, case_sensitive=False),
        )
    )


def or_filter(expressions: list[FilterExpression]) -> FilterExpression:
    return FilterExpression(or_group=FilterExpressionList(expressions=expressions))


def and_filter(expressions: list[FilterExpression]) -> FilterExpression:
    return FilterExpression(and_group=FilterExpressionList(expressions=expressions))


def date_order() -> OrderBy:
    return OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))


def metric_order(metric_name: str, desc: bool = True) -> OrderBy:
    return OrderBy(metric=OrderBy.MetricOrderBy(metric_name=metric_name), desc=desc)


def row_to_dict(response_row, dimension_headers: list[str], metric_headers: list[str]) -> dict:
    data = {}
    for index, value in enumerate(response_row.dimension_values):
        if index < len(dimension_headers):
            data[dimension_headers[index]] = value.value
    for index, value in enumerate(response_row.metric_values):
        if index < len(metric_headers):
            data[metric_headers[index]] = value.value
    return data


def parse_response_rows(response) -> list[dict]:
    dimension_headers = [header.name for header in response.dimension_headers]
    metric_headers = [header.name for header in response.metric_headers]
    return [row_to_dict(row, dimension_headers, metric_headers) for row in response.rows]


def run_daily_metrics_report(app: AppConfig) -> dict[str, dict]:
    request = RunReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[DateRange(start_date=config.start_date, end_date=config.end_date)],
        dimensions=[Dimension(name="date")],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="newUsers"),
            Metric(name="sessions"),
            Metric(name="engagedSessions"),
            Metric(name="averageSessionDuration"),
            Metric(name="userEngagementDuration"),
            Metric(name="engagementRate"),
            Metric(name="eventCount"),
            Metric(name="totalRevenue"),
        ],
        order_bys=[date_order()],
        keep_empty_rows=True,
        limit=100000,
    )
    response = beta_client.run_report(request)
    by_date = {}
    for row in parse_response_rows(response):
        report_date = ga4_date_to_iso(row.get("date", ""))
        active_users = to_number(row.get("activeUsers", 0))
        sessions = to_number(row.get("sessions", 0))
        session_seconds = to_float(row.get("averageSessionDuration", 0))
        engagement_seconds = to_float(row.get("userEngagementDuration", 0))
        by_date[report_date] = {
            "Active Users": active_users,
            "New Users": to_number(row.get("newUsers", 0)),
            "Sessions": sessions,
            "Engaged Sessions": to_number(row.get("engagedSessions", 0)),
            "Avg Session Duration": format_seconds(session_seconds),
            "Avg Session Duration Seconds": round(session_seconds, 2),
            "Total Engagement Time": format_seconds(engagement_seconds),
            "Total Engagement Seconds": round(engagement_seconds, 2),
            "Sessions Per Active User": round(to_float(sessions) / to_float(active_users), 2) if to_float(active_users) else 0,
            "Engagement Rate": percent(row.get("engagementRate", 0)),
            "Total Event Count": to_number(row.get("eventCount", 0)),
            "Total Revenue": round(to_float(row.get("totalRevenue", 0)), 2),
        }
    return by_date


def run_event_report(app: AppConfig, event_names: list[str]) -> dict[tuple[str, str], dict]:
    if not event_names:
        return {}
    request = RunReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[DateRange(start_date=config.start_date, end_date=config.end_date)],
        dimensions=[Dimension(name="date"), Dimension(name="eventName")],
        metrics=[Metric(name="activeUsers"), Metric(name="eventCount")],
        dimension_filter=in_list_filter("eventName", event_names),
        order_bys=[date_order()],
        keep_empty_rows=False,
        limit=100000,
    )
    response = beta_client.run_report(request)
    result = {}
    for row in parse_response_rows(response):
        report_date = ga4_date_to_iso(row.get("date", ""))
        event_name = row.get("eventName", "")
        result[(report_date, event_name)] = {
            "active_users": to_number(row.get("activeUsers", 0)),
            "event_count": to_number(row.get("eventCount", 0)),
        }
    return result


def run_home_screen_report(app: AppConfig) -> dict[str, dict]:
    request = RunReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[DateRange(start_date=config.start_date, end_date=config.end_date)],
        dimensions=[Dimension(name="date")],
        metrics=[Metric(name="activeUsers"), Metric(name="eventCount")],
        dimension_filter=and_filter([exact_filter("eventName", "screen_view"), contains_filter(app.screen_field, app.home_screen_name)]),
        order_bys=[date_order()],
        keep_empty_rows=True,
        limit=100000,
    )
    response = beta_client.run_report(request)
    result = {}
    for row in parse_response_rows(response):
        report_date = ga4_date_to_iso(row.get("date", ""))
        result[report_date] = {
            "active_users": to_number(row.get("activeUsers", 0)),
            "event_count": to_number(row.get("eventCount", 0)),
        }
    return result


def is_retention_target_ready(
    cohort_date: str,
    day_offset: int,
    data_lag_days: int = 1,
) -> bool:
    """Return True after the target day should be fully processed by GA4."""
    cohort_day = datetime.fromisoformat(cohort_date).date()
    target_day = cohort_day + timedelta(days=day_offset)
    latest_complete_day = (
        datetime.now(ZoneInfo(config.timezone)).date()
        - timedelta(days=data_lag_days)
    )
    return target_day <= latest_complete_day


def chunked(values: list[str], size: int):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def parse_cohort_day(value) -> int:
    text = str(value or "0")
    digits = re.sub(r"[^0-9]", "", text)
    return int(digits or 0)


def run_retention_report(
    app: AppConfig,
    report_dates: list[str],
) -> dict[str, dict]:
    """Return API-native retention using the supported firstSessionDate cohort.

    This intentionally avoids funnel-based first_open approximations, which can
    greatly overcount D1/D7 users. Cohort Total Users, D1 and D7 are therefore
    produced by one consistent GA4 cohort definition.
    """
    if config.retention_days <= 0 or not report_dates:
        return {}

    retention_by_date: dict[str, dict] = {
        report_date: {
            "Cohort Total Users": "Not available yet",
            "D1 Active Users": "Not available yet",
            "D1 Retention": "Not available yet",
            "D7 Active Users": "Not available yet",
            "D7 Retention": "Not available yet",
        }
        for report_date in report_dates
    }

    # The Data API supports at most a limited number of cohorts per request.
    # Small chunks also keep responses easy to parse and retry.
    for dates_chunk in chunked(report_dates, 12):
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
                        name=report_date,
                        dimension="firstSessionDate",
                        date_range=DateRange(
                            start_date=report_date,
                            end_date=report_date,
                        ),
                    )
                    for report_date in dates_chunk
                ],
                cohorts_range=CohortsRange(
                    granularity=CohortsRange.Granularity.DAILY,
                    start_offset=0,
                    end_offset=min(max(config.retention_days, 1), 7),
                ),
            ),
            keep_empty_rows=True,
            limit=100000,
        )

        response = beta_client.run_report(request)

        for row in parse_response_rows(response):
            report_date = row.get("cohort", "")
            if report_date not in retention_by_date:
                continue

            day = parse_cohort_day(row.get("cohortNthDay", 0))
            active_users = to_number(row.get("cohortActiveUsers", 0))
            total_users = to_number(row.get("cohortTotalUsers", 0))
            retention = retention_by_date[report_date]

            if is_retention_target_ready(report_date, 0):
                retention["Cohort Total Users"] = total_users

            if day == 1 and config.retention_days >= 1:
                if is_retention_target_ready(report_date, 1):
                    retention["D1 Active Users"] = active_users
                    retention["D1 Retention"] = rate(active_users, total_users)

            if day == 7 and config.retention_days >= 7:
                if is_retention_target_ready(report_date, 7):
                    retention["D7 Active Users"] = active_users
                    retention["D7 Retention"] = rate(active_users, total_users)

    return retention_by_date

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
        ("All Users", None),
        ("US Users", exact_filter("country", "United States")),
        ("Direct Traffic", exact_filter("sessionDefaultChannelGroup", "Direct")),
        ("Paid Traffic", or_filter([exact_filter("sessionDefaultChannelGroup", channel) for channel in paid_channel_groups])),
        ("Mobile Traffic", exact_filter("deviceCategory", "mobile")),
        ("Tablet Traffic", exact_filter("deviceCategory", "tablet")),
    ]


def run_segment_report(app: AppConfig, segment_name: str, segment_filter) -> dict[str, dict]:
    request_params = {
        "property": f"properties/{app.property_id}",
        "date_ranges": [DateRange(start_date=config.start_date, end_date=config.end_date)],
        "dimensions": [Dimension(name="date")],
        "metrics": [
            Metric(name="activeUsers"),
            Metric(name="newUsers"),
            Metric(name="sessions"),
            Metric(name="engagedSessions"),
            Metric(name="averageSessionDuration"),
            Metric(name="engagementRate"),
        ],
        "order_bys": [date_order()],
        "keep_empty_rows": True,
        "limit": 100000,
    }
    if segment_filter is not None:
        request_params["dimension_filter"] = segment_filter
    response = beta_client.run_report(RunReportRequest(**request_params))
    result = {}
    for row in parse_response_rows(response):
        report_date = ga4_date_to_iso(row.get("date", ""))
        result[report_date] = {
            "segment": segment_name,
            "active": to_number(row.get("activeUsers", 0)),
            "new": to_number(row.get("newUsers", 0)),
            "sessions": to_number(row.get("sessions", 0)),
            "engaged": to_number(row.get("engagedSessions", 0)),
            "avg": format_seconds(row.get("averageSessionDuration", 0)),
            "engagement": percent(row.get("engagementRate", 0)),
        }
    return result


def run_all_audience_segments(app: AppConfig, report_dates: list[str]) -> dict[str, list[dict]]:
    by_date = {report_date: [] for report_date in report_dates}
    for segment_name, segment_filter in get_audience_segments():
        try:
            data = run_segment_report(app, segment_name, segment_filter)
            for report_date in report_dates:
                row = data.get(report_date, {})
                by_date[report_date].append(
                    {
                        "segment": segment_name,
                        "active": row.get("active", 0),
                        "sessions": row.get("sessions", 0),
                        "engagement": row.get("engagement", "0%"),
                    }
                )
        except Exception as error:
            status, error_text = classify_api_error(error)
            print(f"AUDIENCE {segment_name} {status} for {app.app_name}: {error_text}")
            for report_date in report_dates:
                by_date[report_date].append({"segment": segment_name, "active": "", "sessions": "", "engagement": status})
    return by_date


def get_personalized_ux_dimensions() -> list[tuple[str, str]]:
    return [
        ("Country", "country"),
        ("Language", "language"),
        ("Device Category", "deviceCategory"),
        ("Operating System", "operatingSystem"),
        ("App Version", "appVersion"),
        ("First User Medium", "firstUserMedium"),
        ("Top Screens / Screen Class", "unifiedPagePathScreen"),
    ]


def run_dimension_session_report(app: AppConfig, label: str, dimension_name: str, report_dates: list[str]) -> dict[str, list[dict]]:
    """Return the top dimension values independently for every report date.

    The old implementation used one small global API limit. Because results are
    ordered by date, high-cardinality dimensions such as country and language
    used the entire limit on the first dates, making later dates look trimmed.

    This version reads the result in large pages. Normally it is still one API
    call; extra calls occur only when the response contains more than one page.
    """
    by_date = {report_date: [] for report_date in report_dates}
    page_size = 100000
    offset = 0

    while True:
        request = RunReportRequest(
            property=f"properties/{app.property_id}",
            date_ranges=[DateRange(start_date=config.start_date, end_date=config.end_date)],
            dimensions=[Dimension(name="date"), Dimension(name=dimension_name)],
            metrics=[
                Metric(name="activeUsers"),
                Metric(name="sessions"),
                Metric(name="averageSessionDuration"),
                Metric(name="engagementRate"),
            ],
            order_bys=[date_order(), metric_order("activeUsers")],
            limit=page_size,
            offset=offset,
        )
        response = beta_client.run_report(request)
        page_rows = parse_response_rows(response)

        if not page_rows:
            break

        for row in page_rows:
            report_date = ga4_date_to_iso(row.get("date", ""))
            value = row.get(dimension_name, "") or "(not set)"
            if report_date in by_date and len(by_date[report_date]) < max(config.personalized_top_n, 1):
                by_date[report_date].append(
                    {
                        "label": label,
                        "value": value,
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

    missing_dates = [date for date, items in by_date.items() if not items]
    if missing_dates:
        print(f"PERSONALIZED UX {label}: no returned rows for dates {missing_dates}")

    return by_date


def run_personalized_ux(app: AppConfig, report_dates: list[str]) -> dict[str, dict[str, list[dict]]]:
    """Return structured top values for each personalized dimension and date."""
    result = {report_date: {} for report_date in report_dates}
    for label, dimension_name in get_personalized_ux_dimensions():
        try:
            by_date = run_dimension_session_report(app, label, dimension_name, report_dates)
            for report_date in report_dates:
                result[report_date][label] = by_date.get(report_date, [])[: max(config.personalized_top_n, 1)]
        except Exception as error:
            status, error_text = classify_api_error(error)
            print(f"PERSONALIZED UX {label} {status} for {app.app_name}: {error_text}")
            for report_date in report_dates:
                result[report_date][label] = []
    return result


def get_remote_config_session():
    global remote_config_session
    if remote_config_session is None:
        remote_config_session = AuthorizedSession(credentials)
    return remote_config_session


def get_firebase_remote_config_template(firebase_project_id: str) -> dict:
    project_id = str(firebase_project_id or "").strip()
    if not project_id:
        raise ValueError("Firebase Project ID is empty in Apps Config.")
    if project_id in remote_config_template_cache:
        return remote_config_template_cache[project_id]
    project_path = f"projects/{project_id}"
    url = f"{config.firebase_remote_config_api_base}/{project_path}/remoteConfig"
    params = {}
    namespace = str(config.remote_config_namespace or "").strip()
    if namespace:
        params["name"] = f"{project_path}/namespaces/{namespace}/remoteConfig"
    response = get_remote_config_session().get(
        url,
        params=params,
        headers={"Accept-Encoding": "gzip"},
        timeout=config.firebase_remote_config_timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Firebase Remote Config API error {response.status_code}: {response.text}")
    template = response.json()
    remote_config_template_cache[project_id] = template
    return template


def iter_remote_config_parameters(template: dict):
    for key, parameter in (template.get("parameters", {}) or {}).items():
        yield key, parameter, ""
    for group_name, group_data in (template.get("parameterGroups", {}) or {}).items():
        for key, parameter in (group_data.get("parameters", {}) or {}).items():
            yield key, parameter, group_name


def format_remote_config_value(value_object) -> str:
    if value_object in [None, ""]:
        return ""
    if isinstance(value_object, dict):
        if "value" in value_object:
            return str(value_object.get("value", ""))
        if value_object.get("useInAppDefault") is True:
            return "Use in-app default"
    return json.dumps(value_object, ensure_ascii=False, sort_keys=True)


def get_parameter_values(parameter: dict) -> list[dict]:
    values = []
    if "defaultValue" in parameter:
        values.append({"source": "Default", "condition": "", "value": format_remote_config_value(parameter.get("defaultValue"))})
    for condition_name, value_object in (parameter.get("conditionalValues", {}) or {}).items():
        values.append({"source": "Conditional", "condition": condition_name, "value": format_remote_config_value(value_object)})
    return values


def find_remote_config_parameter(template: dict, parameter_key: str) -> tuple[str, dict, str] | tuple[str, None, str]:
    wanted = str(parameter_key or "").strip()
    if not wanted:
        return "", None, ""
    wanted_lower = wanted.lower()
    for key, parameter, group_name in iter_remote_config_parameters(template):
        if key == wanted:
            return key, parameter, group_name
    for key, parameter, group_name in iter_remote_config_parameters(template):
        key_lower = key.lower()
        if wanted_lower in key_lower or key_lower in wanted_lower:
            return key, parameter, group_name
    return wanted, None, ""


def get_remote_config_condition_map(template: dict) -> dict[str, str]:
    """Return Remote Config condition name -> expression."""
    return {
        str(condition.get("name", "")): str(condition.get("expression", ""))
        for condition in (template.get("conditions", []) or [])
        if condition.get("name")
    }


def extract_fetch_percent(condition_expression: str) -> str:
    """Best-effort percentage extraction from a Remote Config condition.

    The Remote Config template API does not expose the console's Fetch % as a
    dedicated parameter field. Default values apply to the remaining audience,
    so they are represented as 100%. For conditional values, a percentage is
    returned only when it can be inferred from the condition expression.
    """
    expression = str(condition_expression or "")
    if not expression:
        return ""

    # Examples commonly seen in Remote Config percentile conditions:
    # percent <= 50, percent < 25, percent in [10, 40].
    range_match = re.search(
        r"percent\s+in\s*\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]",
        expression,
        flags=re.IGNORECASE,
    )
    if range_match:
        start = float(range_match.group(1))
        end = float(range_match.group(2))
        value = max(end - start, 0)
        return f"{value:g}%"

    threshold_match = re.search(
        r"percent\s*(?:<=|<|==)\s*(\d+(?:\.\d+)?)",
        expression,
        flags=re.IGNORECASE,
    )
    if threshold_match:
        return f"{float(threshold_match.group(1)):g}%"

    return ""


def format_last_published(version: dict) -> str:
    """Format template-level publication metadata for sheet output."""
    update_time = str(version.get("updateTime", "") or "").strip()
    update_user = version.get("updateUser", {}) or {}
    publisher = str(update_user.get("email", "") or update_user.get("name", "") or "").strip()
    if publisher and update_time:
        return f"{publisher} | {update_time}"
    return publisher or update_time


def build_remote_parameter_rows(
    parameter_key: str,
    parameter: dict | None,
    group_name: str,
    condition_map: dict[str, str],
    last_published: str,
    missing_message: str = "",
) -> list[dict]:
    """Build structured rows matching Name/Condition/Value/Fetch %/Published."""
    if parameter is None:
        return [
            {
                "name": parameter_key,
                "condition": "",
                "value": missing_message or "Parameter not found",
                "fetch_percent": "",
                "last_published": last_published,
            }
        ]

    rows: list[dict] = []
    for value_row in get_parameter_values(parameter):
        value = value_row.get("value", "")
        if value == "":
            continue

        condition_name = str(value_row.get("condition", "") or "").strip()
        is_default = value_row.get("source") == "Default"
        condition_label = "Default value" if is_default else condition_name
        fetch_percent = "100%" if is_default else extract_fetch_percent(condition_map.get(condition_name, ""))
        display_name = parameter_key if not group_name else f"{group_name}/{parameter_key}"

        rows.append(
            {
                "name": display_name,
                "condition": condition_label,
                "value": value,
                "fetch_percent": fetch_percent,
                "last_published": last_published,
            }
        )

    if rows:
        return rows

    return [
        {
            "name": parameter_key,
            "condition": "",
            "value": "No configured value",
            "fetch_percent": "",
            "last_published": last_published,
        }
    ]


def find_iap_parameters(template: dict, explicit_key: str) -> list[tuple[str, dict, str]]:
    matches = []
    seen = set()
    if explicit_key:
        key, parameter, group_name = find_remote_config_parameter(template, explicit_key)
        if parameter is not None:
            matches.append((key, parameter, group_name))
            seen.add((group_name, key))
    keywords = [k.lower() for k in split_csv(config.iap_screen_parameter_keywords)]
    if explicit_key and explicit_key.lower() not in keywords:
        keywords.insert(0, explicit_key.lower())
    for key, parameter, group_name in iter_remote_config_parameters(template):
        unique = (group_name, key)
        if unique in seen:
            continue
        key_lower = key.lower()
        if any(keyword and keyword in key_lower for keyword in keywords):
            matches.append((key, parameter, group_name))
            seen.add(unique)
    return matches


def get_remote_config_ab_rows(app: AppConfig) -> dict:
    empty = {
        "time_capping_rows": [
            {
                "name": app.time_capping_parameter or config.time_capping_parameter,
                "condition": "",
                "value": "Missing Firebase Project ID in Apps Config.",
                "fetch_percent": "",
                "last_published": "",
            }
        ],
        "iap_screen_rows": [
            {
                "name": app.iap_screen_parameter or config.iap_screen_parameter,
                "condition": "",
                "value": "Missing Firebase Project ID in Apps Config.",
                "fetch_percent": "",
                "last_published": "",
            }
        ],
    }
    if not app.firebase_project_id:
        return empty

    try:
        template = get_firebase_remote_config_template(app.firebase_project_id)
        version = template.get("version", {}) or {}
        last_published = format_last_published(version)
        condition_map = get_remote_config_condition_map(template)

        time_key = app.time_capping_parameter or config.time_capping_parameter
        matched_time_key, time_param, time_group = find_remote_config_parameter(template, time_key)
        time_capping_rows = build_remote_parameter_rows(
            matched_time_key or time_key,
            time_param,
            time_group,
            condition_map,
            last_published,
            missing_message=f"Parameter not found: {time_key}",
        )

        iap_key = app.iap_screen_parameter or config.iap_screen_parameter
        iap_matches = find_iap_parameters(template, iap_key)
        if not iap_matches:
            iap_screen_rows = build_remote_parameter_rows(
                iap_key,
                None,
                "",
                condition_map,
                last_published,
                missing_message=(
                    f"No IAP/paywall config found for {iap_key} or configured IAP keywords."
                ),
            )
        else:
            iap_screen_rows = []
            for key, parameter, group_name in iap_matches[:20]:
                iap_screen_rows.extend(
                    build_remote_parameter_rows(
                        key,
                        parameter,
                        group_name,
                        condition_map,
                        last_published,
                    )
                )

        return {
            "time_capping_rows": time_capping_rows,
            "iap_screen_rows": iap_screen_rows,
        }
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"FIREBASE REMOTE CONFIG {status} for {app.app_name}: {error_text}")
        message = f"{status}: {error_text}"
        return {
            "time_capping_rows": [
                {
                    "name": app.time_capping_parameter or config.time_capping_parameter,
                    "condition": "",
                    "value": message,
                    "fetch_percent": "",
                    "last_published": "",
                }
            ],
            "iap_screen_rows": [
                {
                    "name": app.iap_screen_parameter or config.iap_screen_parameter,
                    "condition": "",
                    "value": message,
                    "fetch_percent": "",
                    "last_published": "",
                }
            ],
        }



def get_event_metric(event_data: dict, report_date: str, event_name: str) -> dict:
    return event_data.get((report_date, event_name), {}) or {}


def pick_first_available_event(event_data: dict, report_date: str, event_names: list[str]) -> tuple[str, dict]:
    for event_name in event_names:
        data = get_event_metric(event_data, report_date, event_name)
        if to_float(data.get("active_users", 0)) or to_float(data.get("event_count", 0)):
            return event_name, data
    if event_names:
        return event_names[0], get_event_metric(event_data, report_date, event_names[0])
    return "", {}


def get_home_metrics_for_date(report_date: str, app: AppConfig, event_data: dict, home_data: dict) -> tuple[str, int, int, str]:
    home_event_names = split_csv(app.home_event_names)
    if home_event_names:
        home_event, home_event_data = pick_first_available_event(event_data, report_date, home_event_names)
        return (
            home_event,
            to_number(home_event_data.get("active_users", 0)),
            to_number(home_event_data.get("event_count", 0)),
            f"eventName = {home_event}",
        )

    screen_data = home_data.get(report_date, {})
    return (
        "screen_view",
        to_number(screen_data.get("active_users", 0)),
        to_number(screen_data.get("event_count", 0)),
        f"eventName = screen_view AND {app.screen_field} contains {app.home_screen_name}",
    )

AUDIENCE_SEGMENTS = [
    ("All Users", "all_users"),
    ("US Users", "us_users"),
    ("Direct Traffic", "direct_traffic"),
    ("Paid Traffic", "paid_traffic"),
    ("Mobile Traffic", "mobile_traffic"),
    ("Tablet Traffic", "tablet_traffic"),
]

PERSONALIZED_COLUMN_SPECS = [
    ("Country", "country"),
    ("Language", "language"),
    ("Device Category", "device_category"),
    ("Operating System", "operating_system"),
    ("App Version", "app_version"),
    ("First User Medium", "first_user_medium"),
    ("Top Screens / Screen Class", "screen_class"),
]

# These events backed removed output columns and must not be included in the
# shared GA4 event report, even when they are accidentally listed as feature
# events in an existing Apps Config row or repository variable.
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


def build_output_headers() -> list[str]:
    headers = ["Package Name", "Date"]

    for _, slug in AUDIENCE_SEGMENTS:
        headers.extend(
            [
                f"audience_{slug}/users",
                f"audience_{slug}/sessions",
                f"audience_{slug}/er",
            ]
        )

    headers.extend(
        [
            "funnel_app_open_users",
            "funnel_app_open_events",
            "funnel_home_users",
            "funnel_home_events_views",
            "funnel_possible_drop_off",
            "funnel_home_reach_rate",
            "funnel_ad_impression/events",
            "funnel_ad_impression/users",
            "funnel_in_app_purchase/events",
            "funnel_in_app_purchase/users",
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
            "time_analysis_active_users",
            "time_analysis_new_users",
            "time_analysis_sessions",
            "time_analysis_engaged_sessions",
            "time_analysis_engagement_rate",
            "time_analysis_avg_session_duration",
            "time_analysis_sessions_per_active_user",
            "time_analysis_total_engagement_time",
            "retention_first_session_cohort_total_users",
            "retention_d1_first_session_active_users",
            "retention_d1_first_session_retention",
            "retention_d7_first_session_active_users",
            "retention_d7_first_session_retention",
            # Personalized categories are independent side-by-side row groups.
            # Values (country, language, version, screen, etc.) stay in cells,
            # never in fixed or rank-based column names.
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


def set_audience_columns(row: dict, segments: list[dict]):
    segment_map = {item.get("segment", ""): item for item in segments}
    for segment_name, slug in AUDIENCE_SEGMENTS:
        item = segment_map.get(segment_name, {})
        row[f"audience_{slug}/users"] = item.get("active", 0)
        row[f"audience_{slug}/sessions"] = item.get("sessions", 0)
        row[f"audience_{slug}/er"] = item.get("engagement", "0%")


def set_funnel_columns(
    row: dict,
    report_date: str,
    app: AppConfig,
    event_data: dict,
    home_data: dict,
):
    app_open_event, app_open_data = pick_first_available_event(
        event_data,
        report_date,
        split_csv(app.app_open_event_names),
    )
    app_open_users = to_number(app_open_data.get("active_users", 0))
    app_open_events = to_number(app_open_data.get("event_count", 0))
    _, home_users, home_views, _ = get_home_metrics_for_date(
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

    for event_name in ("ad_impression", "in_app_purchase"):
        data = get_event_metric(event_data, report_date, event_name)
        row[f"funnel_{event_name}/events"] = to_number(data.get("event_count", 0))
        row[f"funnel_{event_name}/users"] = to_number(data.get("active_users", 0))


def set_ab_parameter_columns(row: dict, prefix: str, item: dict):
    row[f"{prefix}_name"] = item.get("name", "")
    row[f"{prefix}_condition"] = item.get("condition", "")
    row[f"{prefix}_value"] = item.get("value", "")
    row[f"{prefix}_fetch_percent"] = item.get("fetch_percent", "")
    row[f"{prefix}_last_published"] = item.get("last_published", "")


def set_time_and_retention_columns(row: dict, metrics: dict, retention: dict):
    row["time_analysis_active_users"] = metrics.get("Active Users", 0)
    row["time_analysis_new_users"] = metrics.get("New Users", 0)
    row["time_analysis_sessions"] = metrics.get("Sessions", 0)
    row["time_analysis_engaged_sessions"] = metrics.get("Engaged Sessions", 0)
    row["time_analysis_engagement_rate"] = metrics.get("Engagement Rate", "0%")
    row["time_analysis_avg_session_duration"] = metrics.get("Avg Session Duration", "0m 0s")
    row["time_analysis_sessions_per_active_user"] = metrics.get("Sessions Per Active User", 0)
    row["time_analysis_total_engagement_time"] = metrics.get("Total Engagement Time", "0m 0s")

    row["retention_first_session_cohort_total_users"] = retention.get("Cohort Total Users", "Not available yet")
    row["retention_d1_first_session_active_users"] = retention.get("D1 Active Users", "Not available yet")
    row["retention_d1_first_session_retention"] = retention.get("D1 Retention", "Not available yet")
    row["retention_d7_first_session_active_users"] = retention.get("D7 Active Users", "Not available yet")
    row["retention_d7_first_session_retention"] = retention.get("D7 Retention", "Not available yet")


def set_personalized_columns(row: dict, slug: str, item: dict):
    """Set one Personalized UX item in its category-specific column group.

    The actual value remains data in the category column. For example, Ukraine
    is written to personalized_category_country; it is never embedded in a
    header. Each category is independent and can occupy the same output row as
    the first item from another category.
    """
    row[f"personalized_category_{slug}"] = item.get("value", "")
    row[f"personalized_{slug}_users"] = item.get("active", "")
    row[f"personalized_{slug}_sessions"] = item.get("sessions", "")
    row[f"personalized_{slug}_er"] = item.get("engagement", "")
    row[f"personalized_{slug}_avg"] = item.get("avg", "")


def build_rows_for_app(app: AppConfig, report_dates: list[str], package_name: str) -> list[list]:
    print(f"Processing: {app.app_name} / {app.property_id} / {report_dates[0]} to {report_dates[-1]}")

    feature_events = split_csv(app.feature_event_names)
    app_open_events = split_csv(app.app_open_event_names)
    home_event_names = split_csv(app.home_event_names)
    required_funnel_events = ["ad_impression", "in_app_purchase"]
    event_names = [
        event_name
        for event_name in split_csv(
            ",".join(
                feature_events
                + required_funnel_events
                + app_open_events
                + home_event_names
            )
        )
        if event_name.lower() not in EXCLUDED_GA4_EVENT_NAMES
    ]

    daily_metrics: dict[str, dict] = {}
    event_data: dict[tuple[str, str], dict] = {}
    home_data: dict[str, dict] = {}
    retention_data: dict[str, dict] = {}
    audience_segments = {report_date: [] for report_date in report_dates}
    personalized_ux: dict[str, dict[str, list[dict]]] = {report_date: {} for report_date in report_dates}
    remote_ab = get_remote_config_ab_rows(app)

    try:
        daily_metrics = run_daily_metrics_report(app)
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"DAILY METRICS {status} for {app.app_name}: {error_text}")
    try:
        event_data = run_event_report(app, event_names)
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"EVENTS {status} for {app.app_name}: {error_text}")
    if not home_event_names:
        try:
            home_data = run_home_screen_report(app)
        except Exception as error:
            status, error_text = classify_api_error(error)
            print(f"HOME SCREEN {status} for {app.app_name}: {error_text}")
    try:
        retention_data = run_retention_report(app, report_dates)
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"RETENTION {status} for {app.app_name}: {error_text}")
    try:
        audience_segments = run_all_audience_segments(app, report_dates)
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"AUDIENCE SEGMENTS {status} for {app.app_name}: {error_text}")
    try:
        personalized_ux = run_personalized_ux(app, report_dates)
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"PERSONALIZED UX {status} for {app.app_name}: {error_text}")
    rows: list[list] = []
    for report_date in report_dates:
        # Personalized UX categories are independent datasets. They are
        # compacted side by side by row index so every category uses its own
        # column group without creating stacked blocks or artificial gaps.
        personalized_for_date = personalized_ux.get(report_date, {})
        personalized_groups: dict[str, list[dict]] = {
            slug: personalized_for_date.get(category, [])
            for category, slug in PERSONALIZED_COLUMN_SPECS
        }

        time_capping_items = remote_ab.get("time_capping_rows", []) or []
        iap_screen_items = remote_ab.get("iap_screen_rows", []) or []
        row_count = max(
            [1, len(time_capping_items), len(iap_screen_items)]
            + [len(items) for items in personalized_groups.values()]
        )

        for index in range(row_count):
            output_row = {header: "" for header in OUTPUT_HEADERS}
            output_row["Package Name"] = package_name
            output_row["Date"] = report_date

            # Store date-level summary metrics only once, on the first compact
            # row for this package and date.
            if index == 0:
                set_audience_columns(output_row, audience_segments.get(report_date, []))
                set_funnel_columns(output_row, report_date, app, event_data, home_data)
                set_time_and_retention_columns(
                    output_row,
                    daily_metrics.get(report_date, {}),
                    retention_data.get(report_date, {}),
                )

            # Time Capping and IAP parameter records are independent lists.
            # They are aligned side by side by row index only to avoid gaps.
            if index < len(time_capping_items):
                set_ab_parameter_columns(output_row, "time_capping", time_capping_items[index])
            if index < len(iap_screen_items):
                set_ab_parameter_columns(output_row, "iap_screen", iap_screen_items[index])

            # Fill each Personalized UX category independently at the same
            # row index. Country row 1, Language row 1, Device Category row 1,
            # and so on appear side by side without implying a relationship.
            for _, slug in PERSONALIZED_COLUMN_SPECS:
                items = personalized_groups.get(slug, [])
                if index < len(items):
                    set_personalized_columns(output_row, slug, items[index])

            rows.append([output_row.get(header, "") for header in OUTPUT_HEADERS])
    return rows


def main():
    print("Reading app list from Apps Config sheet...")
    apps = read_apps_config()
    report_dates = get_report_dates()
    print(f"Total enabled apps found: {len(apps)}")
    print(f"Report date range: {report_dates[0]} to {report_dates[-1]}")

    rows = [OUTPUT_HEADERS]

    for app in apps:
        package_name = fetch_ga4_package_name(app)
        if package_name:
            print(f"Package name found for {app.app_name}: {package_name}")
        else:
            print(f"Package name not found for {app.app_name}; final Package Name cell will be blank.")
        rows.extend(build_rows_for_app(app, report_dates, package_name))

    write_sheet(config.merged_sheet, rows)


if __name__ == "__main__":
    main()

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote

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
    daily_notification_parameters: str
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
notification_api_session = None
package_name_cache: dict[str, str] = {}
remote_config_template_cache: dict[str, dict] = {}
fcm_delivery_cache: dict[tuple[str, str], dict] = {}
firebase_android_apps_cache: dict[str, list[dict]] = {}

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
                daily_notification_parameters=(row[9].strip() if len(row) > 9 else ""),
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


def lines_to_cell(lines: list[str], max_lines: int = 50) -> str:
    clean = [str(line).strip() for line in lines if str(line).strip()]
    if len(clean) > max_lines:
        clean = clean[:max_lines] + [f"... {len(clean) - max_lines} more rows trimmed"]
    return "\n".join(clean)


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


def remote_config_event_filter() -> FilterExpression:
    keywords = [
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
    return or_filter([contains_filter("eventName", keyword) for keyword in keywords])


def run_remote_config_events_report(app: AppConfig, report_dates: list[str]) -> dict[str, dict[str, dict]]:
    """Return Remote Config-related GA4 events keyed by date and event name."""
    result: dict[str, dict[str, dict]] = {report_date: {} for report_date in report_dates}
    try:
        request = RunReportRequest(
            property=f"properties/{app.property_id}",
            date_ranges=[DateRange(start_date=config.start_date, end_date=config.end_date)],
            dimensions=[Dimension(name="date"), Dimension(name="eventName")],
            metrics=[Metric(name="activeUsers"), Metric(name="eventCount")],
            dimension_filter=remote_config_event_filter(),
            order_bys=[date_order(), metric_order("eventCount")],
            keep_empty_rows=False,
            limit=100000,
        )
        response = beta_client.run_report(request)
        for row in parse_response_rows(response):
            report_date = ga4_date_to_iso(row.get("date", ""))
            event_name = row.get("eventName", "")
            if report_date in result and event_name:
                result[report_date][event_name] = {
                    "users": to_number(row.get("activeUsers", 0)),
                    "events": to_number(row.get("eventCount", 0)),
                }
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"REMOTE CONFIG EVENTS {status} for {app.app_name}: {error_text}")
    return result


def run_remote_config_app_versions(app: AppConfig, report_dates: list[str]) -> dict[str, list[dict]]:
    """Return the top app versions independently for every report date."""
    result: dict[str, list[dict]] = {report_date: [] for report_date in report_dates}
    try:
        request = RunReportRequest(
            property=f"properties/{app.property_id}",
            date_ranges=[DateRange(start_date=config.start_date, end_date=config.end_date)],
            dimensions=[Dimension(name="date"), Dimension(name="appVersion")],
            metrics=[Metric(name="activeUsers"), Metric(name="sessions"), Metric(name="engagementRate")],
            order_bys=[date_order(), metric_order("activeUsers")],
            keep_empty_rows=False,
            limit=100000,
        )
        response = beta_client.run_report(request)
        for row in parse_response_rows(response):
            report_date = ga4_date_to_iso(row.get("date", ""))
            version_limit = config.remote_config_app_version_limit
            if report_date in result and (version_limit <= 0 or len(result[report_date]) < version_limit):
                result[report_date].append(
                    {
                        "version": row.get("appVersion", "") or "(not set)",
                        "users": to_number(row.get("activeUsers", 0)),
                        "sessions": to_number(row.get("sessions", 0)),
                        "er": percent(row.get("engagementRate", 0)),
                    }
                )
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"REMOTE CONFIG APP VERSION {status} for {app.app_name}: {error_text}")
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


def extract_experiment_ids(value: str) -> str:
    text = str(value or "")
    ids = []
    for pattern in [r"experiment[_-]?id['\"\s:=]+([A-Za-z0-9_.:-]+)", r"variant[_-]?id['\"\s:=]+([A-Za-z0-9_.:-]+)"]:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            ids.append(match.group(1))
    return ", ".join(dict.fromkeys(ids))


def summarize_parameter_values(parameter_key: str, parameter: dict, group_name: str = "") -> list[str]:
    value_type = parameter.get("valueType", "") if parameter else ""
    lines = []
    for value_row in get_parameter_values(parameter or {}):
        value = value_row["value"]
        if value == "":
            continue
        condition = f" / {value_row['condition']}" if value_row.get("condition") else ""
        group = f" / Group {group_name}" if group_name else ""
        exp = extract_experiment_ids(value)
        exp_text = f" / Experiment {exp}" if exp else ""
        lines.append(f"{parameter_key}{group} | {value_row['source']}{condition} | {value_type} | {value}{exp_text}")
    return lines


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


def get_notification_parameter_keys(app: AppConfig) -> list[str]:
    return split_csv(app.daily_notification_parameters) or split_csv(config.notification_parameter_keywords)


def is_notification_parameter_key(parameter_key: str, explicit_or_keywords: list[str]) -> bool:
    key_lower = parameter_key.lower()
    return any(item.lower() in key_lower for item in explicit_or_keywords if item)


def try_json(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in [text, text.replace("'", '"')]:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def find_key_value_recursive(data, keys: list[str]) -> str:
    if isinstance(data, dict):
        for wanted in keys:
            for key, value in data.items():
                if wanted in str(key).lower():
                    if isinstance(value, (str, int, float)):
                        return str(value)
                    return json.dumps(value, ensure_ascii=False)
        for value in data.values():
            found = find_key_value_recursive(value, keys)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_key_value_recursive(value, keys)
            if found:
                return found
    return ""


def extract_notification_details(raw_value: str) -> str:
    text = str(raw_value or "")
    parsed = try_json(text)
    if parsed is not None:
        title = find_key_value_recursive(parsed, ["title", "heading", "subject"])
        body = find_key_value_recursive(parsed, ["body", "message", "content", "text"])
        send_time = find_key_value_recursive(parsed, ["send_time", "sendtime", "time", "hour", "schedule"])
        days = find_key_value_recursive(parsed, ["days", "repeat", "weekday"])
        parts = []
        if title:
            parts.append(f"Title: {title}")
        if body:
            parts.append(f"Body: {body}")
        if send_time:
            parts.append(f"Time: {send_time}")
        if days:
            parts.append(f"Days: {days}")
        if parts:
            return ", ".join(parts)
    title_match = re.search(r"(?:title|heading)\s*[:=]\s*([^|,;]+)", text, flags=re.IGNORECASE)
    body_match = re.search(r"(?:body|message|content)\s*[:=]\s*([^|;]+)", text, flags=re.IGNORECASE)
    time_match = re.search(r"(?:time|send_time|schedule)\s*[:=]\s*([0-9]{1,2}[:.]?[0-9]{0,2}\s*(?:am|pm)?|[^|,;]+)", text, flags=re.IGNORECASE)
    parts = []
    if title_match:
        parts.append(f"Title: {title_match.group(1).strip()}")
    if body_match:
        parts.append(f"Body: {body_match.group(1).strip()}")
    if time_match:
        parts.append(f"Time: {time_match.group(1).strip()}")
    return ", ".join(parts) if parts else text[:300]


def summarize_daily_notifications_from_template(app: AppConfig, template: dict) -> str:
    keys_or_keywords = get_notification_parameter_keys(app)
    lines = []
    for key, parameter, group_name in iter_remote_config_parameters(template):
        if not is_notification_parameter_key(key, keys_or_keywords):
            continue
        for value_row in get_parameter_values(parameter):
            value = value_row["value"]
            if value == "":
                continue
            details = extract_notification_details(value)
            condition = f" / {value_row['condition']}" if value_row.get("condition") else ""
            group = f" / Group {group_name}" if group_name else ""
            lines.append(f"{key}{group} | {value_row['source']}{condition} | {details}")
    if not lines:
        return "No daily notification Remote Config parameter found. Add exact keys in Apps Config column J."
    return lines_to_cell(lines, 30)


def get_remote_config_static_summaries(app: AppConfig) -> dict:
    empty = {
        "remote_config_static": "Missing Firebase Project ID in Apps Config.",
        "time_capping": "Missing Firebase Project ID in Apps Config.",
        "iap_screen": "Missing Firebase Project ID in Apps Config.",
        "daily_notifications_static": "Missing Firebase Project ID in Apps Config.",
    }
    if not app.firebase_project_id:
        return empty
    try:
        template = get_firebase_remote_config_template(app.firebase_project_id)
        version = template.get("version", {}) or {}
        version_number = version.get("versionNumber", "")
        update_time = version.get("updateTime", "")
        total_parameters = sum(1 for _ in iter_remote_config_parameters(template))

        time_key = app.time_capping_parameter or config.time_capping_parameter
        matched_time_key, time_param, time_group = find_remote_config_parameter(template, time_key)
        if time_param is None:
            time_capping = f"Parameter not found: {time_key}"
        else:
            time_capping = lines_to_cell(summarize_parameter_values(matched_time_key, time_param, time_group), 25)

        iap_key = app.iap_screen_parameter or config.iap_screen_parameter
        iap_matches = find_iap_parameters(template, iap_key)
        if not iap_matches:
            iap_screen = f"No IAP/paywall config found for {iap_key} or configured IAP keywords."
        else:
            lines = []
            for key, parameter, group_name in iap_matches[:20]:
                lines.extend(summarize_parameter_values(key, parameter, group_name))
            iap_screen = lines_to_cell(lines, 40)

        daily_notifications = summarize_daily_notifications_from_template(app, template)
        remote_config_static = lines_to_cell(
            [
                f"Template Version: {version_number}" if version_number else "Template Version: blank",
                f"Last Published At: {update_time}" if update_time else "Last Published At: blank",
                f"Total Parameters: {total_parameters}",
            ]
        )
        return {
            "remote_config_static": remote_config_static,
            "time_capping": time_capping,
            "iap_screen": iap_screen,
            "daily_notifications_static": daily_notifications,
        }
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"FIREBASE REMOTE CONFIG {status} for {app.app_name}: {error_text}")
        message = f"{status}: {error_text}"
        return {
            "remote_config_static": message,
            "time_capping": message,
            "iap_screen": message,
            "daily_notifications_static": message,
        }



# =========================
# FCM NOTIFICATION DELIVERY
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


def get_percent(data: dict, key: str) -> str:
    value = data.get(key, "") if data else ""
    if value in [None, ""]:
        return ""
    try:
        return f"{round(float(value), 2)}%"
    except Exception:
        return str(value)


def looks_like_firebase_app_id(value: str) -> bool:
    value = str(value or "").strip()
    return re.match(r"^1:\d+:(android|ios|web):", value) is not None


def list_firebase_android_apps(project_identifier: str) -> list[dict]:
    project_identifier = str(project_identifier or "").strip()
    if not project_identifier:
        return []
    if project_identifier in firebase_android_apps_cache:
        return firebase_android_apps_cache[project_identifier]

    apps = []
    page_token = ""
    while True:
        parent = f"projects/{quote(project_identifier, safe='-')}"
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
            raise RuntimeError(f"Firebase Management API error {response.status_code}: {response.text}")
        payload = response.json()
        apps.extend(payload.get("apps", []) or [])
        page_token = payload.get("nextPageToken", "")
        if not page_token:
            break
    firebase_android_apps_cache[project_identifier] = apps
    return apps


def choose_firebase_android_app(app: AppConfig, apps: list[dict]) -> dict | None:
    if not apps:
        return None
    wanted = str(app.firebase_app_id or "").strip().lower()
    app_name = str(app.app_name or "").strip().lower()

    if wanted:
        for item in apps:
            if str(item.get("appId", "")).lower() == wanted:
                return item
        for item in apps:
            if str(item.get("packageName", "")).lower() == wanted:
                return item

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
        raise ValueError("Firebase Project ID is empty in Apps Config.")

    if not configured_app_id:
        android_apps = list_firebase_android_apps(project_identifier)
        selected = choose_firebase_android_app(app, android_apps)
        if selected:
            return (
                selected.get("projectId") or project_identifier,
                selected.get("appId", ""),
                "Firebase App ID was empty; auto-resolved from Firebase Android apps list.",
            )
        raise ValueError("Firebase App ID is empty in Apps Config and could not be auto-resolved from Firebase Android apps.")

    if looks_like_firebase_app_id(configured_app_id):
        return project_identifier, configured_app_id, "Using Firebase App ID from Apps Config."

    android_apps = list_firebase_android_apps(project_identifier)
    selected = choose_firebase_android_app(app, android_apps)
    if selected:
        return (
            selected.get("projectId") or project_identifier,
            selected.get("appId", ""),
            f"Firebase App ID auto-resolved from Apps Config value '{configured_app_id}'.",
        )

    raise ValueError(
        "Invalid Firebase App ID. Apps Config Firebase App ID should be Android Firebase App ID like "
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
        raise RuntimeError(f"FCM Data API error {response.status_code}: {response.text}")
    return response.json()


def get_fcm_delivery_data_for_app(app: AppConfig) -> dict:
    project_id, app_id, resolution_note = resolve_fcm_project_and_app_id(app)
    cache_key = (project_id, app_id)
    if cache_key in fcm_delivery_cache:
        cached_payload = dict(fcm_delivery_cache[cache_key])
        cached_payload["_resolution_note"] = resolution_note + " Reused cached FCM delivery response."
        return cached_payload

    try:
        payload = request_fcm_delivery_data(project_id, app_id, encode_colons=False)
        payload["_resolution_note"] = resolution_note
        payload["_resolved_project_id"] = project_id
        payload["_resolved_app_id"] = app_id
        fcm_delivery_cache[cache_key] = dict(payload)
        return payload
    except RuntimeError as error:
        error_text = str(error)
        if "400" in error_text or "INVALID_ARGUMENT" in error_text:
            try:
                payload = request_fcm_delivery_data(project_id, app_id, encode_colons=True)
                payload["_resolution_note"] = resolution_note + " Retried with encoded Firebase App ID."
                payload["_resolved_project_id"] = project_id
                payload["_resolved_app_id"] = app_id
                fcm_delivery_cache[cache_key] = dict(payload)
                return payload
            except RuntimeError:
                pass

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
                fcm_delivery_cache[(normalized_project_id, normalized_app_id)] = dict(payload)
                fcm_delivery_cache[cache_key] = dict(payload)
                return payload
        except Exception:
            pass

        raise RuntimeError(
            error_text
            + " | Check Apps Config: Firebase Project ID should be the real Firebase project ID, "
            "and Firebase App ID should be the Android appId from Firebase project settings."
        )


def fcm_delivery_to_lines(delivery: dict) -> list[str]:
    data = delivery.get("data", {}) or {}
    outcome = data.get("messageOutcomePercents", {}) or {}
    performance = data.get("deliveryPerformancePercents", {}) or {}
    insight = data.get("messageInsightPercents", {}) or {}
    proxy = data.get("proxyNotificationInsightPercents", {}) or {}
    label = delivery.get("analyticsLabel", "") or "(no analytics label)"

    return [
        f"Analytics Label: {label}",
        f"Messages Accepted: {data.get('countMessagesAccepted', '')}",
        f"Notifications Accepted: {data.get('countNotificationsAccepted', '')}",
        f"Delivered: {get_percent(outcome, 'delivered')}",
        f"Pending: {get_percent(outcome, 'pending')}",
        f"Collapsed: {get_percent(outcome, 'collapsed')}",
        f"Dropped - Too Many Pending: {get_percent(outcome, 'droppedTooManyPendingMessages')}",
        f"Dropped - App Force Stopped: {get_percent(outcome, 'droppedAppForceStopped')}",
        f"Dropped - Device Inactive: {get_percent(outcome, 'droppedDeviceInactive')}",
        f"Dropped - TTL Expired: {get_percent(outcome, 'droppedTtlExpired')}",
        f"Delivered No Delay: {get_percent(performance, 'deliveredNoDelay')}",
        f"Delayed - Device Offline: {get_percent(performance, 'delayedDeviceOffline')}",
        f"Delayed - Device Doze: {get_percent(performance, 'delayedDeviceDoze')}",
        f"Delayed - Message Throttled: {get_percent(performance, 'delayedMessageThrottled')}",
        f"Priority Lowered: {get_percent(insight, 'priorityLowered')}",
        f"Proxied: {get_percent(proxy, 'proxied')}",
    ]


def build_fcm_delivery_fields_by_date(app: AppConfig, report_dates: list[str]) -> dict[str, dict]:
    """Return one FCM delivery record per date without creating another sheet.

    If the API returns multiple analytics labels for a date, the unlabelled
    aggregate is preferred because it matches the existing report. Otherwise,
    the first returned label is used.
    """
    result: dict[str, dict] = {report_date: {} for report_date in report_dates}
    try:
        response = get_fcm_delivery_data_for_app(app)
        delivery_rows = response.get("androidDeliveryData", []) or []
        grouped: dict[str, list[dict]] = {report_date: [] for report_date in report_dates}
        for delivery in delivery_rows:
            delivery_date = format_fcm_date(delivery.get("date", {}) or {})
            if delivery_date in grouped:
                grouped[delivery_date].append(delivery)

        for report_date, items in grouped.items():
            if not items:
                continue
            selected = next(
                (item for item in items if not str(item.get("analyticsLabel", "") or "").strip()),
                items[0],
            )
            data = selected.get("data", {}) or {}
            outcome = data.get("messageOutcomePercents", {}) or {}
            performance = data.get("deliveryPerformancePercents", {}) or {}
            label = selected.get("analyticsLabel", "") or "(no analytics label)"
            result[report_date] = {
                "firebase_analytics_label": label,
                "firebase_messages_accepted": to_number(data.get("countMessagesAccepted", 0)),
                "firebase_notifications_accepted": to_number(data.get("countNotificationsAccepted", 0)),
                "firebase_delivered": get_percent(outcome, "delivered"),
                "firebase_pending": get_percent(outcome, "pending"),
                "firebase_dropped_app_force_stopped": get_percent(outcome, "droppedAppForceStopped"),
                "firebase_dropped_device_inactive": get_percent(outcome, "droppedDeviceInactive"),
                "firebase_delivered_no_delay": get_percent(performance, "deliveredNoDelay"),
                "firebase_delayed_device_offline": get_percent(performance, "delayedDeviceOffline"),
            }
        return result
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"FCM DELIVERY {status} for {app.app_name}: {error_text}")
        return result


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

NOTIFICATION_COLUMNS = [
    "notification_receive",
    "notification_foreground",
    "notification_open",
    "notification_dismiss",
]

FCM_COLUMNS = [
    "firebase_analytics_label",
    "firebase_messages_accepted",
    "firebase_notifications_accepted",
    "firebase_delivered",
    "firebase_pending",
    "firebase_dropped_app_force_stopped",
    "firebase_dropped_device_inactive",
    "firebase_delivered_no_delay",
    "firebase_delayed_device_offline",
]

AUDIENCE_SEGMENTS = [
    ("All Users", "all_users"),
    ("US Users", "us_users"),
    ("Direct Traffic", "direct_traffic"),
    ("Paid Traffic", "paid_traffic"),
    ("Mobile Traffic", "mobile_traffic"),
    ("Tablet Traffic", "tablet_traffic"),
]

REMOTE_EVENT_COLUMNS = [
    "dn_rc_inter_clicked",
    "dn_rc_inter_displayed",
    "dn_rc_inter_loaded",
    "dn_rc_inter_requested",
    "dn_rc_inter_dismissed",
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


def build_output_headers() -> list[str]:
    headers = ["Package Name", "Date"]

    # GA4 notification events are split into numeric Events and Users columns.
    for event_name in NOTIFICATION_COLUMNS:
        headers.extend(
            [
                f"{event_name}_Events",
                f"{event_name}_USERS",
            ]
        )

    # Firebase delivery, audience, funnel, A/B, time analysis, and retention
    # columns retain their previously approved names.
    headers.extend(FCM_COLUMNS)

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
            "remote_template_version",
            "remote_last_published_at",
            "remote_total_parameters",
        ]
    )

    for event_name in REMOTE_EVENT_COLUMNS:
        headers.extend(
            [
                f"remote_{event_name}/users",
                f"remote_{event_name}/events",
            ]
        )

    # App-version values are row keys, not rank-specific columns.
    headers.extend(
        [
            "remote_app_version",
            "remote_app_version_users",
            "remote_app_version_sessions",
            "remote_app_version_er",
            "A/B Test on Time Capping",
            "A/B Test on IAPs Screen",
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
            # Personalized values are stored as actual row keys. This avoids
            # fixed columns such as screen_MainActivity or version_1.0.20.
            "personalized_category",
            "personalized_key",
            "personalized_users",
            "personalized_sessions",
            "personalized_er",
            "personalized_avg",
        ]
    )

    if len(headers) != len(set(headers)):
        duplicates = sorted({name for name in headers if headers.count(name) > 1})
        raise ValueError(f"Duplicate output headers found: {duplicates}")
    return headers


OUTPUT_HEADERS = build_output_headers()


def parse_remote_static_fields(static_summary: str) -> dict[str, object]:
    fields: dict[str, object] = {
        "remote_template_version": "",
        "remote_last_published_at": "",
        "remote_total_parameters": "",
    }
    for raw_line in str(static_summary or "").splitlines():
        line = raw_line.strip()
        if line.startswith("Template Version:"):
            fields["remote_template_version"] = line.split(":", 1)[1].strip()
        elif line.startswith("Last Published At:"):
            fields["remote_last_published_at"] = line.split(":", 1)[1].strip()
        elif line.startswith("Total Parameters:"):
            value = line.split(":", 1)[1].strip()
            fields["remote_total_parameters"] = to_number(value)
    return fields


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


def set_remote_columns(
    row: dict,
    remote_static: str,
    remote_events: dict[str, dict],
):
    """Set Remote Config template and GA4 event fields.

    App-version performance is intentionally handled in separate keyed rows so
    the same version never moves between rank-based columns across dates.
    """
    row.update(parse_remote_static_fields(remote_static))

    for event_name in REMOTE_EVENT_COLUMNS:
        data = remote_events.get(event_name, {})
        row[f"remote_{event_name}/users"] = data.get("users", 0)
        row[f"remote_{event_name}/events"] = data.get("events", 0)


def set_remote_version_columns(row: dict, item: dict):
    row["remote_app_version"] = item.get("version", "")
    row["remote_app_version_users"] = item.get("users", "")
    row["remote_app_version_sessions"] = item.get("sessions", "")
    row["remote_app_version_er"] = item.get("er", "")


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


def set_personalized_columns(row: dict, category: str, item: dict):
    """Set one actual Personalized UX key on one row.

    Examples of keys are Ukraine, English, mobile, Android, 1.0.20, cpc,
    MainActivity, or any other value returned by GA4. No value is hardcoded into
    a column name.
    """
    row["personalized_category"] = category
    row["personalized_key"] = item.get("value", "")
    row["personalized_users"] = item.get("active", "")
    row["personalized_sessions"] = item.get("sessions", "")
    row["personalized_er"] = item.get("engagement", "")
    row["personalized_avg"] = item.get("avg", "")


def build_rows_for_app(app: AppConfig, report_dates: list[str], package_name: str) -> list[list]:
    print(f"Processing: {app.app_name} / {app.property_id} / {report_dates[0]} to {report_dates[-1]}")

    notification_events = list(NOTIFICATION_COLUMNS)
    feature_events = split_csv(app.feature_event_names)
    app_open_events = split_csv(app.app_open_event_names)
    home_event_names = split_csv(app.home_event_names)
    required_funnel_events = ["ad_impression", "in_app_purchase"]
    event_names = split_csv(
        ",".join(
            notification_events
            + feature_events
            + required_funnel_events
            + app_open_events
            + home_event_names
        )
    )

    daily_metrics: dict[str, dict] = {}
    event_data: dict[tuple[str, str], dict] = {}
    home_data: dict[str, dict] = {}
    retention_data: dict[str, dict] = {}
    audience_segments = {report_date: [] for report_date in report_dates}
    personalized_ux: dict[str, dict[str, list[dict]]] = {report_date: {} for report_date in report_dates}
    remote_events: dict[str, dict[str, dict]] = {report_date: {} for report_date in report_dates}
    remote_versions: dict[str, list[dict]] = {report_date: [] for report_date in report_dates}
    fcm_delivery: dict[str, dict] = {report_date: {} for report_date in report_dates}
    static_remote = get_remote_config_static_summaries(app)

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
    try:
        remote_events = run_remote_config_events_report(app, report_dates)
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"REMOTE CONFIG EVENTS {status} for {app.app_name}: {error_text}")
    try:
        remote_versions = run_remote_config_app_versions(app, report_dates)
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"REMOTE CONFIG APP VERSIONS {status} for {app.app_name}: {error_text}")
    try:
        fcm_delivery = build_fcm_delivery_fields_by_date(app, report_dates)
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"FCM DELIVERY {status} for {app.app_name}: {error_text}")

    rows: list[list] = []
    for report_date in report_dates:
        # Remote app versions and Personalized UX are independent datasets.
        # They are compacted side by side by row position so one dataset does
        # not create an empty vertical block before the other. The first row
        # also carries the package-date summary fields, removing the previous
        # blank gap under the dynamic columns.
        personalized_items: list[tuple[str, dict]] = []
        personalized_for_date = personalized_ux.get(report_date, {})
        for category, _ in PERSONALIZED_COLUMN_SPECS:
            for item in personalized_for_date.get(category, []):
                personalized_items.append((category, item))

        version_items = remote_versions.get(report_date, [])
        row_count = max(1, len(version_items), len(personalized_items))

        for index in range(row_count):
            output_row = {header: "" for header in OUTPUT_HEADERS}
            output_row["Package Name"] = package_name
            output_row["Date"] = report_date

            # Store date-level summary metrics only once, on the first compact
            # row for this package and date.
            if index == 0:
                for event_name in NOTIFICATION_COLUMNS:
                    data = get_event_metric(event_data, report_date, event_name)
                    output_row[f"{event_name}_Events"] = to_number(data.get("event_count", 0))
                    output_row[f"{event_name}_USERS"] = to_number(data.get("active_users", 0))

                output_row.update(fcm_delivery.get(report_date, {}))
                set_audience_columns(output_row, audience_segments.get(report_date, []))
                set_funnel_columns(output_row, report_date, app, event_data, home_data)
                set_remote_columns(
                    output_row,
                    static_remote.get("remote_config_static", ""),
                    remote_events.get(report_date, {}),
                )
                output_row["A/B Test on Time Capping"] = static_remote.get("time_capping", "")
                output_row["A/B Test on IAPs Screen"] = static_remote.get("iap_screen", "")
                set_time_and_retention_columns(
                    output_row,
                    daily_metrics.get(report_date, {}),
                    retention_data.get(report_date, {}),
                )

            # Fill the next Remote Config app-version record, when available.
            if index < len(version_items):
                set_remote_version_columns(output_row, version_items[index])

            # Fill the next Personalized UX record independently, when
            # available. It can share the row with a remote-version record;
            # there is no analytical relationship implied between them.
            if index < len(personalized_items):
                category, item = personalized_items[index]
                set_personalized_columns(output_row, category, item)

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

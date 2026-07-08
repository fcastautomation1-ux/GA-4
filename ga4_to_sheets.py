import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
        range=f"{sheet_name}!A:Z",
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
    ]


def ensure_apps_config_headers(service, values: list[list]):
    expected_headers = get_apps_config_headers()
    current_headers = values[0] if values else []

    if current_headers[: len(expected_headers)] == expected_headers:
        return

    service.spreadsheets().values().update(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.apps_config_sheet}!A1:I1",
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
        range=f"{config.apps_config_sheet}!A:I",
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

    api_not_enabled_keywords = [
        "api has not been used",
        "has not been enabled",
        "enable it by visiting",
        "service disabled",
        "api disabled",
    ]

    if any(keyword in error_lower for keyword in api_not_enabled_keywords):
        return "API NOT ENABLED", error_text

    if any(keyword in error_lower for keyword in no_access_keywords):
        return "NO ACCESS", error_text

    if any(keyword in error_lower for keyword in invalid_property_keywords):
        return "INVALID PROPERTY ID", error_text

    return "ERROR", error_text


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
        raise ValueError("Firebase Project ID is empty in Apps Config.")

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
# MAIN
# =========================


def main():
    print("Reading app list from Apps Config sheet...")

    apps = read_apps_config()

    print(f"Total enabled apps found: {len(apps)}")

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

    write_sheet(config.summary_sheet, funnel_summary_rows)
    write_sheet(config.details_sheet, funnel_details_rows)
    write_sheet(config.user_session_sheet, user_session_rows)
    write_sheet(config.retention_details_sheet, retention_details_rows)
    write_sheet(config.audience_segments_sheet, audience_segment_rows)
    write_sheet(config.personalized_ux_sheet, personalized_ux_rows)
    write_sheet(config.remote_config_sheet, remote_config_rows)
    write_sheet(config.time_capping_ab_sheet, time_capping_ab_rows)

    print("Done. All reports updated in Google Sheet.")
    print(f"Funnel Summary: {config.summary_sheet}")
    print(f"Funnel Details: {config.details_sheet}")
    print(f"User Session Summary: {config.user_session_sheet}")
    print(f"Retention Details: {config.retention_details_sheet}")
    print(f"Audience Segments: {config.audience_segments_sheet}")
    print(f"Personalized User Experience: {config.personalized_ux_sheet}")
    print(f"Remote Configuration: {config.remote_config_sheet}")
    print(f"Firebase A/B Time Capping: {config.time_capping_ab_sheet}")
    print(f"Report Date Range: {report_date_range}")


if __name__ == "__main__":
    main()

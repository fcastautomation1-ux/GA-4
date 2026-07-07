import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account

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


def get_credentials():
    service_account_info = json.loads(config.service_account_json)

    return service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )


credentials = get_credentials()

alpha_client = AlphaAnalyticsDataClient(credentials=credentials)
beta_client = BetaAnalyticsDataClient(credentials=credentials)


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


def create_apps_config_template(service):
    ensure_sheet_exists(service, config.apps_config_sheet)

    rows = [
        [
            "Enabled",
            "App Name",
            "Property ID",
            "Home Screen Name",
            "Screen Field",
        ],
        [
            "TRUE",
            "ai-voice-generator-b2073",
            "498019838",
            "MainActivity",
            "unifiedPagePathScreen",
        ],
        [
            "TRUE",
            "antivirus-vibrant-soft",
            "504100281",
            "MainActivity",
            "unifiedPagePathScreen",
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
        range=f"{config.apps_config_sheet}!A:E",
    ).execute()

    values = response.get("values", [])

    if len(values) <= 1:
        create_apps_config_template(service)
        raise SystemExit(
            "Apps Config sheet was empty. Template created. Fill apps and run again."
        )

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


def get_personalized_ux_dimensions():
    return [
        {
            "area": "Country Experience",
            "dimension": "country",
            "label": "Country",
        },
        {
            "area": "Language Experience",
            "dimension": "language",
            "label": "Language",
        },
        {
            "area": "Device Experience",
            "dimension": "deviceCategory",
            "label": "Device Category",
        },
        {
            "area": "Operating System Experience",
            "dimension": "operatingSystem",
            "label": "Operating System",
        },
        {
            "area": "App Version Experience",
            "dimension": "appVersion",
            "label": "App Version",
        },
        {
            "area": "Acquisition Experience",
            "dimension": "firstUserMedium",
            "label": "First User Medium",
        },
        {
            "area": "Screen Experience",
            "dimension": "unifiedPagePathScreen",
            "label": "Page Path / Screen Class",
        },
    ]


def run_personalized_ux_report(
    app: AppConfig,
    dimension_name: str,
    limit: int,
):
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
        limit=limit,
    )

    return beta_client.run_report(request)


def safe_int(value) -> int:
    try:
        if value in [None, ""]:
            return 0

        return int(float(value))
    except Exception:
        return 0


def safe_rate_text(numerator, denominator) -> str:
    numerator_value = safe_int(numerator)
    denominator_value = safe_int(denominator)

    if denominator_value == 0:
        return "0%"

    return f"{round((numerator_value / denominator_value) * 100, 2)}%"


def get_personalized_ux_recommendation(
    area: str,
    dimension_value: str,
    active_users: int,
    engagement_rate_raw,
    avg_session_seconds,
    user_share_text: str,
) -> str:
    if active_users == 0:
        return "No personalization action needed because this segment has no users."

    engagement_rate_value = to_float(engagement_rate_raw)
    avg_seconds_value = to_float(avg_session_seconds)

    if engagement_rate_value <= 1:
        engagement_rate_percent = engagement_rate_value * 100
    else:
        engagement_rate_percent = engagement_rate_value

    if active_users >= 100 and engagement_rate_percent < 40:
        return (
            f"High priority: users in {dimension_value} have low engagement. "
            "Review onboarding, content relevance, loading speed, and screen flow for this segment."
        )

    if area == "Country Experience":
        return (
            f"Personalize store listing, language tone, pricing, and onboarding for {dimension_value}. "
            f"This segment represents {user_share_text} of active users."
        )

    if area == "Language Experience":
        return (
            f"Consider localized UI text, onboarding, and support content for {dimension_value} users."
        )

    if area == "Device Experience":
        return (
            f"Optimize layout, performance, and touch targets for {dimension_value} users."
        )

    if area == "Operating System Experience":
        return (
            f"QA crashes, permissions, speed, and UI compatibility for {dimension_value} users."
        )

    if area == "App Version Experience":
        return (
            f"Compare this app version with other versions. If engagement is lower, check release changes and bugs for {dimension_value}."
        )

    if area == "Acquisition Experience":
        return (
            f"Customize onboarding and first-session messaging for users coming from {dimension_value}."
        )

    if area == "Screen Experience":
        if avg_seconds_value > 600:
            return (
                f"Users spend a long time on {dimension_value}. Check whether this is positive engagement or a confusing flow."
            )

        return (
            f"Prioritize UX review for this high-traffic screen: {dimension_value}."
        )

    return "Review this segment for personalization opportunities."


def parse_personalized_ux_response(
    app: AppConfig,
    response,
    area: str,
    dimension_name: str,
    dimension_label: str,
    total_active_users: int,
):
    rows = []
    report_date_range = get_report_date_range_display()

    if not response.rows:
        return [
            [
                app.app_name,
                app.property_id,
                report_date_range,
                area,
                dimension_label,
                dimension_name,
                "",
                0,
                "0%",
                0,
                0,
                0,
                0,
                0,
                "0m 0s",
                0,
                "0m 0s",
                0,
                "0%",
                "No users found for this personalization dimension.",
                "NO UX DATA",
                "",
                now_text(),
            ]
        ]

    dimension_headers = [header.name for header in response.dimension_headers]
    metric_headers = [header.name for header in response.metric_headers]

    parsed_rows = []

    for row in response.rows:
        row_data = {}

        for index, dimension_value in enumerate(row.dimension_values):
            row_data[dimension_headers[index]] = dimension_value.value

        for index, metric_value in enumerate(row.metric_values):
            row_data[metric_headers[index]] = metric_value.value

        dimension_value = row_data.get(dimension_name, "")

        if not str(dimension_value).strip():
            dimension_value = "(not set)"

        active_users = safe_int(row_data.get("activeUsers", 0))
        parsed_rows.append((active_users, dimension_value, row_data))

    parsed_rows.sort(key=lambda item: item[0], reverse=True)

    for rank, (active_users, dimension_value, row_data) in enumerate(parsed_rows, start=1):
        new_users = safe_int(row_data.get("newUsers", 0))
        sessions = safe_int(row_data.get("sessions", 0))
        engaged_sessions = safe_int(row_data.get("engagedSessions", 0))

        avg_session_seconds = to_float(row_data.get("averageSessionDuration", 0))
        total_engagement_seconds = to_float(row_data.get("userEngagementDuration", 0))

        if active_users > 0:
            sessions_per_active_user = round(sessions / active_users, 2)
        else:
            sessions_per_active_user = 0

        user_share = safe_rate_text(active_users, total_active_users)
        engagement_rate_raw = row_data.get("engagementRate", 0)

        recommendation = get_personalized_ux_recommendation(
            area=area,
            dimension_value=dimension_value,
            active_users=active_users,
            engagement_rate_raw=engagement_rate_raw,
            avg_session_seconds=avg_session_seconds,
            user_share_text=user_share,
        )

        rows.append(
            [
                app.app_name,
                app.property_id,
                report_date_range,
                area,
                dimension_label,
                dimension_name,
                dimension_value,
                rank,
                user_share,
                active_users,
                new_users,
                sessions,
                engaged_sessions,
                round(avg_session_seconds, 2),
                format_seconds(avg_session_seconds),
                round(total_engagement_seconds, 2),
                format_seconds(total_engagement_seconds),
                sessions_per_active_user,
                to_percent(engagement_rate_raw),
                recommendation,
                "SUCCESS",
                "",
                now_text(),
            ]
        )

    return rows


def build_personalized_ux_rows_for_app(
    app: AppConfig,
    total_active_users,
) -> list[list]:
    rows = []
    total_active_users_value = safe_int(total_active_users)
    report_limit = max(config.ux_report_limit, 1)

    for dimension_config in get_personalized_ux_dimensions():
        area = dimension_config["area"]
        dimension_name = dimension_config["dimension"]
        dimension_label = dimension_config["label"]

        try:
            response = run_personalized_ux_report(
                app=app,
                dimension_name=dimension_name,
                limit=report_limit,
            )

            rows.extend(
                parse_personalized_ux_response(
                    app=app,
                    response=response,
                    area=area,
                    dimension_name=dimension_name,
                    dimension_label=dimension_label,
                    total_active_users=total_active_users_value,
                )
            )

        except Exception as error:
            status, error_text = classify_api_error(error)

            rows.append(
                [
                    app.app_name,
                    app.property_id,
                    get_report_date_range_display(),
                    area,
                    dimension_label,
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
                    "",
                    "",
                    status,
                    error_text,
                    now_text(),
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
            "Personalization Area",
            "Dimension Label",
            "Dimension Field",
            "Dimension Value",
            "Rank",
            "Active User Share",
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
            app_personalized_ux_rows = build_personalized_ux_rows_for_app(
                app=app,
                total_active_users=session_data["active_users"],
            )
            personalized_ux_rows.extend(app_personalized_ux_rows)

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

    print("Done. All reports updated in Google Sheet.")
    print(f"Funnel Summary: {config.summary_sheet}")
    print(f"Funnel Details: {config.details_sheet}")
    print(f"User Session Summary: {config.user_session_sheet}")
    print(f"Retention Details: {config.retention_details_sheet}")
    print(f"Audience Segments: {config.audience_segments_sheet}")
    print(f"Personalized UX: {config.personalized_ux_sheet}")
    print(f"Report Date Range: {report_date_range}")


if __name__ == "__main__":
    main()

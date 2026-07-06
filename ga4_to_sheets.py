import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from google.analytics.data_v1alpha import AlphaAnalyticsDataClient
from google.analytics.data_v1alpha.types import (
    DateRange,
    Funnel,
    FunnelStep,
    FunnelEventFilter,
    FunnelFieldFilter,
    FunnelFilterExpression,
    FunnelFilterExpressionList,
    RunFunnelReportRequest,
    StringFilter,
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


def create_apps_config_template(service):
    ensure_sheet_exists(service, config.apps_config_sheet)

    headers = [
        "Enabled",
        "App Name",
        "Property ID",
        "Home Screen Name",
        "Screen Field",
    ]

    sample_rows = [
        headers,
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
        body={"values": sample_rows},
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
            "Apps Config sheet was empty. Template created. Fill your 20+ apps and run again."
        )

    apps = []

    for index, row in enumerate(values[1:], start=2):
        enabled = row[0].strip().upper() if len(row) > 0 else ""
        app_name = row[1].strip() if len(row) > 1 else ""
        property_id = row[2].strip() if len(row) > 2 else ""
        home_screen_name = row[3].strip() if len(row) > 3 and row[3].strip() else config.default_home_screen_name
        screen_field = row[4].strip() if len(row) > 4 and row[4].strip() else config.default_screen_field

        if enabled not in ["TRUE", "YES", "1", "Y"]:
            continue

        if not app_name or not property_id:
            print(f"Skipping row {index}: App Name or Property ID missing.")
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


def get_date_range_display() -> str:
    start = resolve_ga4_date(config.start_date)
    end = resolve_ga4_date(config.end_date)
    return f"{start} to {end}"


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
    client = AlphaAnalyticsDataClient(credentials=credentials)

    request = RunFunnelReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[
            DateRange(
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

    return client.run_funnel_report(request)


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


def get_metric(row_data: dict, possible_names: list[str], default=""):
    for name in possible_names:
        if name in row_data:
            return row_data[name]

    return default


def parse_funnel_rows(app: AppConfig, response):
    table = response.funnel_table

    dimension_headers = [header.name for header in table.dimension_headers]
    metric_headers = [header.name for header in table.metric_headers]

    updated_at = datetime.now(
        ZoneInfo(config.timezone)
    ).strftime("%Y-%m-%d %I:%M:%S %p")

    date_range_display = get_date_range_display()

    details_rows = []

    first_open_users = 0
    home_users = 0
    first_open_abandonments = 0

    for row in table.rows:
        row_data = {}

        for index, dimension_value in enumerate(row.dimension_values):
            header_name = dimension_headers[index]
            row_data[header_name] = dimension_value.value

        for index, metric_value in enumerate(row.metric_values):
            header_name = metric_headers[index]
            row_data[header_name] = metric_value.value

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

        details_rows.append(
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
        conversion_rate = f"{round((home_users / first_open_users) * 100, 2)}%"
        drop_off = first_open_users - home_users
        abandonment_rate = f"{round((drop_off / first_open_users) * 100, 2)}%"
    else:
        conversion_rate = "0%"
        drop_off = 0
        abandonment_rate = "0%"

    if first_open_abandonments > 0:
        drop_off = first_open_abandonments

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
        "SUCCESS",
        "",
        updated_at,
    ]

    return summary_row, details_rows


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


def main():
    print("Reading app list from Apps Config sheet...")

    apps = read_apps_config()

    print(f"Total enabled apps found: {len(apps)}")

    updated_at = datetime.now(
        ZoneInfo(config.timezone)
    ).strftime("%Y-%m-%d %I:%M:%S %p")

    summary_rows = [
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

    details_rows = [
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

    date_range_display = get_date_range_display()

    for app in apps:
        print(f"Processing: {app.app_name} / {app.property_id}")

        try:
            response = run_first_open_to_home_funnel(app)
            summary_row, app_details_rows = parse_funnel_rows(app, response)

            summary_rows.append(summary_row)
            details_rows.extend(app_details_rows)

        except Exception as error:
            error_text = str(error)

            print(f"ERROR for {app.app_name}: {error_text}")

            summary_rows.append(
                [
                    app.app_name,
                    app.property_id,
                    date_range_display,
                    "",
                    "",
                    "",
                    "",
                    "",
                    app.home_screen_name,
                    app.screen_field,
                    "ERROR",
                    error_text,
                    updated_at,
                ]
            )

            details_rows.append(
                [
                    app.app_name,
                    app.property_id,
                    date_range_display,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "ERROR",
                    error_text,
                    updated_at,
                ]
            )

    write_sheet(config.summary_sheet, summary_rows)
    write_sheet(config.details_sheet, details_rows)

    print("Done. All apps updated in Google Sheet.")
    print(f"Summary Sheet: {config.summary_sheet}")
    print(f"Details Sheet: {config.details_sheet}")
    print(f"Date Range: {date_range_display}")


if __name__ == "__main__":
    main()

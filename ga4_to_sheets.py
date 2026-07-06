import json
from datetime import datetime
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


def get_credentials():
    service_account_info = json.loads(config.service_account_json)

    return service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )


credentials = get_credentials()


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


def run_first_open_to_home_funnel():
    client = AlphaAnalyticsDataClient(credentials=credentials)

    request = RunFunnelReportRequest(
        property=f"properties/{config.property_id}",
        date_ranges=[
            DateRange(
                start_date=config.start_date,
                end_date=config.end_date,
            )
        ],
        funnel=Funnel(
            is_open_funnel=False,  # closed funnel: users must start from step 1
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
                                    config.screen_field,
                                    config.home_screen_name,
                                ),
                            ]
                        )
                    ),
                ),
            ],
        ),
    )

    return client.run_funnel_report(request)


def get_sheets_service():
    return build(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )


def ensure_sheet_exists(service):
    spreadsheet = service.spreadsheets().get(
        spreadsheetId=config.spreadsheet_id
    ).execute()

    existing_sheets = [
        sheet["properties"]["title"]
        for sheet in spreadsheet.get("sheets", [])
    ]

    if config.sheet_name not in existing_sheets:
        service.spreadsheets().batchUpdate(
            spreadsheetId=config.spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": config.sheet_name
                            }
                        }
                    }
                ]
            },
        ).execute()


def write_rows_to_sheet(rows: list[list]):
    service = get_sheets_service()
    ensure_sheet_exists(service)

    service.spreadsheets().values().clear(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.sheet_name}!A:Z",
        body={},
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.sheet_name}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()


def to_number(value):
    if value in [None, ""]:
        return 0

    try:
        return int(float(value))
    except Exception:
        return value


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


def funnel_table_to_rows(response):
    table = response.funnel_table

    dimension_headers = [header.name for header in table.dimension_headers]
    metric_headers = [header.name for header in table.metric_headers]

    updated_at = datetime.now(
        ZoneInfo(config.timezone)
    ).strftime("%Y-%m-%d %I:%M:%S %p")

    rows = [
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
            "Updated At",
        ]
    ]

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

        active_users = get_metric(
            row_data,
            ["activeUsers"],
            "0",
        )

        completion_rate = get_metric(
            row_data,
            [
                "completionRate",
                "funnelStepCompletionRate",
                "funnelCompletionRate",
            ],
            "",
        )

        abandonments = get_metric(
            row_data,
            [
                "abandonments",
                "funnelStepAbandonments",
            ],
            "0",
        )

        abandonment_rate = get_metric(
            row_data,
            [
                "abandonmentRate",
                "funnelStepAbandonmentRate",
            ],
            "",
        )

        if "First Open" in funnel_step:
            event_name = "first_open"
            screen_condition = ""
        elif "Home Users" in funnel_step:
            event_name = "screen_view"
            screen_condition = f"{config.screen_field} contains {config.home_screen_name}"
        else:
            event_name = ""
            screen_condition = ""

        rows.append(
            [
                config.app_name,
                config.property_id,
                f"{config.start_date} to {config.end_date}",
                funnel_step,
                event_name,
                screen_condition,
                to_number(active_users),
                to_percent(completion_rate),
                to_number(abandonments),
                to_percent(abandonment_rate),
                updated_at,
            ]
        )

    return rows


def main():
    print("Reading real GA4 funnel report...")

    response = run_first_open_to_home_funnel()
    rows = funnel_table_to_rows(response)

    write_rows_to_sheet(rows)

    print("Done. Real GA4 funnel written to Google Sheet.")
    print(f"App Name: {config.app_name}")
    print(f"Property ID: {config.property_id}")
    print(f"Home Screen: {config.home_screen_name}")
    print(f"Screen Field: {config.screen_field}")


if __name__ == "__main__":
    main()

import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
    FilterExpression,
    FilterExpressionList,
    Filter,
)
from googleapiclient.discovery import build


APP_NAME = "antivirus-vibrant-soft"
PROPERTY_ID = os.getenv("GA4_PROPERTY_ID", "504100281")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHEET_NAME = "GA4 Basic Funnel"

START_DATE = "30daysAgo"
END_DATE = "today"

HOME_SCREEN_NAME = os.getenv("HOME_SCREEN_NAME") or "Home"


SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_credentials():
    service_account_json = os.getenv("GA4_SERVICE_ACCOUNT_JSON")

    if not service_account_json:
        raise ValueError("Missing GitHub secret: GA4_SERVICE_ACCOUNT_JSON")

    info = json.loads(service_account_json)

    return service_account.Credentials.from_service_account_info(
        info,
        scopes=SCOPES,
    )


credentials = get_credentials()


def exact_filter(field_name, value):
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


def contains_filter(field_name, value):
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


def get_ga4_step_data(event_name, screen_name=None):
    client = BetaAnalyticsDataClient(credentials=credentials)

    filters = [
        exact_filter("eventName", event_name)
    ]

    if screen_name:
        filters.append(contains_filter("unifiedScreenName", screen_name))

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        date_ranges=[
            DateRange(start_date=START_DATE, end_date=END_DATE)
        ],
        dimensions=[
            Dimension(name="eventName")
        ],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="eventCount"),
        ],
        dimension_filter=FilterExpression(
            and_group=FilterExpressionList(expressions=filters)
        ),
    )

    response = client.run_report(request)

    if not response.rows:
        return {
            "active_users": 0,
            "event_count": 0,
        }

    row = response.rows[0]

    return {
        "active_users": row.metric_values[0].value,
        "event_count": row.metric_values[1].value,
    }


def get_sheets_service():
    return build("sheets", "v4", credentials=credentials)


def ensure_sheet_exists(service):
    spreadsheet = service.spreadsheets().get(
        spreadsheetId=SPREADSHEET_ID
    ).execute()

    existing_sheets = [
        sheet["properties"]["title"]
        for sheet in spreadsheet.get("sheets", [])
    ]

    if SHEET_NAME not in existing_sheets:
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": SHEET_NAME
                            }
                        }
                    }
                ]
            },
        ).execute()


def write_rows_to_sheet(rows):
    if not SPREADSHEET_ID:
        raise ValueError("Missing GitHub secret: SPREADSHEET_ID")

    service = get_sheets_service()
    ensure_sheet_exists(service)

    service.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A:Z",
        body={},
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()


def main():
    first_open_data = get_ga4_step_data("first_open")

    home_users_data = get_ga4_step_data(
        event_name="screen_view",
        screen_name=HOME_SCREEN_NAME,
    )

    updated_at = datetime.now(
        ZoneInfo("Asia/Karachi")
    ).strftime("%Y-%m-%d %I:%M:%S %p PKT")

    rows = [
        [
            "App Name",
            "Property ID",
            "Date Range",
            "Funnel Step",
            "Event Name",
            "Screen Condition",
            "Active Users",
            "Event Count",
            "Updated At",
        ],
        [
            APP_NAME,
            PROPERTY_ID,
            f"{START_DATE} to {END_DATE}",
            "Step 1 - First Open",
            "first_open",
            "",
            first_open_data["active_users"],
            first_open_data["event_count"],
            updated_at,
        ],
        [
            APP_NAME,
            PROPERTY_ID,
            f"{START_DATE} to {END_DATE}",
            "Step 2 - Home Users",
            "screen_view",
            f"unifiedScreenName contains {HOME_SCREEN_NAME}",
            home_users_data["active_users"],
            home_users_data["event_count"],
            updated_at,
        ],
    ]

    write_rows_to_sheet(rows)

    print("GA4 data written successfully to Google Sheet.")


if __name__ == "__main__":
    main()

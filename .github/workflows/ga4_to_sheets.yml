name: GA4 to Google Sheets

on:
  workflow_dispatch:

  schedule:
    - cron: "0 4 * * *"

jobs:
  ga4-to-sheets:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install packages
        run: pip install -r requirements.txt

      - name: Run GA4 script
        env:
          GA4_SERVICE_ACCOUNT_JSON: ${{ secrets.GA4_SERVICE_ACCOUNT_JSON }}
          SPREADSHEET_ID: ${{ secrets.SPREADSHEET_ID }}

          START_DATE: ${{ vars.START_DATE || '28daysAgo' }}
          END_DATE: ${{ vars.END_DATE || 'yesterday' }}
          RETENTION_DAYS: ${{ vars.RETENTION_DAYS || '7' }}

          APPS_CONFIG_SHEET: Apps Config
          SUMMARY_SHEET: GA4 Funnel Summary
          DETAILS_SHEET: GA4 Funnel Details
          USER_SESSION_SHEET: GA4 User Session Summary
          RETENTION_DETAILS_SHEET: GA4 Retention Details
          AUDIENCE_SEGMENTS_SHEET: GA4 Audience Segments

          DEFAULT_HOME_SCREEN_NAME: MainActivity
          DEFAULT_SCREEN_FIELD: unifiedPagePathScreen
          TIMEZONE: Asia/Karachi

        run: python ga4_to_sheets.py

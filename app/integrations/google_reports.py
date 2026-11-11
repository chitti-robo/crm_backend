"""Search Console + GA4 readers using one service account.

Test from the folder that holds .env:
    python -m app.integrations.google_reports
"""
from datetime import date, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

from ..config import settings

GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"


def search_console_top_queries(days: int = 28, limit: int = 25) -> list[dict]:
    """Top queries. Search Console data lags about 2-3 days, so the window ends 3 days ago."""
    creds = service_account.Credentials.from_service_account_file(
        settings.GOOGLE_SERVICE_ACCOUNT_JSON, scopes=[GSC_SCOPE])
    svc = build("searchconsole", "v1", credentials=creds, cache_discovery=False)
    end = date.today() - timedelta(days=3)
    start = end - timedelta(days=days)
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
            "dimensions": ["query"], "rowLimit": limit}
    res = svc.searchanalytics().query(siteUrl=settings.GSC_SITE_URL, body=body).execute()
    return [{"query": r["keys"][0], "clicks": r["clicks"], "impressions": r["impressions"],
             "position": round(r["position"], 1)} for r in res.get("rows", [])]


def ga4_sessions_by_source(days: int = 28) -> list[dict]:
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest

    client = BetaAnalyticsDataClient.from_service_account_file(settings.GOOGLE_SERVICE_ACCOUNT_JSON)
    req = RunReportRequest(
        property=f"properties/{settings.GA4_PROPERTY_ID}",
        dimensions=[Dimension(name="sessionSourceMedium")],
        metrics=[Metric(name="sessions"), Metric(name="activeUsers")],
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        limit=25,
    )
    res = client.run_report(req)
    return [{"source_medium": r.dimension_values[0].value,
             "sessions": int(r.metric_values[0].value),
             "users": int(r.metric_values[1].value)} for r in res.rows]


if __name__ == "__main__":
    for name, fn in (("Search Console", search_console_top_queries), ("GA4", ga4_sessions_by_source)):
        try:
            rows = fn()
            print(f"{name}: OK, {len(rows)} rows")
            for r in rows[:5]:
                print("  ", r)
        except Exception as e:  # prints the real reason (403, wrong id, API not enabled...)
            print(f"{name}: FAILED -> {e}")
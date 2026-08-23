from datetime import date, datetime, timezone
from types import SimpleNamespace

from weatherman import providers


AIRPORT = {
    "latitude": 52.166,
    "longitude": 20.967,
    "timezone": "Europe/Warsaw",
    "market_city": "warsaw",
    "elevation_m": 110,
}


def test_forecast_horizon_separates_tomorrow_from_later_days():
    run_at = datetime(2026, 7, 19, 9, tzinfo=timezone.utc)
    assert providers._forecast_horizon(run_at, date(2026, 7, 20), "Europe/Warsaw") == "D-1"
    assert providers._forecast_horizon(run_at, date(2026, 7, 21), "Europe/Warsaw") == "D-2+"


def test_previous_run_d1_aggregates_each_local_day(monkeypatch):
    monkeypatch.setattr(
        providers,
        "_get",
        lambda *_args, **_kwargs: {
            "hourly": {
                "time": ["2026-07-18T12:00", "2026-07-18T15:00", "2026-07-19T12:00"],
                "temperature_2m_previous_day1": [29.0, 31.5, 28.0],
            }
        },
    )
    rows = providers.previous_run_d1(AIRPORT, "ecmwf_ifs025", date(2026, 7, 18), date(2026, 7, 19))
    assert [row["max_temp_c"] for row in rows] == [31.5, 28.0]
    assert all(row["horizon"] == "D-1" for row in rows)


def test_recent_metars_skips_incomplete_reports(monkeypatch):
    monkeypatch.setattr(
        providers,
        "_get",
        lambda *_args, **_kwargs: [
            {"obsTime": None, "temp": 30},
            {
                "obsTime": 1_752_921_600,
                "temp": 31,
                "dewp": 14,
                "wspd": 10,
                "wdir": 240,
                "rawOb": "EPWA 191200Z 24010KT CAVOK 31/14 Q1014",
            },
        ],
    )
    rows = providers.recent_metars("EPWA")
    assert len(rows) == 1
    assert rows[0]["temp_c"] == 31
    assert round(rows[0]["wind_kph"], 2) == 18.52
    assert rows[0]["wind_direction"] == 240
    assert rows[0]["cloud_cover"] == 0


def test_recent_metars_accepts_variable_wind_without_direction(monkeypatch):
    monkeypatch.setattr(
        providers,
        "_get",
        lambda *_args, **_kwargs: [
            {"obsTime": 1_752_921_600, "temp": 31, "wspd": 3, "wdir": "VRB"},
        ],
    )
    rows = providers.recent_metars("EPWA")
    assert rows[0]["wind_direction"] is None


def test_meteoblue_preserves_model_run_metadata(monkeypatch):
    monkeypatch.setattr(
        providers,
        "settings",
        SimpleNamespace(
            meteoblue_api_key="test",
            meteoblue_url_template=(
                "https://example.test?lat={lat}&lon={lon}&asl={elevation}&apikey={apikey}"
            ),
            timeout=30,
        ),
    )
    monkeypatch.setattr(
        providers,
        "_get",
        lambda *_args, **_kwargs: {
            "metadata": {
                "modelrun_utc": "2026-07-22T12:00:00Z",
                "modelrun_updatetime_utc": "2026-07-22T13:10:00Z",
            },
            "data_day": {
                "time": ["2026-07-23"],
                "temperature_max": [29.5],
            },
        },
    )
    row = providers.meteoblue_forecast(AIRPORT)[0]
    assert row["model"] == "meteoblue"
    assert row["model_run_at"] == datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
    assert row["available_at"] == datetime(2026, 7, 22, 13, 10, tzinfo=timezone.utc)


def test_recent_tafs_parse_tx_tn_and_decoded_peak_periods(monkeypatch):
    monkeypatch.setattr(
        providers,
        "_get",
        lambda *_args, **_kwargs: [
            {
                "icaoId": "LEMD",
                "issueTime": "2026-07-21T11:00:00Z",
                "bulletinTime": "2026-07-21T11:00:00Z",
                "validTimeFrom": 1_784_635_200,
                "validTimeTo": 1_784_743_200,
                "rawTAF": (
                    "TAF AMD LEMD 211100Z 2112/2218 VRB04KT CAVOK "
                    "TX39/2116Z TN19/2205Z TEMPO 2112/2118 24012G25KT"
                ),
                "fcsts": [
                    {
                        "timeFrom": 1_784_635_200,
                        "timeTo": 1_784_656_800,
                        "timeBec": None,
                        "fcstChange": "TEMPO",
                        "probability": 40,
                        "wdir": 240,
                        "wspd": 12,
                        "wgst": 25,
                        "wxString": None,
                        "clouds": [{"cover": "NSC", "base": None, "type": None}],
                        "temp": [],
                    }
                ],
            }
        ],
    )
    rows = providers.recent_tafs(["LEMD"])
    assert len(rows) == 1
    assert rows[0]["max_temp_c"] == 39
    assert rows[0]["max_temp_at"] == datetime(2026, 7, 21, 16, tzinfo=timezone.utc)
    assert rows[0]["min_temp_c"] == 19
    assert rows[0]["is_amended"]
    assert '\"wind_gust_kt\":25' in rows[0]["periods_json"]


def test_polymarket_prices_parse_exact_and_boundary_ranges(monkeypatch):
    monkeypatch.setattr(
        providers,
        "_get",
        lambda *_args, **_kwargs: {
            "resolutionSource": "https://example.test/station",
            "markets": [
                {
                    "id": "lower",
                    "slug": "lower-market",
                    "groupItemTitle": "25°C or below",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.10", "0.90"]',
                    "clobTokenIds": '["yes-lower", "no-lower"]',
                    "bestBid": 0.08,
                    "bestAsk": 0.11,
                    "closed": False,
                },
                {
                    "id": "exact",
                    "slug": "exact-market",
                    "groupItemTitle": "26°C",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.55", "0.45"]',
                    "clobTokenIds": '["yes-exact", "no-exact"]',
                    "bestBid": 0.53,
                    "bestAsk": 0.57,
                    "closed": False,
                },
            ],
        },
    )
    rows = providers.polymarket_prices(AIRPORT, date(2026, 7, 20))
    assert providers.polymarket_event_slug(AIRPORT, date(2026, 7, 20)).endswith(
        "warsaw-on-july-20-2026"
    )
    assert len(rows) == 2
    assert rows[0]["bucket_low_c"] is None
    assert rows[0]["bucket_high_c"] == 25
    assert rows[1]["bucket_low_c"] == rows[1]["bucket_high_c"] == 26
    assert rows[1]["token_id"] == "yes-exact"


def test_temperature_market_discovery_keeps_unknown_cities(monkeypatch):
    monkeypatch.setattr(
        providers,
        "_get",
        lambda *_args, **_kwargs: [
            {
                "slug": "highest-temperature-in-new-city-on-july-25-2026",
                "title": "Highest temperature in New City on July 25?",
                "active": True,
                "closed": False,
                "resolutionSource": "https://example.test/new-city",
                "markets": [{"groupItemTitle": "90-91°F"}],
            },
            {"slug": "will-it-rain-tomorrow", "title": "Will it rain?"},
        ],
    )
    rows = providers.discover_polymarket_temperature_events(max_pages=1)
    assert rows == [
        {
            "market_city": "new-city",
            "display_name": "New City",
            "target_date": date(2026, 7, 25),
            "event_slug": "highest-temperature-in-new-city-on-july-25-2026",
            "resolution_source": "https://example.test/new-city",
            "market_unit": "F",
            "active": True,
        }
    ]


def test_polymarket_order_books_preserve_depth_and_exchange_timestamp(monkeypatch):
    def fake_post(url, payload, **_kwargs):
        assert url.endswith("/books")
        assert payload == [{"token_id": "yes-34"}]
        return [
            {
                "asset_id": "yes-34",
                "timestamp": "1782753357257",
                "hash": "book-hash",
                "bids": [{"price": "0.19", "size": "8"}],
                "asks": [{"price": "0.21", "size": "7"}],
                "min_order_size": "5",
                "tick_size": "0.01",
            }
        ]

    monkeypatch.setattr(providers, "_post", fake_post)
    books = providers.polymarket_order_books(["yes-34", "yes-34"])
    assert set(books) == {"yes-34"}
    assert books["yes-34"]["hash"] == "book-hash"
    assert books["yes-34"]["asks"][0]["size"] == "7"
    assert books["yes-34"]["observed_at"].year == 2026

from datetime import date

import httpx

from weatherman.aemet_live import (
    AEMET_CLASSIFICATION,
    archive_path,
    curve_rows,
    fetch_public_aemet_json,
    normalized_public_base_url,
)


def test_aemet_public_url_is_https_and_credential_free() -> None:
    assert normalized_public_base_url("https://weather.example.workers.dev/") == (
        "https://weather.example.workers.dev"
    )
    assert normalized_public_base_url("http://weather.example.workers.dev") is None
    assert normalized_public_base_url("https://user:secret@example.test") is None
    assert normalized_public_base_url("https://example.test?token=secret") is None
    assert archive_path(date(2026, 9, 2)) == "archive/aemet/2026/09/02.json.gz"


def test_aemet_reader_validates_station_and_classification(monkeypatch) -> None:
    payload = {
        "classification": AEMET_CLASSIFICATION,
        "station": {"id": "3129"},
        "observations": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/aemet-live.json"
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    class Client:
        def __init__(self, **_kwargs):
            self.client = httpx.Client(transport=transport)

        def __enter__(self):
            return self.client

        def __exit__(self, *_args):
            self.client.close()

    real_client = httpx.Client

    class MockClient(Client):
        def __init__(self, **_kwargs):
            self.client = real_client(transport=transport)

    monkeypatch.setattr(httpx, "Client", MockClient)
    assert fetch_public_aemet_json(
        "https://weather.example.workers.dev", "aemet-live.json"
    ) == payload


def test_curve_keeps_aemet_and_metar_as_separate_series() -> None:
    payload = {
        "observations": [
            {"observed_at": "2026-09-02T14:00:00Z", "temperature_c": 35.1},
            {"observed_at": "2026-09-02T16:20:00Z", "temperature_c": 36.1},
        ],
        "physical_tmax": {
            "observed_at": "2026-09-02T16:20:00Z",
            "value_c": 36.2,
        },
    }
    rows = curve_rows(
        payload,
        [{"observed_at": "2026-09-02T16:00:00Z", "temp_c": 36.0}],
    )

    assert [row["series"] for row in rows] == [
        "AEMET 3129 (physical)",
        "AEMET 3129 (physical)",
        "LEMD METAR (integer)",
        "AEMET physical Tmax",
    ]
    assert rows[-1]["temperature_c"] == 36.2

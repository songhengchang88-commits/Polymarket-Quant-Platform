import pytest

from pmq import marketdata


def test_parse_orderbook_picks_best_prices():
    raw = {
        "bids": [{"price": "0.40", "size": "10"}, {"price": "0.48", "size": "5"}],
        "asks": [{"price": "0.55", "size": "7"}, {"price": "0.49", "size": "3"}],
    }
    book = marketdata.parse_orderbook(raw, ts=123.0)
    assert book is not None
    assert book.bid == pytest.approx(0.48)
    assert book.ask == pytest.approx(0.49)
    assert book.ts == 123.0


def test_parse_orderbook_returns_none_on_empty_side():
    assert marketdata.parse_orderbook({"bids": [], "asks": [{"price": "0.5"}]}) is None
    assert marketdata.parse_orderbook({}) is None
    assert marketdata.parse_orderbook(None) is None


def test_parse_orderbook_rejects_out_of_range_prices():
    raw = {"bids": [{"price": "0"}], "asks": [{"price": "1.2"}]}
    assert marketdata.parse_orderbook(raw) is None


def test_extract_tokens_handles_json_string():
    m = {"clobTokenIds": '["111", "222"]'}
    assert marketdata.extract_tokens(m) == ("111", "222")


def test_extract_tokens_handles_snake_case_and_list():
    assert marketdata.extract_tokens({"clob_token_ids": ["a", "b"]}) == ("a", "b")
    assert marketdata.extract_tokens({"clobTokenIds": ["only-one"]}) is None
    assert marketdata.extract_tokens({}) is None


def test_fetch_books_bulk_parses_payload(monkeypatch):
    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return [
                {"asset_id": "t0", "bids": [{"price": "0.48"}], "asks": [{"price": "0.49"}]},
                {"asset_id": "t1", "bids": [{"price": "0.49"}], "asks": [{"price": "0.50"}]},
            ]

    monkeypatch.setattr(marketdata.requests, "post", lambda *a, **k: Resp())
    books = marketdata.fetch_books_bulk(["t0", "t1"])
    assert set(books) == {"t0", "t1"}
    assert books["t0"].ask == pytest.approx(0.49)


def test_fetch_books_bulk_survives_http_failure(monkeypatch):
    class Resp:
        status_code = 500

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(marketdata.requests, "post", lambda *a, **k: Resp())
    assert marketdata.fetch_books_bulk(["t0"]) == {}

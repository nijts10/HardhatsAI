"""_recover_connection is the resilience fix for ingest-website's long
crawl loop (a Supabase pooler connection can outlive its own session
lifetime mid-run, making rollback() itself raise) -- unit tested with
fake connection objects since it's pure control flow, no real DB needed."""
from unittest.mock import patch

from ingest.cli import _recover_connection


class _FakeConfig:
    database_url = "postgresql://fake"


class _AliveConn:
    def __init__(self):
        self.rolled_back = False

    def rollback(self):
        self.rolled_back = True


class _DeadConn:
    def __init__(self):
        self.closed = False

    def rollback(self):
        raise RuntimeError("the connection is lost")

    def close(self):
        self.closed = True


def test_recover_connection_just_rolls_back_a_healthy_connection():
    conn = _AliveConn()
    result = _recover_connection(conn, _FakeConfig())
    assert result is conn
    assert conn.rolled_back is True


def test_recover_connection_reconnects_when_rollback_itself_fails():
    conn = _DeadConn()
    sentinel = object()
    with patch("ingest.cli.db.connect", return_value=sentinel) as mock_connect:
        result = _recover_connection(conn, _FakeConfig())
    assert conn.closed is True
    assert result is sentinel
    mock_connect.assert_called_once_with("postgresql://fake")


def test_recover_connection_survives_close_also_failing():
    class _VeryDeadConn:
        def rollback(self):
            raise RuntimeError("connection is lost")

        def close(self):
            raise RuntimeError("already closed")

    sentinel = object()
    with patch("ingest.cli.db.connect", return_value=sentinel):
        result = _recover_connection(_VeryDeadConn(), _FakeConfig())
    assert result is sentinel

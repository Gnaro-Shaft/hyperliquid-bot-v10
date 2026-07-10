"""Tests du garde-fou de purge — on ne supprime jamais un jour non archivé."""

import os
from datetime import date

import polars as pl

from scripts.purge_mongo_buffer import day_bounds_ms, parquet_day_count


def test_day_bounds():
    start, end = day_bounds_ms(date(2026, 7, 10))
    assert end - start == 86400 * 1000
    assert start % 1000 == 0


def test_parquet_day_count_missing_returns_none(tmp_path):
    assert parquet_day_count(str(tmp_path), "signal_evaluations", date(2026, 7, 1)) is None


def test_parquet_day_count_sums_all_coins(tmp_path):
    for coin, n in [("BTC", 3), ("ETH", 2)]:
        d = tmp_path / "signal_evaluations" / f"coin={coin}"
        d.mkdir(parents=True)
        pl.DataFrame({"timestamp": list(range(n))}).write_parquet(
            d / "date=2026-07-01.parquet")
    assert parquet_day_count(str(tmp_path), "signal_evaluations", date(2026, 7, 1)) == 5


class _FakeCollection:
    def __init__(self, n):
        self.n = n
        self.deleted = 0

    def count_documents(self, q):
        return self.n

    def delete_many(self, q):
        self.deleted = self.n
        class R: deleted_count = self.n
        return R()


class _FakeDB(dict):
    def __getitem__(self, k):
        return super().__getitem__(k)


def test_purge_refuses_when_parquet_incomplete(tmp_path):
    from scripts.purge_mongo_buffer import purge_day
    db = _FakeDB(signal_evaluations=_FakeCollection(10))
    # Parquet ne couvre que 4 lignes sur 10 → refus
    d = tmp_path / "signal_evaluations" / "coin=BTC"
    d.mkdir(parents=True)
    pl.DataFrame({"timestamp": list(range(4))}).write_parquet(d / "date=2026-07-01.parquet")
    deleted, problem = purge_day(db, "signal_evaluations", date(2026, 7, 1), str(tmp_path))
    assert deleted == 0
    assert "incomplet" in problem
    assert db["signal_evaluations"].deleted == 0


def test_purge_deletes_when_fully_archived(tmp_path):
    from scripts.purge_mongo_buffer import purge_day
    db = _FakeDB(signal_evaluations=_FakeCollection(10))
    d = tmp_path / "signal_evaluations" / "coin=BTC"
    d.mkdir(parents=True)
    pl.DataFrame({"timestamp": list(range(10))}).write_parquet(d / "date=2026-07-01.parquet")
    deleted, problem = purge_day(db, "signal_evaluations", date(2026, 7, 1), str(tmp_path))
    assert deleted == 10
    assert problem is None

"""数据快照清单和全量 sha256 发布门禁测试。"""

from __future__ import annotations

import json
import copy
import hashlib
import stat

import pandas as pd
import pytest

import quantbot.price_action.data.downloader as downloader_mod
import quantbot.price_action.data.snapshot as snapshot_mod
import quantbot.price_action.data.sp500_pit as pit_mod
from quantbot.price_action.backtest.engine import BacktestEngine
from quantbot.price_action.config import config_hash
from quantbot.price_action.data.snapshot import (
    MANIFEST_NAME,
    create_snapshot,
    resolve_snapshot,
    verify_snapshot,
)

from .conftest import make_cfg


def _cache_with_files(tmp_path, count: int = 30):
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(count):
        (cache / f"symbol-{index:02d}.csv").write_text(
            f"date,close\n2026-08-07,{100 + index}\n",
            encoding="utf-8",
        )
    return cache


def test_full_hash_verification_detects_any_modified_file(tmp_path):
    cache = _cache_with_files(tmp_path)
    snapshot = create_snapshot(cache, "release-candidate")
    assert verify_snapshot(snapshot, full=True)

    target = snapshot / "symbol-29.csv"
    target.chmod(0o644)
    target.write_text(
        "date,close\n2026-08-07,999\n", encoding="utf-8"
    )

    assert not verify_snapshot(snapshot, full=True)
    assert not verify_snapshot(snapshot, sample=None)
    with pytest.raises(RuntimeError, match="全量 sha256 校验失败"):
        resolve_snapshot(cache, "release-candidate", verify_hashes=True)


def test_existing_sampled_verification_call_remains_compatible(tmp_path):
    cache = _cache_with_files(tmp_path, count=3)
    snapshot = create_snapshot(cache, "compat")
    assert verify_snapshot(snapshot)
    assert verify_snapshot(snapshot, sample=1)
    assert resolve_snapshot(cache, "compat") == snapshot


def test_manifest_count_mismatch_is_rejected_before_hashing(tmp_path):
    cache = _cache_with_files(tmp_path, count=2)
    snapshot = create_snapshot(cache, "bad-count")
    manifest_path = snapshot / MANIFEST_NAME
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["file_count"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert not verify_snapshot(snapshot, full=True)
    with pytest.raises(RuntimeError, match="file_count"):
        resolve_snapshot(cache, "bad-count")


def test_snapshot_is_self_verified_atomically_published_and_read_only(tmp_path):
    cache = _cache_with_files(tmp_path, count=2)
    snapshot = create_snapshot(cache, "atomic")

    manifest = json.loads((snapshot / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 1
    assert manifest["created_at"].endswith("Z")
    assert verify_snapshot(snapshot, full=True)
    assert not (snapshot.stat().st_mode & stat.S_IWUSR)
    assert all(not (path.stat().st_mode & stat.S_IWUSR) for path in snapshot.rglob("*"))
    assert not list((cache / "snapshots").glob(".snapshot-atomic-*"))


def test_failed_copy_never_publishes_partial_snapshot(tmp_path, monkeypatch):
    cache = _cache_with_files(tmp_path, count=2)
    original = snapshot_mod.shutil.copy2
    calls = 0

    def fail_on_second_copy(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated copy failure")
        return original(source, destination)

    monkeypatch.setattr(snapshot_mod.shutil, "copy2", fail_on_second_copy)
    with pytest.raises(OSError, match="simulated copy failure"):
        create_snapshot(cache, "incomplete")

    assert not (cache / "snapshots" / "incomplete").exists()
    assert not list((cache / "snapshots").glob(".snapshot-incomplete-*"))


@pytest.mark.parametrize("snapshot_id", ["../escape", "nested/name", "..", ""])
def test_snapshot_id_cannot_escape_snapshot_root(tmp_path, snapshot_id):
    cache = _cache_with_files(tmp_path, count=1)
    with pytest.raises(ValueError, match="snapshot_id"):
        create_snapshot(cache, snapshot_id)


@pytest.mark.parametrize("snapshot_id", ["../escape", "nested/name", "..", ""])
def test_resolve_snapshot_id_cannot_escape_snapshot_root(tmp_path, snapshot_id):
    cache = _cache_with_files(tmp_path, count=1)
    with pytest.raises(ValueError, match="snapshot_id"):
        resolve_snapshot(cache, snapshot_id)


def test_resolve_rejects_manifest_id_mismatch(tmp_path):
    cache = _cache_with_files(tmp_path, count=1)
    snapshot = create_snapshot(cache, "expected-id")
    manifest_path = snapshot / MANIFEST_NAME
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["id"] = "different-id"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="manifest.id"):
        resolve_snapshot(cache, "expected-id")


def test_resolve_rejects_symlinked_payload(tmp_path):
    cache = _cache_with_files(tmp_path, count=1)
    snapshot = create_snapshot(cache, "symlinked")
    payload = snapshot / "symbol-00.csv"
    outside = tmp_path / "outside.csv"
    outside.write_bytes(payload.read_bytes())
    snapshot.chmod(0o755)
    payload.unlink()
    payload.symlink_to(outside)

    with pytest.raises(RuntimeError, match="不是普通文件"):
        resolve_snapshot(cache, "symlinked", verify_hashes=True)


def test_dangling_target_is_not_replaced(tmp_path):
    cache = _cache_with_files(tmp_path, count=1)
    target = cache / "snapshots" / "reserved"
    target.parent.mkdir()
    target.symlink_to(tmp_path / "missing-target", target_is_directory=True)

    with pytest.raises(FileExistsError, match="已存在"):
        create_snapshot(cache, "reserved")
    assert target.is_symlink()


def test_snapshot_creation_rejects_input_symlink(tmp_path):
    cache = _cache_with_files(tmp_path, count=1)
    outside = tmp_path / "outside.json"
    outside.write_text('{"sector": "external"}', encoding="utf-8")
    (cache / "external.json").symlink_to(outside)

    with pytest.raises(RuntimeError, match="软链接"):
        create_snapshot(cache, "no-follow")

    assert not (cache / "snapshots" / "no-follow").exists()


def test_readonly_symbol_loader_never_downloads_missing_payload(tmp_path, monkeypatch):
    cache = tmp_path / "readonly"
    cache.mkdir()
    frame = pd.DataFrame(
        {"open": range(300), "high": range(300), "low": range(300),
         "close": range(300), "volume": range(300)}
    )
    frame.to_parquet(cache / "SPY.parquet")
    monkeypatch.setattr(
        downloader_mod,
        "download_symbol",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("只读快照不得下载")
        ),
    )

    loaded = downloader_mod.load_all(
        ["SPY", "MISSING"], "2020-01-01", "2026-01-01", cache,
        readonly=True,
    )

    assert set(loaded) == {"SPY"}
    assert not (cache / "MISSING.parquet").exists()


def test_readonly_membership_loader_never_fetches_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pit_mod.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("只读快照不得联网")
        ),
    )

    with pytest.raises(FileNotFoundError, match="只读快照"):
        pit_mod.load_membership(tmp_path, readonly=True)


def test_market_data_load_full_verifies_snapshot_before_consuming_it(tmp_path):
    cache = _cache_with_files(tmp_path, count=1)
    snapshot = create_snapshot(cache, "runtime-tamper")
    payload = snapshot / "symbol-00.csv"
    payload.chmod(0o644)
    payload.write_text("date,close\n2026-08-07,999\n", encoding="utf-8")
    cfg = {
        "data": {
            "cache_dir": str(cache),
            "snapshot": "runtime-tamper",
            "start": "2020-01-01",
            "end": "2026-08-08",
        },
        "universe": {"mode": "static"},
    }

    with pytest.raises(RuntimeError, match="全量 sha256 校验失败"):
        downloader_mod.load_market_data(cfg)


def _research_snapshot(tmp_path, market_data, *, missing=None):
    cfg = make_cfg(tmp_path)
    cache = tmp_path / "research-cache"
    cache.mkdir()
    cfg["data"].update(cache_dir=str(cache), snapshot="trusted")
    cfg["universe"]["mode"] = "sp500_pit"
    cfg["risk"]["max_per_sector"] = 3
    cfg["strategy"]["earnings_avoid_days"] = 3
    for symbol, frame in market_data.items():
        frame.to_parquet(cache / f"{symbol}.parquet")
    if missing != "sector":
        (cache / "sectors.json").write_text('{"TST":"Technology"}', encoding="utf-8")
    if missing != "membership":
        (cache / "sp500_pit.csv").write_text("date,tickers\n2020-01-01,TST\n", encoding="utf-8")
    if missing != "earnings":
        (cache / "earnings").mkdir()
        pd.DataFrame({"date": pd.to_datetime(["2026-10-01"])}).to_parquet(
            cache / "earnings" / "TST.parquet"
        )
    return cfg, create_snapshot(cache, "trusted")


def _forbid_research_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("研究快照消费不得联网补数")

    monkeypatch.setattr(downloader_mod, "download_symbol", forbidden)
    monkeypatch.setattr(downloader_mod.yf, "download", forbidden)
    monkeypatch.setattr(downloader_mod.yf, "Ticker", forbidden)
    monkeypatch.setattr(pit_mod.requests, "get", forbidden)


def test_backtest_reads_all_snapshot_dependencies_without_mutation(
    tmp_path, market_data, monkeypatch
):
    cfg, snapshot = _research_snapshot(tmp_path, market_data)
    before_cfg = copy.deepcopy(cfg)
    before = {p.relative_to(snapshot): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in snapshot.rglob("*") if p.is_file()}
    _forbid_research_network(monkeypatch)

    data = downloader_mod.load_market_data(cfg)
    result = BacktestEngine(data, cfg).run()

    assert not result.equity.empty
    assert cfg == before_cfg
    assert config_hash(cfg) == config_hash(before_cfg)
    assert verify_snapshot(snapshot, full=True)
    assert {p.relative_to(snapshot): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in snapshot.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("missing", ["sector", "earnings", "membership"])
def test_missing_snapshot_dependency_is_rejected_without_repair(
    tmp_path, market_data, monkeypatch, missing
):
    cfg, snapshot = _research_snapshot(tmp_path, market_data, missing=missing)
    _forbid_research_network(monkeypatch)

    with pytest.raises(FileNotFoundError, match="只读快照"):
        BacktestEngine(downloader_mod.load_market_data(cfg), cfg).run()
    assert verify_snapshot(snapshot, full=True)


def test_live_refresh_keeps_candidate_config_and_hash_unchanged(tmp_path, market_data, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg["data"]["snapshot"] = "research-snapshot"
    before = copy.deepcopy(cfg)
    active = tmp_path / "paper-cache"
    observed_paths = []

    def download(symbol, start, end, cache_dir, force=False):
        observed_paths.append(str(cache_dir))
        return market_data[symbol]

    monkeypatch.setattr("quantbot.price_action.data.universe.all_symbols", lambda: ["SPY", "TST"])
    monkeypatch.setattr(downloader_mod, "download_symbol", download)
    assert set(downloader_mod.load_market_data(cfg, force=True, cache_dir_override=str(active))) == {"SPY", "TST"}

    assert observed_paths == [str(active), str(active)]
    assert cfg == before
    assert config_hash(cfg) == config_hash(before)

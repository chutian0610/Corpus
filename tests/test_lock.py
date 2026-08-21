"""corpus.atomic + SQLite WAL 并发安全测试.

flock 文件锁已移除 (Phase 1.5 重构): 多 agent 并行 ingest 靠 SQLite WAL +
atomic_write_text. 此文件保留 vault_file_lock utility (corpus.lock) 给可选 advisory lock.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from corpus.errors import StorageError
from corpus.lock import is_locked, vault_file_lock


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "raw").mkdir(parents=True)
    from corpus.vault import ensure_vault, vault_paths
    from corpus.storage import init_db
    ensure_vault(v)
    init_db(vault_paths(v)["corpus_db"])
    return v


def test_vault_file_lock_releases_on_exit(vault: Path):
    with vault_file_lock(vault, exclusive=True):
        assert is_locked(vault) is True
    assert is_locked(vault) is False


def test_vault_file_lock_holder_blocks_attempt(vault: Path):
    """线程 A 持锁, 线程 B 拿锁立刻抛 StorageError."""
    barrier = threading.Barrier(2)

    def holder():
        with vault_file_lock(vault, exclusive=True):
            barrier.wait()
            time.sleep(0.5)
        barrier.wait()

    def attacker():
        barrier.wait()
        with pytest.raises(StorageError) as exc:
            with vault_file_lock(vault, exclusive=True, timeout_s=0):
                pass
        assert "locked by another process" in str(exc.value).lower()
        barrier.wait()

    th_holder = threading.Thread(target=holder)
    th_attacker = threading.Thread(target=attacker)
    th_holder.start(); th_attacker.start()
    th_holder.join(); th_attacker.join()
    assert is_locked(vault) is False


def test_is_locked_returns_false_on_unlocked(vault: Path):
    assert is_locked(vault) is False


def test_is_locked_detects_holder(vault: Path):
    with vault_file_lock(vault, exclusive=True):
        assert is_locked(vault) is True


def test_cli_concurrent_writes_all_succeed(tmp_path: Path):
    """多 agent 并行 ingest 不同 source, 都应成功 (SQLite WAL + atomic write).

    无 flock 强制锁: SQLite WAL 串行化 DB 写, atomic_write_text 防 raw/ 文件 race.
    """
    import subprocess
    vault = tmp_path / "v"
    raw = vault / "raw"
    raw.mkdir(parents=True)
    env = {**os.environ, "PYTHONPATH": "src"}
    subprocess.run(
        ["python3", "-m", "corpus", "vault", "init", str(vault), "--json"],
        cwd="/Users/didi/myprojects/CorpusBot", env=env,
        check=True, capture_output=True,
    )

    # 5 个不同 source, 并行 ingest
    sources = []
    for i in range(5):
        p = tmp_path / f"s{i}.md"
        p.write_text(f"# source {i}\ncontent {i}")
        sources.append(p)

    results = {}
    import threading
    def worker(name, src):
        r = subprocess.run(
            ["python3", "-m", "corpus", "sources", "add", str(vault), str(src), "--json"],
            cwd="/Users/didi/myprojects/CorpusBot", env=env,
            capture_output=True, text=True, timeout=15,
        )
        results[name] = (r.returncode, r.stdout, r.stderr)

    threads = [threading.Thread(target=worker, args=(f"agent-{i}", s)) for i, s in enumerate(sources)]
    for t in threads: t.start()
    for t in threads: t.join()

    # 所有 ingest 应成功 (无 flock 阻塞, 无 SQLITE_BUSY 超时)
    for name, (rc, out, err) in sorted(results.items()):
        assert rc == 0, f"{name} failed rc={rc}: {err[:200]}"

    # 所有 source 都入库了
    src_list = subprocess.run(
        ["python3", "-m", "corpus", "sources", "list", str(vault), "--json"],
        cwd="/Users/didi/myprojects/CorpusBot", env=env, capture_output=True, text=True,
    )
    items = json.loads(src_list.stdout)
    assert len(items) == 5, f"期望 5 sources, 实际 {len(items)}"

    # raw/ 下 5 个文件都 atomic write 完成 (无半写)
    raw_files = sorted(p.name for p in (vault / "raw").iterdir() if p.name not in (".tmp", ".gitkeep"))
    assert len(raw_files) == 5, f"期望 5 个 raw 文件, 实际 {len(raw_files)}: {raw_files}"
    # 每个文件 size > 0
    for f in raw_files:
        assert (vault / "raw" / f).stat().st_size > 0

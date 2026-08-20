"""corpus.lock 跨进程文件锁测试."""
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


def test_cli_concurrent_writes_second_fails(tmp_path: Path):
    """两个 corpus sources ingest 进程并发, 第二个必须立刻失败 (exit 1).

    验证场景: 一个进程持 flock sleep, 另一个进程尝试 ingest.
    """
    import subprocess
    vault = tmp_path / "v"
    raw = vault / "raw"
    raw.mkdir(parents=True)
    # init vault
    env = {**os.environ, "PYTHONPATH": "src"}
    subprocess.run(
        ["python3", "-m", "corpus", "vault", "init", str(vault), "--json"],
        cwd="/Users/didi/myprojects/CorpusBot", env=env,
        check=True, capture_output=True,
    )
    src = tmp_path / "x.md"
    src.write_text("# x\ncontent")

    # holder: 拿 flock + sleep 2s
    holder_script = (
        "import sys, os, time, fcntl\n"
        f"sys.path.insert(0, '/Users/didi/myprojects/CorpusBot/src')\n"
        "from pathlib import Path\n"
        f"vault = Path('{vault}')\n"
        "lock = vault / '.wiki-meta' / '.lock'\n"
        "fd = os.open(str(lock), os.O_CREAT | os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "os.write(fd, str(os.getpid()).encode())\n"
        "time.sleep(2)\n"
        "fcntl.flock(fd, fcntl.LOCK_UN)\n"
        "os.close(fd)\n"
    )
    proc_holder = subprocess.Popen(
        ["python3", "-c", holder_script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # 等 holder 拿锁
    time.sleep(0.3)

    # 启动 ingest (此时 vault 被锁)
    proc_ing = subprocess.run(
        ["python3", "-m", "corpus", "sources", "ingest", str(vault), str(src), "--json"],
        cwd="/Users/didi/myprojects/CorpusBot", env=env,
        capture_output=True, text=True, timeout=10,
    )
    proc_holder.wait(timeout=10)

    assert proc_ing.returncode == 1, f"ingest 应该失败, got rc={proc_ing.returncode}"
    assert "locked by another process" in proc_ing.stderr.lower()
    # holder pid 应该出现在错误里
    assert str(proc_holder.pid) in proc_ing.stderr

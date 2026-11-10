"""A killed atomic replacement must not leave bytes outside every retention budget."""

import multiprocessing
import signal

import pytest
from _client import _age


@pytest.mark.parametrize("kind", ["room", "note", "counter"])
def test_a_reap_reclaims_staging_left_by_a_killed_writer(tmp_path, monkeypatch, kind):
    import os

    import store

    store.append(tmp_path, "p-crash", "bot", "acknowledged message")
    store.note_set(tmp_path, "work", "state", "acknowledged value")
    path = {
        "room": store.room_path(tmp_path, "p-crash"),
        "note": store.note_path(tmp_path, "work", "state"),
        "counter": tmp_path / store.COUNTERS_FILE,
    }[kind]
    acknowledged = path.read_bytes()

    def interrupted_write():
        def die_before_publish(source, destination):
            os.kill(os.getpid(), signal.SIGKILL)

        monkeypatch.setattr(store.os, "replace", die_before_publish)
        store._replace(path, b"unpublished bytes" * 1024, fsync=True)

    worker = multiprocessing.get_context("fork").Process(target=interrupted_write)
    worker.start()
    worker.join(10)
    if worker.is_alive():
        worker.kill()
        worker.join(10)
        pytest.fail("the replacement did not reach its publication boundary")
    assert worker.exitcode == -signal.SIGKILL
    assert path.read_bytes() == acknowledged, "the failed write must not publish its bytes"
    staging = list(tmp_path.rglob("*.tmp"))
    assert len(staging) == 1 and staging[0].stat().st_size > 0

    monkeypatch.setattr(store, "REAP_EVERY", 0)
    store._reap(tmp_path)
    assert staging[0].exists(), "a fresh staging file may still belong to a live writer"
    _age(staging[0], store.IDLE_SECONDS + 60)
    store._reap(tmp_path)

    assert not staging[0].exists(), "a killed writer's staging bytes survived retention"
    assert path.read_bytes() == acknowledged, "cleanup must leave the published file intact"


def test_staging_cleanup_allows_a_drained_namespace_to_be_pruned(tmp_path, monkeypatch):
    import store

    store.note_set(tmp_path, "drained", "state", "value")
    path = store.note_path(tmp_path, "drained", "state")
    # The previous test produces this shape with SIGKILL; here the whole namespace ages
    # out, so the last staging file must also report the directory it has emptied.
    (path.parent / "state.txt.abandoned.tmp").write_bytes(b"unpublished")
    for entry in (tmp_path / "notes").rglob("*"):
        _age(entry, store.IDLE_SECONDS + 60)
    monkeypatch.setattr(store, "REAP_EVERY", 0)
    store._reap(tmp_path)

    assert store.note_get(tmp_path, "drained", "state") is None
    assert not (tmp_path / "notes" / "drained").exists()

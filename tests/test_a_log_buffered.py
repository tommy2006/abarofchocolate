"""Buffered decision-log writes (diagnose stage): same entries, same order, intact hash chain, other threads unaffected."""
import threading

from tpm.log.decision_log import DecisionLog


def test_buffered_keeps_order_and_chain(tmp_path):
    log = DecisionLog(tmp_path / "log.sqlite")
    log.record("system:test", "start", "t", "0")
    with log.buffered(flush_every=3):
        for i in range(1, 8):
            assert log.record("system:test", "step", "t", str(i), {"i": i}) is None
        assert log.count() == 1 + 6  # two chunks of three flushed, one entry still pending
    assert log.count() == 8
    assert [e.object_id for e in log.entries()] == [str(i) for i in range(8)]
    assert log.verify_chain()["ok"]
    assert log.record("system:test", "after", "t", "8").seq == 9  # normal writes again after the block


def test_buffered_flushes_on_error_and_is_per_thread(tmp_path):
    log = DecisionLog(tmp_path / "log.sqlite")
    seen = {}

    def other():
        seen["entry"] = log.record("system:other", "x", "t", "other")

    try:
        with log.buffered():
            log.record("system:test", "a", "t", "1")
            th = threading.Thread(target=other)
            th.start()
            th.join()
            assert seen["entry"] is not None and log.count() == 1  # the other thread wrote at once
            with log.buffered():  # nested: joins the outer block
                log.record("system:test", "b", "t", "2")
            assert log.count() == 1
            raise RuntimeError("stage failed")
    except RuntimeError:
        pass
    assert [e.object_id for e in log.entries()] == ["other", "1", "2"]
    assert log.verify_chain()["ok"]

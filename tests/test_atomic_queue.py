import os

import pytest

from claude_agent_sandbox.atomic_queue import AtomicQueue, UnknownStatusError


def _queue(tmp_path, name="a.md", statuses=("queue", "doing", "needs-you", "done")):
    q = AtomicQueue(tmp_path, statuses=statuses)
    d = tmp_path / "queue"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("# A job\n", encoding="utf-8")
    return q, p


def test_claim_moves_queue_to_doing(tmp_path):
    q, p = _queue(tmp_path)
    got = q.claim(p)
    assert got == tmp_path / "doing" / "a.md"
    assert got.exists() and not p.exists()


def test_claim_twice_returns_none_on_the_second(tmp_path):
    q, p = _queue(tmp_path)
    assert q.claim(p) is not None
    assert q.claim(p) is None  # file is gone — someone else got it


def test_claim_refuses_when_destination_already_holds_that_name(tmp_path):
    q, p = _queue(tmp_path)
    (tmp_path / "doing").mkdir(parents=True, exist_ok=True)
    (tmp_path / "doing" / "a.md").write_text("# mine\n", encoding="utf-8")
    assert q.claim(p) is None
    assert p.exists()  # ours is left where it was


def test_claim_does_not_clobber_existing_destination_content(tmp_path):
    q, p = _queue(tmp_path)
    (tmp_path / "doing").mkdir(parents=True, exist_ok=True)
    existing = tmp_path / "doing" / "a.md"
    existing.write_text("# original content\n", encoding="utf-8")
    assert q.claim(p) is None
    assert p.exists()
    assert existing.read_text(encoding="utf-8") == "# original content\n"


def test_infra_errors_propagate_not_silent_contention(tmp_path, monkeypatch):
    q, p = _queue(tmp_path)

    def fail_on_link(src, dst):
        raise PermissionError("no hard links allowed")

    monkeypatch.setattr(os, "link", fail_on_link)
    with pytest.raises(PermissionError):
        q.claim(p)


def test_move_to_does_not_overwrite_suffixes_instead(tmp_path):
    q = AtomicQueue(tmp_path)
    done = tmp_path / "done"
    done.mkdir(parents=True, exist_ok=True)
    (done / "x.md").write_text("# original\n", encoding="utf-8")
    doing = tmp_path / "doing"
    doing.mkdir(parents=True, exist_ok=True)
    new_file = doing / "x.md"
    new_file.write_text("# new\n", encoding="utf-8")

    got = q.move_to(new_file, "done")
    assert got == tmp_path / "done" / "x-2.md"
    assert got.exists()
    assert (tmp_path / "done" / "x.md").read_text(encoding="utf-8") == "# original\n"
    assert got.read_text(encoding="utf-8") == "# new\n"


def test_move_to_suffixing_is_atomic(tmp_path, monkeypatch):
    q = AtomicQueue(tmp_path)
    done = tmp_path / "done"
    done.mkdir(parents=True, exist_ok=True)
    (done / "x.md").write_text("# original\n", encoding="utf-8")
    doing = tmp_path / "doing"
    doing.mkdir(parents=True, exist_ok=True)
    new_file = doing / "x.md"
    new_file.write_text("# new\n", encoding="utf-8")

    call_count = [0]
    real_link = os.link

    def link_side_effect(src, dst):
        call_count[0] += 1
        if call_count[0] == 1 and str(dst).endswith("x.md"):
            raise FileExistsError("x.md taken")
        return real_link(src, dst)

    monkeypatch.setattr(os, "link", link_side_effect)
    got = q.move_to(new_file, "done")
    assert got == tmp_path / "done" / "x-2.md"
    assert got.exists()
    assert (tmp_path / "done" / "x.md").read_text(encoding="utf-8") == "# original\n"
    assert got.read_text(encoding="utf-8") == "# new\n"


def test_move_to_rejects_an_unknown_status(tmp_path):
    q, p = _queue(tmp_path)
    with pytest.raises(UnknownStatusError):
        q.move_to(p, "review")  # deliberately not one of the configured statuses


def test_statuses_are_caller_supplied_not_hardcoded(tmp_path):
    """The generalization seam: a queue with a completely different status vocabulary
    behaves identically, because nothing in AtomicQueue hardcodes status names."""
    q, p = _queue(tmp_path, statuses=("inbox", "in-review", "archived"))
    (tmp_path / "inbox").mkdir(parents=True, exist_ok=True)
    p2 = tmp_path / "inbox" / "b.md"
    p2.write_text("# B\n", encoding="utf-8")
    got = q.move_to(p2, "in-review")
    assert got == tmp_path / "in-review" / "b.md"
    with pytest.raises(UnknownStatusError):
        q.move_to(got, "doing")  # not in this queue's vocabulary


def test_list_jobs_is_non_recursive_and_sorted(tmp_path):
    q = AtomicQueue(tmp_path)
    d = tmp_path / "queue"
    (d / "nested").mkdir(parents=True)
    (d / "nested" / "hidden.md").write_text("# hidden\n", encoding="utf-8")
    (d / "b.md").write_text("# b\n", encoding="utf-8")
    (d / "a.md").write_text("# a\n", encoding="utf-8")
    assert [p.name for p in q.list_jobs("queue")] == ["a.md", "b.md"]


def test_list_jobs_on_missing_directory_is_empty_not_an_error(tmp_path):
    q = AtomicQueue(tmp_path)
    assert q.list_jobs("queue") == []


def test_trash_relocates_out_of_the_queue_without_destroying_the_file(tmp_path):
    q, p = _queue(tmp_path)
    trash_dir = tmp_path / "Trash"
    dest = q.trash(p, trash_dir)
    assert not p.exists()
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == "# A job\n"


def test_trash_never_clobbers_a_same_named_file_already_there(tmp_path):
    q, p = _queue(tmp_path)
    trash_dir = tmp_path / "Trash"
    trash_dir.mkdir()
    (trash_dir / "a.md").write_text("AN OLDER FILE\n", encoding="utf-8")
    dest = q.trash(p, trash_dir)
    assert (trash_dir / "a.md").read_text(encoding="utf-8") == "AN OLDER FILE\n"
    assert dest.name != "a.md"
    assert dest.read_text(encoding="utf-8") == "# A job\n"


def test_dir_for_rejects_unknown_status_up_front(tmp_path):
    q = AtomicQueue(tmp_path)
    with pytest.raises(UnknownStatusError):
        q.dir_for("blocked")  # not one of DEFAULT_STATUSES

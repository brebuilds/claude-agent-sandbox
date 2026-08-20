"""atomic_queue.py — a filesystem job queue where the directory IS the state machine,
claimed atomically with `os.link`, never `os.rename`.

Two patterns, bundled because each one is the reason the other is safe to rely on:

1. THE ATOMIC CLAIM
   POSIX `rename()` (and Python's `os.rename` / `os.replace`) SILENTLY OVERWRITES its
   destination if one already exists. That makes the obvious-looking claim —
   `if not dest.exists(): os.rename(src, dest)` — a TOCTOU race: two workers can both
   pass the `exists()` check in the gap before either renames, and the second
   `rename()` call destroys whatever the first one just wrote, with no exception and
   no warning.

   Concrete failure this causes in a multi-worker queue: worker A and worker B both
   list `queue/` and see `job-42.md`. Both decide to claim it. A calls
   `rename(queue/job-42.md, doing/job-42.md)` — succeeds. B, a few milliseconds
   behind, calls the same rename. If nothing has ever landed in `doing/job-42.md`
   this second call is *also* a silent overwrite of A's now-claimed file with B's copy
   of the same content — harmless by luck here, but the same call would silently
   clobber an unrelated file that happened to reuse that name (a retried job, an id
   collision, a stale leftover from a crashed run). Nothing tells you it happened.

   `os.link(src, dest)` instead FAILS ATOMICALLY with `FileExistsError` the instant
   `dest` already exists — the filesystem itself resolves the race, so there is no
   window between checking and acting. That is exactly the property a claim needs:
   "only one caller may succeed at giving this name to this content."

   Only `FileExistsError` is treated as normal contention ("someone else got there
   first"). Every other `OSError` (no hard-link support on this filesystem, a
   permissions problem, a full disk) is allowed to propagate — masking those as
   "contention" would make an infrastructure failure look like a race that resolved
   itself, when actually nothing claimed the job at all.

2. DIRECTORY-NAME-AS-STATE-MACHINE
   A job's status is not a field that can drift out of sync with reality — it IS
   the directory the job's file currently lives in. `queue/`, `doing/`, `done/`,
   whatever states the caller defines. There is no separate "status: doing" value
   that could disagree with where the file actually sits, because there is only one
   place that fact is recorded.

   Tradeoff, stated honestly: this buys atomic status transitions (a move IS the
   state change, and it's the same os.link + os.unlink primitive as the claim, so it
   inherits the same atomicity) and zero schema drift (nothing to migrate when a
   status is renamed — you rename a directory). It costs you cheap querying by
   anything other than status: "every job created by X regardless of state" means
   scanning every directory, because there's no index but the directories themselves.
   For a queue small enough to `glob()`, that's a fair trade. It stops being one once
   the job count is large enough that a full scan is expensive, or once you need to
   query on a dimension the directory layout doesn't express.

Extracted and genericized from a personal agent-runner (`runner.py` in the source
project — see the top-level README for what it did and how this was pulled out of
it). The root path and the set of valid statuses are both caller-supplied here; the
original had a hardcoded vault path and four fixed status names.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional

DEFAULT_STATUSES = ("queue", "doing", "needs-you", "done")


class UnknownStatusError(ValueError):
    """Raised when asked to address a status that isn't part of this queue's machine.

    Deliberately a hard error, not a fallback: inventing a fifth status on the fly
    (a typo, a copy-pasted string) would silently create a new directory nothing else
    reads from, which is a worse failure mode than refusing loudly.
    """


class AtomicQueue:
    """A job queue whose statuses are subdirectories of `root`, claimed and moved via
    atomic hardlink-then-unlink.

    Not a database, not a process, not a lock. It is a thin, dependency-free wrapper
    around the two filesystem primitives above. Suitable for the scale a directory
    listing can comfortably represent — see the state-machine tradeoff note above.
    """

    def __init__(self, root: os.PathLike, statuses: Iterable[str] = DEFAULT_STATUSES):
        self.root = Path(root)
        self.statuses = tuple(statuses)
        if not self.statuses:
            raise ValueError("a queue needs at least one status")

    # -- addressing -------------------------------------------------------------

    def dir_for(self, status: str) -> Path:
        if status not in self.statuses:
            raise UnknownStatusError(
                f"{status!r} is not one of {self.statuses} — do not invent a new one"
            )
        return self.root / status

    def list_jobs(self, status: str) -> List[Path]:
        """Job files currently in `status`, sorted by name. Non-recursive on purpose:
        a stray subdirectory (old archived canaries, a `.DS_Store` folder, whatever)
        is invisible to this without needing to be specially excluded."""
        d = self.dir_for(status)
        if not d.is_dir():
            return []
        return sorted(p for p in d.glob("*") if p.is_file())

    # -- the shared primitive ----------------------------------------------------

    def _link_into(self, src: Path, dest_dir: Path, name: str) -> Optional[Path]:
        """Atomically create dest_dir/name as a hardlink to src. Returns None if that
        name is already taken in dest_dir — meaning someone else got there first.

        Every move in this class routes through here so the no-clobber guarantee is
        defined exactly once, instead of re-derived (and potentially re-gotten-wrong)
        at each call site.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / name
        try:
            os.link(src, target)
        except FileExistsError:
            return None
        return target

    # -- operations ---------------------------------------------------------------

    def claim(self, path: Path, to_status: str = "doing") -> Optional[Path]:
        """Move a job from wherever `path` is into `to_status`, atomically.

        Returns the new path, or None if someone else claimed it first (or it
        vanished from under us — same outcome: we don't have it).
        """
        dest_dir = self.dir_for(to_status)
        try:
            target = self._link_into(path, dest_dir, path.name)
        except FileNotFoundError:
            return None  # the source vanished — someone else already claimed it
        if target is None:
            return None
        try:
            os.unlink(path)
        except OSError:
            pass  # a duplicate left behind in the source dir is visible and recoverable
        return target

    def move_to(self, path: Path, status: str) -> Path:
        """Move a job to another status. This is the only "set status" there is.

        Never overwrites: if the destination name is taken, a numeric suffix is
        appended instead. A job id can legitimately be filed twice (e.g. a retried
        run) and silently losing the earlier record to an overwrite would lose real
        work.

        Unlike claim(), a vanished source here raises rather than returning None: by
        this point the caller already owns the job (typically just claimed it), so
        the file disappearing out from under them is a bug to surface, not normal
        contention to absorb quietly.
        """
        return self._relocate(path, self.dir_for(status))

    def trash(self, path: Path, trash_dir: os.PathLike) -> Path:
        """Move a job out of the queue entirely, into an arbitrary directory that is
        not one of this queue's statuses (a recycle bin, an archive — caller's call).

        "Delete" in a shared queue read by other processes should mean "stop showing
        up anywhere", not "destroy irrecoverably" — a mistaken destroy can propagate
        to every reader before anyone notices. Relocating instead of unlinking keeps
        a recovery path.
        """
        return self._relocate(path, Path(trash_dir))

    def _relocate(self, path: Path, dest: Path) -> Path:
        """Move `path` into `dest`, suffixing the name rather than ever overwriting.

        Lives once, here, for the same reason `_link_into` does: every caller that
        files something away needs the identical no-clobber guarantee, and
        re-deriving it per call site is how a TOCTOU bug gets reintroduced.
        """
        n = 1
        while True:
            name = path.name if n == 1 else f"{path.stem}-{n}{path.suffix}"
            target = self._link_into(path, dest, name)
            if target is not None:
                break
            n += 1
            if n > 999:
                raise OSError(f"cannot file {path.name} into {dest} — 999 names taken")
        try:
            os.unlink(path)
        except OSError:
            pass
        return target

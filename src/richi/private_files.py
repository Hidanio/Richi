"""Small files of this user that several processes change: the agent settings, the map's edit key.

One way to replace such a file and to keep its writers apart, for the modules
that own one. Like the launcher's own files, a file is on disk before it
replaces the old one, and only this user may open it or its lock.
"""
import contextlib
import os
import secrets
import threading
import time


# Where the operating system has no file locks, the threads of one process still take turns.
_threads = threading.Lock()
# How often a lock with a time limit is tried again.
_RETRY = 0.05


def replace(path, text):
    """Replace the file atomically with one only this user can read, written to disk first."""
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(6) + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            # Otherwise a power loss right after the rename may leave an empty file behind.
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(str(temporary))
        raise


@contextlib.contextmanager
def locked(path, timeout=None):
    """One holder at a time, across processes and threads; the lock is the file path + ".lock".

    Whoever reads the file, decides and then writes it holds the lock for all
    three, so that no other writer's change falls in between and is lost. The
    lock file stays: two writers must always lock the same file. With timeout
    (seconds), TimeoutError when another holder keeps it longer.
    """
    try:
        import fcntl
    except ImportError:
        if not _threads.acquire(timeout=-1 if timeout is None else timeout):
            raise TimeoutError("Another process holds " + str(path) + ".lock") from None
        try:
            yield
        finally:
            _threads.release()
        return
    descriptor = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        # A lock file made by an earlier version may still be readable by others.
        with contextlib.suppress(OSError):
            os.fchmod(descriptor, 0o600)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                # Each holder opens the file itself, so the lock also keeps two threads of one process apart.
                fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if deadline is None else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another process holds " + str(path) + ".lock") from None
                time.sleep(_RETRY)
        yield
    finally:
        os.close(descriptor)

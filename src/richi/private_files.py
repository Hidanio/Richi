"""Small files of this user that several processes change: the agent settings, the map's edit key.

One way to replace such a file and to keep its writers apart, for the modules
that own one.
"""
import contextlib
import os
import secrets
import threading


# Where the operating system has no file locks, the threads of one process still take turns.
_threads = threading.Lock()


def replace(path, text):
    """Replace the file atomically with one only this user can read."""
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(6) + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(str(temporary), str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(str(temporary))
        raise


@contextlib.contextmanager
def locked(path):
    """One holder at a time, across processes and threads; the lock is the file path + ".lock".

    Whoever reads the file, decides and then writes it holds the lock for all
    three, so that no other writer's change falls in between and is lost.
    """
    try:
        import fcntl
    except ImportError:
        with _threads:
            yield
        return
    with open(str(path) + ".lock", "a") as handle:
        # Each holder opens the file itself, so the lock also keeps two threads of one process apart.
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield

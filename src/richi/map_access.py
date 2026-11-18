"""Who may change standing rules and agent settings through the loopback map.

The map serves knowledge read-only to any loopback client of its port. Rules
are different: agents follow them as the user's instructions, and agent
settings decide which skill folders are read, so a page that changes either
must show it was opened by this user. `richi map` writes a
one-time ticket next to the store, readable only by the user, and opens the
map with that ticket in the URL fragment, which browsers never send to a
server. The page redeems the ticket once, within TICKET_SECONDS, for a new
edit secret, keeps the secret in its session storage and sends it with every
change and every reading of the rules and the agents, which tells it whether
it may edit. A ticket seen afterwards is spent, and opening the map again
replaces the secret. The secret also ends with the map: a map that starts
other than by handing itself over forgets it, and a map that stops forgets the
one its pages held (forget_secret), so a page left open cannot hand a still
valid key to whatever answers on that port later. A page opened without a
valid one, such as a bookmark or an earlier tab, still shows the rules and
composes the commands that change them. The ticket and the secret belong to
one store: stores kept in one directory do not share them, and two maps of one
store share one. So a map records beside the store on which port it listens
(record_port), and richi map reuses the map it finds there instead of starting
a second one; a second map started by hand (richi map serve) still ends the
first one's key.

Where the browser would receive the ticket as a command-line argument, which
other local users can list, the map is opened through a private redirect file
instead, so only the file's path appears there. A browser in a sandbox (a
snap or flatpak package) cannot read that file, so when the ticket is still
unused after FALLBACK_SECONDS the map's plain address is opened as well: the
map always appears, there composing commands instead of saving.
"""
import errno
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
import webbrowser

from . import private_files


TICKET_SECONDS = 120
FALLBACK_SECONDS = 5
# How long issuing or redeeming a ticket waits for the other; each holds the lock for a moment.
LOCK_SECONDS = 10
# How long a stopping map waits for a page being given a key: that key is a newer one, and it stays.
FORGET_LOCK_SECONDS = 2
HEADER = "X-Richi-Map-Secret"
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")


def runtime_dir(database, create=False):
    directory = Path(database).resolve().parent / "runtime"
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def _file(database, name, suffix="", create=False):
    """A file of this store's map. The directory may serve several stores, so the name carries the store."""
    store = hashlib.sha256(os.fsencode(str(Path(database).resolve()))).hexdigest()[:16]
    return runtime_dir(database, create) / (name + "-" + store + suffix)


def ticket_path(database, create=False):
    return _file(database, "map-edit-ticket", create=create)


def secret_path(database, create=False):
    return _file(database, "map-edit-secret", create=create)


def redirect_path(database, create=False):
    return _file(database, "map-open", ".html", create)


def _read_private(path):
    """The file's text if it is a regular file only this user can read, else None.

    OSError when the file is there but cannot be read now, such as with too
    many open files: that says nothing about the key, so it is not taken for
    "no key" (a page would drop a key that still works).
    """
    try:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        # Missing, or a link (O_NOFOLLOW refuses one with ELOOP, on some systems EMLINK): no usable file.
        if isinstance(exc, (FileNotFoundError, NotADirectoryError)) or exc.errno in (errno.ELOOP, errno.EMLINK):
            return None
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        return os.read(descriptor, 4096).decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)


def secret(database):
    """The current edit secret of this store's map, or None when there is no usable one; OSError (_read_private)."""
    value = (_read_private(secret_path(database)) or "").strip()
    return value if _TOKEN.fullmatch(value) else None


def _new_secret(database):
    # Each opened page gets a new secret, so a secret that leaked stops
    # working when the user next opens the map; earlier pages compose commands.
    value = secrets.token_hex(32)
    private_files.replace(secret_path(database, create=True), value + "\n")
    return value


def port_path(database, create=False):
    return _file(database, "map-port", create=create)


def record_port(database, port):
    """Say beside the store on which port its map listens, so that richi map finds it in every installation."""
    path = port_path(database, create=True)
    with private_files.locked(path, FORGET_LOCK_SECONDS):
        private_files.replace(path, json.dumps({"port": port, "pid": os.getpid()}))


def recorded_port(database):
    """The port the store's map recorded, or None. Only a hint: whoever uses it checks the map that answers there."""
    try:
        value = json.loads(_read_private(port_path(database)) or "null")
    except (OSError, ValueError):
        return None
    port = value.get("port") if isinstance(value, dict) else None
    return port if type(port) is int and 1 <= port <= 65535 else None


def clear_port(database, port):
    """Drop the record while it still names this process and port: a map started after this one keeps its own."""
    path = port_path(database)
    try:
        with private_files.locked(path, FORGET_LOCK_SECONDS):
            value = json.loads(_read_private(path) or "null")
            if isinstance(value, dict) and value.get("pid") == os.getpid() and value.get("port") == port:
                os.unlink(str(path))
    except (OSError, ValueError):
        # TimeoutError is an OSError: a map recording itself right now is newer than this one.
        pass


def forget_secret(database, expected=None):
    """End the edit key of this store's map: its pages compose commands until richi map opens a new one.

    expected: end only that key, the one a stopping map's pages held. A map
    started meanwhile (richi map restart goes on when the old map is slow to
    finish its requests) may have given its page a new one, which stays.
    """
    path = secret_path(database)
    if expected is not None:
        if not path.parent.is_dir():
            return
        try:
            # The lock with which a page redeems its ticket: no new key is written in between.
            with private_files.locked(ticket_path(database), FORGET_LOCK_SECONDS):
                if secret(database) != expected:
                    return
                os.unlink(str(path))
        except (FileNotFoundError, TimeoutError):
            # Gone already, or a page is being given a key right now: that one is newer.
            pass
        return
    try:
        os.unlink(str(path))
    except FileNotFoundError:
        pass


def authorized(database, value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return False
    current = secret(database)
    return current is not None and hmac.compare_digest(current, value)


def issue_ticket(database, now=None):
    ticket = secrets.token_hex(32)
    expires = (time.time() if now is None else now) + TICKET_SECONDS
    path = ticket_path(database, create=True)
    # Not while a page redeems the previous ticket: this one would be taken for it and deleted.
    with private_files.locked(path, LOCK_SECONDS):
        private_files.replace(path, json.dumps({"ticket": ticket, "expires": expires}))
    return ticket


def redeem(database, ticket, now=None):
    """The edit secret for a valid unspent ticket, else None. A ticket works once.

    OSError when the secret cannot be written or the ticket cannot be spent:
    no secret is given out then, and the caller says so instead of calling
    the ticket invalid.
    """
    if not isinstance(ticket, str) or not _TOKEN.fullmatch(ticket):
        return None
    path = ticket_path(database)
    if not path.parent.is_dir():
        return None
    # One at a time, also against a new ticket being issued by another process.
    with private_files.locked(path, LOCK_SECONDS):
        try:
            issued = json.loads(_read_private(path) or "null")
            expected, expires = issued["ticket"], float(issued["expires"])
        except (ValueError, TypeError, KeyError):
            return None
        if not isinstance(expected, str) or not hmac.compare_digest(expected, ticket):
            return None
        if expires < (time.time() if now is None else now):
            try:
                os.unlink(str(path))
            except OSError:
                pass
            return None
        # The secret first: a ticket is spent only once there is something to give for it.
        value = _new_secret(database)
        try:
            os.unlink(str(path))
        except FileNotFoundError:
            return None
        return value


def _argument_safe():
    """The system browser can be told the URL outside any argument list (_tell_system_browser)."""
    return sys.platform == "darwin" and not os.environ.get("BROWSER")


def _tell_system_browser(url):
    """Open the URL with macOS's own controller, which hands it to osascript on standard input.

    webbrowser.open would go on to the next registered browser when this one
    fails, and that one may take the URL, ticket included, as an argument.
    """
    try:
        return bool(webbrowser.get("MacOSX").open(url))
    except (webbrowser.Error, OSError):
        return False


def _unused(database, ticket):
    """Whether this ticket is still the one waiting to be redeemed."""
    try:
        return json.loads(_read_private(ticket_path(database)) or "null")["ticket"] == ticket
    except (ValueError, TypeError, KeyError):
        return False
    except OSError:
        # Unknown for now: the wait goes on, and at its end the plain address is opened anyway.
        return True


def open_browser(address, database, opener=None, wait=FALLBACK_SECONDS, sleep=time.sleep, problems=None):
    """Open the map so that this page may change rules; returns whether a browser was opened.

    opener replaces the browser in tests: it takes the URL and says whether it
    opened. problems, a list, receives why the page cannot edit, if it cannot.
    """
    try:
        ticket = issue_ticket(database)
    except OSError as exc:
        # Without a ticket the map still opens; it composes commands instead of saving.
        if problems is not None:
            problems.append("The edit ticket could not be written beside the store (" + (exc.strerror or str(exc))
                            + "), so this page composes commands instead of saving changes")
        return (opener or webbrowser.open)(address)
    target = address + "#ticket=" + ticket
    if _argument_safe() and (opener or _tell_system_browser)(target):
        return True
    # Anywhere else a browser would get the URL as an argument: it gets the path of a private file instead.
    opener = opener or webbrowser.open
    try:
        redirect = redirect_path(database, create=True)
        link = html.escape(target, quote=True)
        private_files.replace(redirect, '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                                 '<meta http-equiv="refresh" content="0;url=' + link + '"><title>Richi</title>'
                                 '<p><a href="' + link + '">Открыть карту Richi</a></p>\n')
    except OSError:
        return opener(address)
    opened = opener(redirect.as_uri())
    if opened and wait:
        # The page redeems its ticket as soon as it loads. A ticket nobody used means the
        # browser could not read the private file, so the map is opened by its address.
        deadline = time.monotonic() + wait
        while _unused(database, ticket):
            if time.monotonic() >= deadline:
                opener(address)
                break
            sleep(0.2)
    return opened

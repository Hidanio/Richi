"""Who may change standing rules and agent settings through the loopback map.

The map serves knowledge read-only to any loopback client of its port. Rules
are different: agents follow them as the user's instructions, and agent
settings decide which skill folders are read, so a page that changes either
must show it was opened by this user. `richi map` writes a
one-time ticket next to the store, readable only by the user, and opens the
map with that ticket in the URL fragment, which browsers never send to a
server. The page redeems the ticket once, within TICKET_SECONDS, for a new
edit secret, keeps the secret in its session storage and sends it with every
change. A ticket seen afterwards is spent, and opening the map again replaces
the secret. A page opened without a valid one, such as a bookmark or an
earlier tab, still shows the rules and composes the commands that change them.

Where the browser would receive the ticket as a command-line argument, which
other local users can list, the map is opened through a private redirect file
instead, so only the file's path appears there. A browser in a sandbox (a
snap or flatpak package) cannot read that file, so when the ticket is still
unused after FALLBACK_SECONDS the map's plain address is opened as well: the
map always appears, there composing commands instead of saving.
"""
import hmac
import html
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading
import time
import webbrowser


SECRET_FILE = "map-edit-secret"
TICKET_FILE = "map-edit-ticket"
REDIRECT_FILE = "map-open.html"
TICKET_SECONDS = 120
FALLBACK_SECONDS = 5
HEADER = "X-Richi-Map-Secret"
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_redeeming = threading.Lock()


def runtime_dir(database, create=False):
    directory = Path(database).resolve().parent / "runtime"
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def _read_private(path):
    """The file's text if it is a regular file only this user can read, else None."""
    try:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        return os.read(descriptor, 4096).decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)


def _write_private(path, text):
    """Replace the file atomically with one only this user can read."""
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(6) + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(str(temporary), str(path))
    except BaseException:
        try:
            os.unlink(str(temporary))
        except OSError:
            pass
        raise


def secret(database):
    """The current edit secret of this store's map, or None when there is no usable one."""
    value = (_read_private(runtime_dir(database) / SECRET_FILE) or "").strip()
    return value if _TOKEN.fullmatch(value) else None


def _new_secret(database):
    # Each opened page gets a new secret, so a secret that leaked stops
    # working when the user next opens the map; earlier pages compose commands.
    value = secrets.token_hex(32)
    _write_private(runtime_dir(database, create=True) / SECRET_FILE, value + "\n")
    return value


def authorized(database, value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return False
    current = secret(database)
    return current is not None and hmac.compare_digest(current, value)


def issue_ticket(database, now=None):
    ticket = secrets.token_hex(32)
    expires = (time.time() if now is None else now) + TICKET_SECONDS
    _write_private(runtime_dir(database, create=True) / TICKET_FILE,
                   json.dumps({"ticket": ticket, "expires": expires}))
    return ticket


def redeem(database, ticket, now=None):
    """The edit secret for a valid unspent ticket, else None. A ticket works once."""
    if not isinstance(ticket, str) or not _TOKEN.fullmatch(ticket):
        return None
    with _redeeming:
        path = runtime_dir(database) / TICKET_FILE
        try:
            issued = json.loads(_read_private(path) or "null")
            expected, expires = issued["ticket"], float(issued["expires"])
        except (ValueError, TypeError, KeyError):
            return None
        if not isinstance(expected, str) or not hmac.compare_digest(expected, ticket):
            return None
        try:
            os.unlink(str(path))
        except FileNotFoundError:
            return None
        if expires < (time.time() if now is None else now):
            return None
        return _new_secret(database)


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
        return json.loads(_read_private(runtime_dir(database) / TICKET_FILE) or "null")["ticket"] == ticket
    except (ValueError, TypeError, KeyError):
        return False


def open_browser(address, database, opener=None, wait=FALLBACK_SECONDS, sleep=time.sleep):
    """Open the map so that this page may change rules; returns whether a browser was opened.

    opener replaces the browser in tests: it takes the URL and says whether it opened.
    """
    try:
        ticket = issue_ticket(database)
    except OSError:
        # Without a ticket the map still opens; it composes commands instead of saving.
        return (opener or webbrowser.open)(address)
    target = address + "#ticket=" + ticket
    if _argument_safe() and (opener or _tell_system_browser)(target):
        return True
    # Anywhere else a browser would get the URL as an argument: it gets the path of a private file instead.
    opener = opener or webbrowser.open
    try:
        redirect = runtime_dir(database, create=True) / REDIRECT_FILE
        link = html.escape(target, quote=True)
        _write_private(redirect, '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
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

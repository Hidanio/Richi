"""Overlapping map processes updating the installation's port registry."""
import fcntl
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from richi_launcher.maps import registry


def _finish_old_map(port, action, pipe):
    path = registry.register(port)
    read = registry.read_json

    def pause_after_read(path):
        value = read(path)
        pipe.send("checked")
        if not pipe.poll(10):
            raise TimeoutError("Replacement did not reach the registry")
        pipe.recv()
        return value

    with mock.patch.object(registry, "read_json", side_effect=pause_after_read):
        getattr(registry, action)(path)


class MapRegistrationTests(unittest.TestCase):
    def test_unavailable_lock_preserves_existing_registration(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
                os.environ, {registry.ROOT_ENV: temp, registry.GENERATION_ENV: "g2"}):
            (Path(temp) / "state.json").write_text("{}")
            entries = registry.directory(temp, create=True)
            path = entries / "41001.json"
            original = {"pid": os.getpid(), "port": 41001}
            registry.atomic_json(path, original)
            (entries / "registrations.lock").mkdir()
            self.assertIsNone(registry.register_store(41001, Path(temp) / "new.sqlite3"))
            registry.mark_restarting(path)
            registry.unregister(path)
            self.assertEqual(registry.read_json(path), original)

    def test_new_registration_survives_an_old_maps_pending_change(self):
        context = multiprocessing.get_context("spawn")
        port = 41001
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
                os.environ, {registry.ROOT_ENV: temp, registry.GENERATION_ENV: "g2"}):
            (Path(temp) / "state.json").write_text("{}")
            database = Path(temp) / "new.sqlite3"
            for action in ("unregister", "mark_restarting"):
                with self.subTest(action=action):
                    parent, child = context.Pipe()
                    process = context.Process(target=_finish_old_map, args=(port, action, child))
                    process.start()
                    child.close()
                    released = False

                    def release_old():
                        nonlocal released
                        if not released:
                            parent.send("continue")
                            released = True

                    flock = fcntl.flock

                    def try_lock(descriptor, flags):
                        # Release the old process when its lock blocks the replacement.
                        # Without a shared lock it resumes after the replacement has committed.
                        try:
                            return flock(descriptor, flags | fcntl.LOCK_NB)
                        except BlockingIOError:
                            release_old()
                            return flock(descriptor, flags)

                    try:
                        self.assertTrue(parent.poll(10), "Old map did not read its registration")
                        self.assertEqual(parent.recv(), "checked")
                        with mock.patch.object(fcntl, "flock", side_effect=try_lock):
                            path = registry.register_store(port, database)
                        release_old()
                        process.join(10)
                        self.assertEqual(process.exitcode, 0)
                        entry = registry.read_json(path)
                        self.assertIsNotNone(entry)
                        self.assertEqual((entry["pid"], entry["database"]), (os.getpid(), str(database)))
                        self.assertNotIn("restarting", entry)
                    finally:
                        if process.is_alive():
                            process.terminate()
                            process.join(10)
                        parent.close()

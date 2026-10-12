"""One lock for everything that changes a station's schedule or catalog.

Several programs in this container can ask the console to build a day of schedule or rebuild a catalog: the
dashboard (Stations page and the schedule keeper), the first-start job, the channel tools, and the sync
service. They are separate processes. If two of them extend the same station at the same moment, both read
the same "end of schedule" and build the SAME day twice, leaving overlapping blocks. So each takes this lock
for the length of one build (one day, or one catalog rebuild), and they take turns. It is an advisory file
lock in /tmp (local to the container), and it is released when the holder exits, even if it crashes.
"""
import contextlib
import fcntl
import os
import tempfile
import time

PATH = os.environ.get("FS42_SCHEDULE_LOCK", os.path.join(tempfile.gettempdir(), "fs42-schedule.lock"))


class LockTimeout(Exception):
    pass


@contextlib.contextmanager
def held(timeout=3600, poll=0.2, on_wait=None):
    """Wait for the lock (up to `timeout` seconds), hold it for the body, then release it.
    on_wait() is called once if the lock is not free straight away, so a page can say what it is waiting for."""
    fd = os.open(PATH, os.O_CREAT | os.O_RDWR, 0o664)
    try:
        end = time.time() + timeout
        told = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if not told and on_wait:
                    told = True
                    on_wait()
                if time.time() > end:
                    raise LockTimeout("another schedule change has been running for too long")
                time.sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

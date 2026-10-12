"""The private folder the container's own services keep their small signal files in.

The schedule lock, the "a build is running" mark, "convert now" / "new channels" and the TubeArchivist job's status
used to sit directly in /tmp, under fixed names, where any other process on the machine could create or replace
them. They live in one folder that only the user the services run as can open (mode 0700).

The entrypoint makes it (/run/fs42, FS42_RUN_DIR) for the user it starts the services as. Anywhere else (the tests,
a run from the repo) it is made on first use under the system temp folder, one per user.
"""
import os
import stat
import tempfile


def directory():
    path = os.environ.get("FS42_RUN_DIR") or os.path.join(tempfile.gettempdir(), f"fs42-run-{os.getuid()}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise OSError(f"{path} is not a private folder of this user's (mode 0700); not using it for the services' signal files")
    return path


def path(name):
    """The full path of one signal file in the private folder."""
    return os.path.join(directory(), name)

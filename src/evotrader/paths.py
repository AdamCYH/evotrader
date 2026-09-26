"""Where the app finds its project folder, its data folder and the broker sign-in.

Code and data live apart. The code can be public; the data folder is each
user's own: settings, agent instructions, algorithm versions, the trade journal,
memory and keys. By default it is ``data/`` inside the project. Set
``EVOTRADER_DATA_DIR`` (or pass ``--data-dir``) to keep it anywhere else — for
example in a private repository of its own, so updating the code never touches
it.

Every path into the data folder should come from :func:`data_dir`, so that one
setting moves all of them together.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

DATA_DIR_ENV = "EVOTRADER_DATA_DIR"


def project_root() -> Path:
    """The folder holding ``pyproject.toml``, searched upwards from the working directory.

    Falls back to this package's own checkout, then to the working directory.
    """
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for folder in (start, *start.parents):
            if (folder / "pyproject.toml").exists():
                return folder
    return Path.cwd()


def data_dir(root: Path | None = None) -> Path:
    """The data folder: ``EVOTRADER_DATA_DIR`` when set, else ``data/`` in the project."""
    configured = os.environ.get(DATA_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (root or project_root()) / "data"


SIGNIN_DIR_ENV = "EVOTRADER_SIGNIN_DIR"


def signin_dir() -> Path:
    """Where the broker sign-in is kept: ``EVOTRADER_SIGNIN_DIR`` when set, else ``~/.evotrader``.

    Outside the data folder on purpose: the sign-in belongs to the person at
    this computer, not to a data folder that may be copied, shared or kept in a
    repository.
    """
    configured = os.environ.get(SIGNIN_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".evotrader"


# Set by run.sh and scripts/setup.sh: the folder the command was typed in.
CALLER_DIR_ENV = "EVOTRADER_CALLER_DIR"


def run_command(args: str = "") -> str:
    """What to type for ``./run.sh ARGS`` on the data folder in use.

    Names the data folder when it isn't the default (advice that drops it would
    set up or start the wrong one), and gives the path to run.sh from where the
    person typed the command, when that isn't the project folder.
    """
    root = project_root()
    script = "./run.sh"
    caller = os.environ.get(CALLER_DIR_ENV, "").strip()
    if caller and Path(caller).resolve() != root.resolve():
        relative = os.path.relpath(root / "run.sh", caller)
        script = str(root / "run.sh") if relative.startswith("../..") else relative
    command = f"{script} {args}".strip()
    folder = data_dir(root)
    if folder != root / "data":
        command += f' --data-dir "{folder}"'
    return command


_held: dict[Path, IO[str]] = {}


def hold(folder: Path) -> int | None:
    """Take ``folder`` for this process; the process id of another that has it.

    One EvoTrader per data folder and per broker sign-in: two would both trade
    the same account, and two refreshing one sign-in can invalidate it. The
    lock lives in ``folder/.evotrader.lock`` and goes when the process ends,
    however it ends. None: taken (or locks unsupported, as on Windows); -1:
    held by a process that left no id.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        return None
    folder = folder.resolve()
    if folder in _held:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / ".evotrader.lock", "a+", encoding="utf-8")  # noqa: SIM115
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        holder = handle.read().strip()
        handle.close()
        return int(holder) if holder.isdigit() else -1
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    _held[folder] = handle  # held open for the life of the process
    return None

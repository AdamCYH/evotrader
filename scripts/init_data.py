#!/usr/bin/env python3
"""Create your data folder from the starter data.

Copies every file in ``starter_data/`` into your data folder — ``data/`` in the
project, or wherever ``EVOTRADER_DATA_DIR`` points — and creates the empty
folders the app writes into while it runs. Then it checks that the settings and
the constitution load.

Safe to run again: a file that already exists is never overwritten, only
reported as skipped, so your own settings, instructions and notes are kept.
Delete a file first if you want its starter version back.

Usage::

    ./run.sh --init                              # the usual way
    uv run python scripts/init_data.py           # the same
    uv run python scripts/init_data.py FOLDER    # a data folder of your choice
"""

from __future__ import annotations

import argparse
import contextlib
import io
import shutil
import sys
from pathlib import Path

try:
    from evotrader import paths
except ImportError:  # run outside the project's environment: use this checkout
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from evotrader import paths

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STARTER_DIR = PROJECT_ROOT / "starter_data"

# Folders the app writes into at run time. They start empty; the app would make
# most of them on first use, but creating them now shows the layout up front.
RUNTIME_DIRS = (
    "db",  # live mode: trade journal and metrics
    "sessions",
    "memory",  # semantic memory of past trades and your notes
    "sim/db",  # paper trading: journal and the simulated broker
    "sim/sessions",
    "sim/memory",
    "trading/notes",  # the strategy agent's note to its own next cycle
    "logs",  # ./run.sh offline writes cycle logs here
)

# A file with this name only keeps an empty folder in git: the folder is
# created, the file itself is not copied.
FOLDER_MARKER = ".gitkeep"

# Every agent whose instructions the app loads at start.
AGENTS = ("orchestrator", "news_sentiment", "strategy", "risk_manager", "executor", "evolution")


def copy_starter_files(source: Path, target: Path) -> tuple[int, int]:
    """Copy each starter file into ``target`` unless a file is already there.

    Returns how many files were created and how many were skipped.
    """
    created = skipped = 0
    for item in sorted(source.rglob("*")):
        if item.is_dir() or item.name == ".DS_Store":
            continue
        relative = item.relative_to(source)
        destination = target / relative
        if item.name == FOLDER_MARKER:
            if not destination.parent.is_dir():
                destination.parent.mkdir(parents=True)
                print(f"  + folder  {relative.parent}/")
            continue
        if destination.exists():
            print(f"  = skipped {relative}  (already there)")
            skipped += 1
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Content only: the new file gets normal permissions, whatever the
        # checkout's are, so it can always be edited afterwards.
        shutil.copyfile(item, destination)
        print(f"  + created {relative}")
        created += 1
    return created, skipped


def create_runtime_dirs(target: Path) -> None:
    """Create the empty folders the app writes into at run time."""
    for name in RUNTIME_DIRS:
        folder = target / name
        if not folder.is_dir():
            folder.mkdir(parents=True)
            print(f"  + folder  {name}/")


def check_data_folder(target: Path) -> list[str]:
    """Problems that would stop the app from starting; an empty list when it can.

    Loads the constitution and settings with the app's own validation, and
    checks that every agent's active instruction file exists.
    """
    import yaml

    from evotrader.models.config import Constitution, Settings

    problems: list[str] = []
    for name, model in (("constitution.yaml", Constitution), ("settings.yaml", Settings)):
        path = target / name
        try:
            model.model_validate(yaml.safe_load(path.read_text()) or {})
        except FileNotFoundError:
            problems.append(f"{name} is missing")
        except Exception as e:  # a YAML or validation error, shown as the app would
            problems.append(f"{name} does not load: {e}")

    for agent in AGENTS:
        pointer = target / "instructions" / agent / "active.txt"
        if not pointer.is_file():
            problems.append(f"instructions/{agent}/active.txt is missing")
            continue
        version = pointer.read_text().strip()
        if not (pointer.parent / f"{version}.md").is_file():
            problems.append(
                f"instructions/{agent}/{version}.md is missing (active.txt points to it)"
            )

    return problems


def init_data(target: Path) -> int:
    """Fill ``target`` from the starter data. Returns a process exit code."""
    print()
    print("═" * 60)
    print("  EvoTrader — data folder")
    print("═" * 60)
    print(f"  Starter data: {STARTER_DIR}")
    print(f"  Data folder:  {target}")
    print()

    if not STARTER_DIR.is_dir():
        print(f"  ✗ The starter data is missing: {STARTER_DIR}", file=sys.stderr)
        return 1
    if target.exists() and not target.is_dir():
        print(f"  ✗ {target} exists and is not a folder.", file=sys.stderr)
        return 1

    target.mkdir(parents=True, exist_ok=True)
    created, skipped = copy_starter_files(STARTER_DIR, target)
    create_runtime_dirs(target)
    print()
    print(f"  {created} file(s) created, {skipped} skipped because they already exist.")

    problems = check_data_folder(target)
    if problems:
        print()
        print("  ✗ The app will not start until these are fixed:", file=sys.stderr)
        for problem in problems:
            print(f"    - {problem}", file=sys.stderr)
        return 1

    print("  ✓ Settings, constitution and agent instructions load.")
    print()
    print("  Paper trading (mode: sim) is the default. Review the risk limits in")
    print("  constitution.yaml before trading real money.")
    setup = "./run.sh setup"
    if target != PROJECT_ROOT / "data":
        setup += f' --data-dir "{target}"'
    print(f"  To choose AI models and save your keys: {setup}")
    print("═" * 60)
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create the data folder from starter_data/. Existing files are never "
            "overwritten, so it is safe to run again."
        )
    )
    parser.add_argument(
        "folder",
        nargs="?",
        help="Data folder to fill. Default: EVOTRADER_DATA_DIR if set, else data/ in the project.",
    )
    parser.add_argument("--data-dir", help="The same as FOLDER, as the app's own flag spells it.")
    parser.add_argument(
        "--quiet", action="store_true", help="Print only problems (setup uses this)."
    )
    args = parser.parse_args(argv)

    if args.folder and args.data_dir and Path(args.folder) != Path(args.data_dir):
        parser.error("give the data folder once: either FOLDER or --data-dir")
    chosen = args.folder or args.data_dir
    target = Path(chosen).expanduser().resolve() if chosen else paths.data_dir(root=PROJECT_ROOT)
    if args.quiet:  # problems go to stderr, so they still show
        with contextlib.redirect_stdout(io.StringIO()):
            return init_data(target)
    return init_data(target)


if __name__ == "__main__":
    sys.exit(main())

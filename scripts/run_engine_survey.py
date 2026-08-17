"""Run the canonical engine's estate survey with this repository's Tableau credentials.

usage: python scripts/run_engine_survey.py --server <host> --site <site> --pat-name <name> --json <path>

Pass any ``estate_survey.py`` arguments after this wrapper. It reads ``--env-file`` (default:
``.env``), accepts either historical PAT secret spelling, and exports both names to the engine.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from engine_source import engine_scripts_dir
from tableau_env import engine_child_env, require, resolve_env


def _env_file(arguments: list[str]) -> Path:
    """Return the engine's ``--env-file`` value without consuming its arguments."""
    for index, argument in enumerate(arguments):
        if argument == "--env-file":
            if index + 1 == len(arguments) or arguments[index + 1].startswith("-"):
                return Path(".env")
            return Path(arguments[index + 1])
        if argument.startswith("--env-file="):
            return Path(argument.partition("=")[2])
    return Path(".env")


def _with_no_prompt(arguments: list[str]) -> list[str]:
    """Ensure a missing PAT fails immediately rather than opening a hidden-input prompt."""
    return arguments if "--no-prompt" in arguments else [*arguments, "--no-prompt"]


def main(argv: list[str] | None = None) -> None:
    """Replace this process with the engine's survey after normalising its credential environment."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    env = resolve_env(_env_file(arguments))
    require(env, "TABLEAU_PAT_SECRET")
    command = [sys.executable, str(engine_scripts_dir() / "estate_survey.py"), *_with_no_prompt(arguments)]
    os.execvpe(sys.executable, command, engine_child_env(env))


if __name__ == "__main__":
    main()

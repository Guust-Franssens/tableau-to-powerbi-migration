"""Run the canonical engine's estate survey with this repository's Tableau credentials.

usage: python scripts/run_engine_survey.py [estate_survey.py arguments ...]

Every argument is passed straight through to the engine's ``estate_survey.py``, which stays the one
source of truth for its own command line -- this wrapper implements none of its flags, so a new
engine flag works here without a change. It reads the engine's ``--env-file`` value (default:
``.env``) to resolve credentials, accepts either historical PAT secret spelling, exports both names
to the engine, and appends ``--no-prompt`` so a missing PAT fails loudly instead of blocking on a
hidden-input prompt.

``-h``/``--help`` prints this text, needs no credential and no engine install. The engine's own flags
come from ``estate_survey.py --help``; ``python scripts/engine_source.py`` prints where it lives.

The engine runs as a CHILD process, not via ``os.exec*``: on Windows that detaches and reports
success, which destroys the engine's exit code (``estate_survey.py`` returns 1 on unresolved
dependencies or a degraded survey) and returns the prompt before the survey has written its JSON.
"""

from __future__ import annotations

import subprocess
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


def _wants_help(arguments: list[str]) -> bool:
    """Whether the operator asked for help - which must never demand a credential."""
    return any(argument in ("-h", "--help") for argument in arguments)


def main(argv: list[str] | None = None) -> None:
    """Run the engine's survey as a child process, after normalising its credential environment."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if _wants_help(arguments):
        print(__doc__)
        return
    env = resolve_env(_env_file(arguments))
    require(env, "TABLEAU_PAT_SECRET")
    command = [sys.executable, str(engine_scripts_dir() / "estate_survey.py"), *_with_no_prompt(arguments)]
    sys.exit(subprocess.run(command, env=engine_child_env(env), check=False).returncode)


if __name__ == "__main__":
    main()

"""Bash-command predicate for the training Discord gateway.

The training bot is allowed to invoke `python ~/.count/tools/kgraph.py
<safe-subcommand> ...` and nothing else. This module exposes
is_safe_kgraph_invocation(cmd) which returns True iff cmd matches that shape
with no shell metacharacters that could smuggle a second command.
"""
import shlex
from pathlib import Path

KGRAPH_SAFE_SUBCOMMANDS = frozenset({
    "stats", "search", "similar", "node",
    "facts", "path", "random", "neighbors",
})

# Cypher is denied: raw Cypher can destructively delete the graph.
# The structured subcommands above cover the same surface safely.

SHELL_METACHARS = ("|", "&&", "||", ";", "`", "$(", ">", "<")

PYTHON_INTERPRETERS = frozenset({
    "python", "python3", "py",
    "python.exe", "python3.exe", "py.exe",
})


def is_safe_kgraph_invocation(cmd: str) -> bool:
    """Return True iff cmd is a safe `python <kgraph.py> <safe-subcommand> ...`."""
    if not cmd or not cmd.strip():
        return False

    # 1. Reject any shell control characters outright (cheap pre-filter).
    for meta in SHELL_METACHARS:
        if meta in cmd:
            return False

    # 2. Tokenize via shlex. POSIX mode handles quoting correctly and raises
    #    on unbalanced quotes.
    try:
        argv = shlex.split(cmd, posix=True)
    except ValueError:
        return False

    # 3. Need at least: <interpreter> <script> <subcommand>
    if len(argv) < 3:
        return False

    # 4. argv[0] must be a Python interpreter (by basename).
    interp = Path(argv[0]).name
    if interp not in PYTHON_INTERPRETERS:
        return False

    # 5. argv[1] must be a path whose basename is kgraph.py.
    script = Path(argv[1])
    if script.name != "kgraph.py":
        return False

    # 6. argv[2] must be in the safe subcommand allowlist.
    if argv[2] not in KGRAPH_SAFE_SUBCOMMANDS:
        return False

    return True

"""Launcher that tees gateway output to both console and log file."""
import subprocess, sys, os
from pathlib import Path

log = Path.home() / ".count" / "logs" / "gateway.log"
log.parent.mkdir(parents=True, exist_ok=True)

with open(log, "w", buffering=1) as f:
    proc = subprocess.Popen(
        [sys.executable, "-u", "count_agent.py", "telegram"],
        cwd=str(Path(__file__).parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    for line in proc.stdout:
        text = line.decode("utf-8", errors="replace")
        f.write(text)
        f.flush()
    proc.wait()

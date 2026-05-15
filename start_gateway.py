"""Launcher for the Count's always-on services.

Spawns the sampling proxy first (so ANTHROPIC_BASE_URL is live before anything
calls the Anthropic API), waits for its port to open, then spawns the telegram
gateway. Each service's output is teed to its own log under ~/.count/logs/.

If either service dies, the other gets terminated and we exit. Ctrl-C kills
both cleanly.
"""
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).parent
LOGS = Path.home() / ".count" / "logs"
LOGS.mkdir(parents=True, exist_ok=True)

PROXY_HOST = os.environ.get("COUNT_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("COUNT_PROXY_PORT", "8787"))
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"


def wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect((host, port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def tee(proc: subprocess.Popen, log_path: Path, label: str) -> threading.Thread:
    """Pipe a subprocess's stdout to both console and a log file."""
    def run():
        with open(log_path, "w", buffering=1, encoding="utf-8", errors="replace") as f:
            for line in proc.stdout:
                text = line.decode("utf-8", errors="replace")
                f.write(text)
                f.flush()
                sys.stdout.write(f"[{label}] {text}")
                sys.stdout.flush()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def spawn(args: list[str], env: dict | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        args,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=env,
    )


def terminate(proc: subprocess.Popen):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def main():
    proxy = spawn([sys.executable, "-u", "count_proxy.py"])
    tee(proxy, LOGS / "proxy.log", "proxy")

    if not wait_for_port(PROXY_HOST, PROXY_PORT, timeout=10.0):
        sys.stderr.write(f"[start_gateway] proxy failed to open {PROXY_URL} within 10s\n")
        terminate(proxy)
        sys.exit(1)

    sys.stdout.write(f"[start_gateway] proxy listening on {PROXY_URL}\n")

    gw_env = os.environ.copy()
    gw_env["ANTHROPIC_BASE_URL"] = PROXY_URL
    gateway = spawn([sys.executable, "-u", "count_agent.py", "telegram"], env=gw_env)
    tee(gateway, LOGS / "gateway.log", "gateway")

    def shutdown(*_):
        sys.stdout.write("[start_gateway] shutting down\n")
        terminate(gateway)
        terminate(proxy)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Wait on whichever process exits first, then take the other one down with it.
    while True:
        if gateway.poll() is not None:
            sys.stdout.write(f"[start_gateway] gateway exited ({gateway.returncode}); stopping proxy\n")
            terminate(proxy)
            sys.exit(gateway.returncode or 0)
        if proxy.poll() is not None:
            sys.stdout.write(f"[start_gateway] proxy exited ({proxy.returncode}); stopping gateway\n")
            terminate(gateway)
            sys.exit(proxy.returncode or 1)
        time.sleep(0.5)


if __name__ == "__main__":
    main()

"""Probe Honcho: show what recall surfaces for given queries."""
import asyncio, sys
from pathlib import Path

# Load .env the same way count_agent.py does
env = Path.home() / ".count" / ".env"
if env.exists():
    import os
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from honcho_memory import HonchoMemory


async def main():
    queries = sys.argv[1:] or [
        "Ok lets test out the new shortform video production pipeline. Lets produce a whole video, start to finish, that deals with the first essay in the first movement folder",
        "produce a shortform video",
        "chain subagents to separate concerns",
        "video production workflow",
    ]
    m = HonchoMemory()
    ok = await m.init()
    if not ok:
        print("Honcho init failed")
        return
    for q in queries:
        print("=" * 70)
        print(f"QUERY: {q[:100]}{'...' if len(q) > 100 else ''}")
        print("-" * 70)
        ctx = await m.recall(q)
        print(ctx or "(empty)")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())

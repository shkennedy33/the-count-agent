#!/usr/bin/env python3
"""
The Count — Sampling Proxy

Local HTTP proxy that sits between Claude Code (which the Agent SDK spawns)
and api.anthropic.com, injecting sampling hyperparameters into
POST /v1/messages bodies. The SDK doesn't expose temperature / top_p / top_k,
so we patch them in at the wire.

Self-tuning: the Count reads/writes ~/.count/sampling.json. The proxy reloads
that file on every request, so he can adjust his own temperature via his
normal file tools and the next API call picks it up. No restart needed.

Usage:
    python count_proxy.py            # listen on 127.0.0.1:8787
    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 python count_agent.py chat ...

Env overrides:
    COUNT_PROXY_HOST        (default 127.0.0.1)
    COUNT_PROXY_PORT        (default 8787)
    COUNT_PROXY_UPSTREAM    (default https://api.anthropic.com)
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web


SAMPLING_FILE = Path.home() / ".count" / "sampling.json"
UPSTREAM = os.environ.get("COUNT_PROXY_UPSTREAM", "https://api.anthropic.com").rstrip("/")
HOST = os.environ.get("COUNT_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("COUNT_PROXY_PORT", "8787"))

# Hop-by-hop headers (RFC 7230 §6.1) must not be forwarded verbatim.
HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade",
}

SAMPLING_KEYS = ("temperature", "top_p", "top_k")

# ---- Request capture log ---------------------------------------------------
# Set COUNT_PROXY_LOG=0 to disable. Default on. One JSONL line per request.
# Purpose: capture the wire-level shape of caller traffic (Agent SDK now,
# claude -p / Claude Code later) so we can diff what identifies one from the
# other — header? body field? endpoint? auth shape? The proxy already sees
# the whole request; this just persists it.
LOG_FILE = Path(
    os.environ.get(
        "COUNT_PROXY_LOG_FILE",
        str(Path.home() / ".count" / "logs" / "proxy_calls.jsonl"),
    )
)
LOG_ENABLED = os.environ.get("COUNT_PROXY_LOG", "1") != "0"

# Headers whose values are secrets — redact to type+shape, keep prefix/suffix
# so we can still distinguish "Bearer ..." from "sk-ant-..." token families.
SECRET_HEADERS = {"authorization", "x-api-key", "proxy-authorization"}


def _redact_secret(value: str) -> str:
    """Reveal token family + length + first6/last4 only. Never the middle."""
    if not value:
        return ""
    n = len(value)
    head = value[:6]
    tail = value[-4:] if n > 10 else ""
    return f"{head}…{tail} (len={n})"


def _redact_headers(items) -> dict:
    out = {}
    for k, v in items:
        if k.lower() in SECRET_HEADERS:
            out[k] = _redact_secret(v)
        else:
            out[k] = v
    return out


def _parse_body(body_bytes: bytes):
    """Return parsed JSON if possible, else a {raw_len, raw_preview} summary."""
    if not body_bytes:
        return None
    try:
        return json.loads(body_bytes)
    except (ValueError, UnicodeDecodeError):
        return {
            "raw_len": len(body_bytes),
            "raw_preview_hex": body_bytes[:64].hex(),
        }


def log_request(request: web.Request, body_bytes: bytes) -> None:
    """Append one JSONL event describing this request. Never raises."""
    if not LOG_ENABLED:
        return
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "method": request.method,
            "path": request.path,
            "query": dict(request.rel_url.query),
            "headers": _redact_headers(request.headers.items()),
            "body": _parse_body(body_bytes),
            "body_bytes": len(body_bytes),
            "remote": request.remote,
        }
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str))
            f.write("\n")
    except Exception as e:  # noqa: BLE001 — logging must never break proxying
        print(f"[count-proxy] log error: {e}", file=sys.stderr)


def load_sampling() -> dict:
    """Read sampling.json. Missing / malformed / null values → no override."""
    try:
        cfg = json.loads(SAMPLING_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(cfg, dict):
        return {}
    return {k: cfg[k] for k in SAMPLING_KEYS if cfg.get(k) is not None}


def inject_sampling(body_bytes: bytes) -> bytes:
    """Merge sampling overrides into a /v1/messages JSON body."""
    try:
        body = json.loads(body_bytes)
    except (ValueError, UnicodeDecodeError):
        return body_bytes
    if not isinstance(body, dict):
        return body_bytes
    for k, v in load_sampling().items():
        body[k] = v
    return json.dumps(body).encode("utf-8")


async def proxy(request: web.Request) -> web.StreamResponse:
    upstream_url = f"{UPSTREAM}{request.rel_url.path_qs}"
    body = await request.read()

    # Capture the request as the caller sent it — before any mutation we do.
    # Used for the Agent SDK ↔ Claude Code discriminator hunt (see header comment).
    log_request(request, body)

    # Only /v1/messages POSTs get sampling injected. /v1/messages/count_tokens
    # and everything else passes through untouched.
    if request.method == "POST" and request.path == "/v1/messages":
        body = inject_sampling(body)

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}

    session: aiohttp.ClientSession = request.app["session"]
    try:
        upstream_resp = await session.request(
            method=request.method,
            url=upstream_url,
            data=body if body else None,
            headers=headers,
            allow_redirects=False,
        )
    except aiohttp.ClientError as e:
        return web.json_response({"error": f"proxy upstream error: {e}"}, status=502)

    resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP}
    response = web.StreamResponse(status=upstream_resp.status, headers=resp_headers)
    await response.prepare(request)
    try:
        async for chunk in upstream_resp.content.iter_any():
            await response.write(chunk)
    finally:
        upstream_resp.release()
    await response.write_eof()
    return response


async def on_startup(app: web.Application):
    # SSE streams are long-lived; disable the default read timeout.
    # auto_decompress=False keeps response bytes in their on-wire form — without
    # this, aiohttp silently gunzips the body but we'd still forward the
    # Content-Encoding header, and the caller would ZlibError on plain JSON.
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
    app["session"] = aiohttp.ClientSession(timeout=timeout, auto_decompress=False)


async def on_cleanup(app: web.Application):
    await app["session"].close()


def make_app() -> web.Application:
    app = web.Application(client_max_size=100 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", proxy)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    SAMPLING_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SAMPLING_FILE.exists():
        SAMPLING_FILE.write_text(
            json.dumps({"temperature": 1.0, "top_p": None, "top_k": None}, indent=2),
            encoding="utf-8",
        )
    print(f"[count-proxy] upstream  = {UPSTREAM}", file=sys.stderr)
    print(f"[count-proxy] listening = http://{HOST}:{PORT}", file=sys.stderr)
    print(f"[count-proxy] sampling  = {SAMPLING_FILE}", file=sys.stderr)
    print(f"[count-proxy] ->  export ANTHROPIC_BASE_URL=http://{HOST}:{PORT}", file=sys.stderr)
    web.run_app(make_app(), host=HOST, port=PORT, print=lambda *a, **k: None)


if __name__ == "__main__":
    main()

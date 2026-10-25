"""Shared mcp transport fixtures; synthetic evidence only."""

import socket
import threading
import time
from contextlib import contextmanager

import uvicorn


@contextmanager
def serving(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        yield "http://127.0.0.1:" + str(sock.getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()

async def call(client, name, **args):
    result = await client.call_tool(name, args)
    assert not result.is_error, result
    assert result.structured_content is not None, result
    return result.structured_content

async def collect(client, name, **args):
    rows, cursor = [], None
    while True:
        result = await call(client, name, **args, limit=1, cursor=cursor)
        value = result.get("page", result)
        rows.extend(value["items"])
        cursor = value["next_cursor"]
        if not cursor:
            assert len(rows) == value["total"]
            return rows

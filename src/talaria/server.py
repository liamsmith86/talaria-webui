"""Uvicorn with an early drain for Talaria's long-lived browser streams."""

import logging
import socket

import uvicorn
from uvicorn.main import STARTUP_FAILURE
from uvicorn.supervisors import ChangeReload


class Server(uvicorn.Server):
    async def shutdown(self, sockets=None):
        # ASGI lifespan shutdown comes after Uvicorn drains HTTP requests.
        # Finish SSE responses first so they cannot hold that drain open.
        try:
            await self.lifespan.state["talaria_stop_streams"]()
        finally:
            await super().shutdown(sockets=sockets)


def named_tcp(sock):
    """The same socket, stating its protocol. asyncio sets TCP_NODELAY on accepted connections
    only if the listener says it is TCP; otherwise a response waits about 40 ms for a delayed
    ACK between its headers and its body. Uvicorn's own host and port bind does say so."""
    return socket.socket(fileno=sock.detach())


def run(app, **kwargs):
    config = uvicorn.Config(app, **kwargs)
    if config.workers > 1 and not config.should_reload:
        # The CLI serves an app instance; as with uvicorn.run, an inherited
        # WEB_CONCURRENCY must not silently change its single-worker contract.
        logging.getLogger("uvicorn.error").error(
            "Talaria runs with one worker. Set WEB_CONCURRENCY=1."
        )
        raise SystemExit(STARTUP_FAILURE)
    server = Server(config)
    try:
        if config.should_reload:
            with named_tcp(config.bind_socket()) as sock:
                ChangeReload(config, target=server.run, sockets=[sock]).run()
        else:
            server.run()
    except KeyboardInterrupt:
        # Uvicorn restores and replays SIGINT after completing shutdown.
        return
    if not server.started and not config.should_reload:
        raise SystemExit(STARTUP_FAILURE)

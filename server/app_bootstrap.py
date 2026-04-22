from __future__ import annotations

import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import Any

from cleanup_old_sessions import cleanup_expired_sessions


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def build_lifespan(*, state: Any, logger: logging.Logger):
    @asynccontextmanager
    async def lifespan(app):  # type: ignore[no-untyped-def]
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
        )
        ip = lan_ip()
        logger.info("LAN IP: %s  →  set iPhone Settings → Server IP = %s, Port = 8765", ip, ip)
        cleanup_days = int(os.environ.get("BALL_TRACKER_CLEANUP_DAYS", "30"))
        if cleanup_days > 0:
            sessions, files, bytes_removed = cleanup_expired_sessions(
                state.data_dir,
                days=cleanup_days,
                dry_run=False,
            )
            logger.info(
                "cleanup: removed %d sessions / %d files / %d bytes older than %d days from %s",
                sessions,
                files,
                bytes_removed,
                cleanup_days,
                state.data_dir,
            )
        yield

    return lifespan

"""PR audit-r3 BLOCK 1: WS server must fail loud on unknown mtype.

Silent drop would mask iOS↔server schema drift — if iOS adds a new
outbound message type without the server being updated in lockstep, we
want the WS handler to close noisily, not for subsequent frames /
cycle_ends to keep flowing while the new mtype's payload disappears
into the void.

See CLAUDE.md "WS-only checklist § 4".
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
from conftest import preassign_and_open_ws
from main import app


def test_unknown_mtype_closes_ws(monkeypatch):
    """Sending a `type` the server doesn't recognise must raise inside
    `ws_device`, which the Starlette WS wrapper translates into a server-
    side close. From the client's POV the WS terminates abnormally (the
    `with ...` context exits via WebSocketDisconnect rather than a clean
    orderly close).
    """
    client = TestClient(app)

    class _NoopHub:
        async def broadcast(self, event: str, data: dict) -> None:
            return None

        async def subscribe(self):
            if False:
                yield ""

    monkeypatch.setattr(main, "sse_hub", _NoopHub())

    with pytest.raises(Exception):  # WebSocketDisconnect or transport close
        with preassign_and_open_ws(client, "A") as ws_a:
            assert ws_a.receive_json()["type"] == "settings"
            # `frame_v999` is not a known mtype — handler must raise so
            # the WS closes instead of silently dropping the payload.
            ws_a.send_json({"type": "frame_v999", "sid": "s_test"})
            # The next read should fail because the server has closed
            # the connection. If the handler silently ignored the message
            # the receive would block until timeout — we'd then exit the
            # context manager cleanly with no exception, FAILING the test.
            ws_a.receive_json()

from __future__ import annotations

import pytest

from render_sync_page import _render_burst_params_body


def test_burst_params_body_none_uses_boot_defaults() -> None:
    html = _render_burst_params_body(None)

    assert 'value="0.3, 0.5, 0.7"' in html
    assert 'value="4.0"' in html


def test_burst_params_body_rejects_partial_sync_params() -> None:
    with pytest.raises(KeyError, match="search_window_s"):
        _render_burst_params_body({
            "emit_a_at_s": [0.3, 0.5, 0.7],
            "emit_b_at_s": [1.8, 2.0, 2.2],
            "record_duration_s": 4.0,
        })

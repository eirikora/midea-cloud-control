"""Tests for the private midea-local transparent-send adapter."""

from __future__ import annotations

import pytest
from midealocal.cloud import RAISE_FOR_ERROR_CODES, MideaCloud
from midealocal.exceptions import CLOUD_ERRORS

from custom_components.midea_cloud_control import protocol


def test_midea_local_3176_is_logged_but_not_raised() -> None:
    """Pin the midea-local 12.2.0 behavior that turns 3176 into None."""
    assert 3176 not in CLOUD_ERRORS
    assert 3176 not in RAISE_FOR_ERROR_CODES
    assert (
        MideaCloud._handle_error_code(  # type: ignore[arg-type]
            None,
            "https://mapp.appsmb.com/v1/appliance/transparent/send",
            3176,
            "The asyn reply does not exist",
            True,
        )
        is None
    )


@pytest.mark.asyncio
async def test_transparent_send_requests_structured_cloud_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeCloud:
        _access_token = "token"

        def _make_general_data(self) -> dict[str, object]:
            return {"sessionId": "session"}

        async def _api_request(self, **kwargs: object) -> dict[str, str]:
            captured.update(kwargs)
            return {"reply": "encrypted-reply"}

    monkeypatch.setattr(protocol, "derive_data_key", lambda *_args: "data-key")
    monkeypatch.setattr(protocol, "aes_encrypt", lambda *_args: "encrypted-order")
    monkeypatch.setattr(protocol, "aes_decrypt", lambda *_args: "decrypted-reply")
    monkeypatch.setattr(protocol, "unwrap_reply", lambda _reply: b"response")

    response = await protocol.transparent_send(FakeCloud(), 123, b"frame", "app-key")

    assert response == b"response"
    assert captured["endpoint"] == "/v1/appliance/transparent/send"
    assert captured["raise_for_error"] is True
    data = captured["data"]
    assert isinstance(data, dict)
    assert data["applianceId"] == "123"

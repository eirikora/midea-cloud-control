"""Tests for narrow midea-local warning suppression."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from custom_components.midea_cloud_control.const import DOMAIN
from custom_components.midea_cloud_control.log_filter import (
    AsyncReplyMissingFilter,
    install_async_reply_log_filter,
    remove_async_reply_log_filter,
)


def make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord("midealocal.cloud", logging.WARNING, "", 0, message, (), None)


def test_filter_only_suppresses_transparent_send_3176() -> None:
    log_filter = AsyncReplyMissingFilter()

    assert not log_filter.filter(
        make_record(
            "transparent/send rejected the request with code 3176 (cloud_error): "
            "The asyn reply does not exist"
        )
    )
    assert log_filter.filter(make_record("transparent/send code 3106 invalidSession"))
    assert log_filter.filter(make_record("other endpoint code 3176"))


def test_filter_lifecycle_is_reference_counted() -> None:
    hass = SimpleNamespace(data={})

    install_async_reply_log_filter(hass)  # type: ignore[arg-type]
    install_async_reply_log_filter(hass)  # type: ignore[arg-type]
    assert hass.data[DOMAIN]["log_filter_references"] == 2

    remove_async_reply_log_filter(hass)  # type: ignore[arg-type]
    assert hass.data[DOMAIN]["log_filter_references"] == 1
    remove_async_reply_log_filter(hass)  # type: ignore[arg-type]
    assert hass.data[DOMAIN]["log_filter_references"] == 0

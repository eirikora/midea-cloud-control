"""Narrow log filtering for expected NetHome Plus async-reply misses."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .const import DATA_LOG_FILTER, DATA_LOG_FILTER_REFS, DOMAIN

_MIDEA_CLOUD_LOGGER = logging.getLogger("midealocal.cloud")


class AsyncReplyMissingFilter(logging.Filter):
    """Suppress only transparent-send 3176 warnings handled by this integration."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage().casefold()
        return not (
            "transparent/send" in message
            and "code 3176" in message
            and "asyn reply does not exist" in message
        )


def install_async_reply_log_filter(hass: HomeAssistant) -> None:
    """Install one process-wide filter and increment its integration refcount."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    references = int(domain_data.get(DATA_LOG_FILTER_REFS, 0))
    if references == 0:
        log_filter = AsyncReplyMissingFilter()
        _MIDEA_CLOUD_LOGGER.addFilter(log_filter)
        domain_data[DATA_LOG_FILTER] = log_filter
    domain_data[DATA_LOG_FILTER_REFS] = references + 1


def remove_async_reply_log_filter(hass: HomeAssistant) -> None:
    """Remove the filter after the last config entry unloads."""
    domain_data = hass.data.get(DOMAIN, {})
    references = max(0, int(domain_data.get(DATA_LOG_FILTER_REFS, 0)) - 1)
    domain_data[DATA_LOG_FILTER_REFS] = references
    if references != 0:
        return
    if log_filter := domain_data.pop(DATA_LOG_FILTER, None):
        _MIDEA_CLOUD_LOGGER.removeFilter(log_filter)

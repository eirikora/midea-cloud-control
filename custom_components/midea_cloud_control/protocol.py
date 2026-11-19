"""Midea cloud 'transparent send' transport.

Midea retired the /v1/iot/secure/getToken endpoint, which is what every
existing Home Assistant integration relies on to obtain the per-device
token/key needed for LOCAL control. Devices onboarded after the shutdown
therefore cannot be controlled locally at all.

The legacy /v1/appliance/transparent/send endpoint is still live. It relays a
raw Midea protocol frame to the appliance via the cloud, and requires no
token or key -- only a normal app login. This module implements that path.

Frames are wrapped in the 40-byte 0x5A5A packet header, serialised as
comma-separated *signed* integers, AES-ECB encrypted with a key derived from
the login access token, and sent hex-encoded in the `order` parameter.
"""

from __future__ import annotations

import binascii
import hashlib
import logging
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

_LOGGER = logging.getLogger(__name__)

# 40-byte 0x5A5A packet header. Byte 4 is overwritten with the total length.
_PACKET_HEADER = [
    90, 90, 1, 0, 89, 0, 32, 0, 1, 0, 0, 0, 39, 36, 17, 9, 13, 10, 18, 20,
    218, 73, 0, 0, 0, 16, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
]
_TRAILER_LEN = 16
_ENDPOINT = "/v1/appliance/transparent/send"


class TransparentSendDecodeError(Exception):
    """Raised when an encrypted or relayed cloud response cannot be decoded."""


def _pad(text: str) -> str:
    """Pad to a 16-char boundary, always appending a full block if aligned."""
    blocks = [text[i:i + 16] for i in range(0, len(text), 16)] or [""]
    if len(blocks[-1]) < 16:
        pad = 16 - len(blocks[-1])
        blocks[-1] += chr(pad) * pad
    else:
        blocks.append(chr(16) * 16)
    return "".join(blocks)


def aes_encrypt(text: str, key: str) -> str:
    cipher = AES.new(key.encode(), AES.MODE_ECB)
    return binascii.hexlify(cipher.encrypt(_pad(text).encode())).decode()


def aes_decrypt(payload: str, key: str) -> str:
    """Decrypt and strictly validate PKCS#7 padding."""
    try:
        cipher = AES.new(key.encode(), AES.MODE_ECB)
        raw = cipher.decrypt(binascii.unhexlify(payload))
        return unpad(raw, AES.block_size).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError) as err:
        raise TransparentSendDecodeError(
            "Invalid encrypted cloud response"
        ) from err


def derive_data_key(access_token: str, app_key: str) -> str:
    """Derive the payload key from the login access token."""
    return aes_decrypt(access_token, hashlib.md5(app_key.encode()).hexdigest()[:16])


def wrap_frame(frame: bytes) -> str:
    """Wrap a raw 0xAA frame and serialise it as signed comma-separated ints."""
    packet = _PACKET_HEADER + list(frame) + [0] * _TRAILER_LEN
    packet[4] = len(packet)
    return ",".join(str(b - 256 if b >= 128 else b) for b in packet)


def unwrap_reply(reply: str) -> bytes:
    """Strip the 0x5A5A header from a decrypted reply, returning the 0xAA frame.

    The relayed packet is zero-padded past the end of the frame, so the frame is
    truncated to its declared length (byte 1, which excludes the trailing
    checksum) -- otherwise the padding is read as payload and fails CRC.
    """
    values = [int(v) for v in reply.split(",")]
    body = bytes(v + 256 if v < 0 else v for v in values[len(_PACKET_HEADER):])
    if len(body) >= 2 and body[0] == 0xAA:
        body = body[:body[1] + 1]
    return body


async def transparent_send(cloud: Any, appliance_id: int, frame: bytes,
                           app_key: str) -> bytes | None:
    """Relay `frame` to `appliance_id` through the cloud and return its reply."""
    try:
        data_key = derive_data_key(cloud._access_token, app_key)
        encrypted_order = aes_encrypt(wrap_frame(frame), data_key)
    except (AttributeError, TypeError, ValueError, TransparentSendDecodeError) as err:
        raise TransparentSendDecodeError(
            "Could not encode transparent-send request"
        ) from err

    data = cloud._make_general_data()
    data.update({
        "order": encrypted_order,
        "funId": "0000",
        "applianceId": str(appliance_id),
    })

    # midea-local only surfaces 3106/3144 as structured exceptions when this
    # flag is set. The shared client can then replace the expired cloud object.
    response = await cloud._api_request(
        endpoint=_ENDPOINT, data=data, raise_for_error=True
    )
    if not response or "reply" not in response:
        _LOGGER.debug("transparent_send: no reply for appliance %s", appliance_id)
        return None

    try:
        return unwrap_reply(aes_decrypt(response["reply"], data_key))
    except (TypeError, ValueError, TransparentSendDecodeError) as err:
        raise TransparentSendDecodeError(
            "Could not decode transparent-send reply"
        ) from err

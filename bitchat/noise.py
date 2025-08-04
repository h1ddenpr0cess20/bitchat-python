"""Noise identity announcement helpers"""
from __future__ import annotations
from typing import Optional

from .protocol import debug_println


def parse_noise_identity_announcement_binary(data: bytes) -> Optional[dict]:
    """Parse binary format noise identity announcement"""
    try:
        offset = 0
        debug_println(f"[NOISE] Parsing binary announcement, total length: {len(data)}")
        debug_println(f"[NOISE] Raw data (hex): {data.hex()}")
        if offset >= len(data):
            debug_println("[NOISE] Error: Not enough data for flags")
            return None
        flags = data[offset]
        offset += 1
        has_previous_peer_id = (flags & 0x01) != 0
        if offset + 8 > len(data):
            debug_println("[NOISE] Error: Not enough data for peerID")
            return None
        peer_id = data[offset:offset+8].hex()
        offset += 8
        if offset >= len(data):
            debug_println("[NOISE] Error: Not enough data for publicKey length")
            return None
        pub_key_len = data[offset]
        offset += 1
        if offset + pub_key_len > len(data):
            debug_println("[NOISE] Error: Not enough data for publicKey")
            return None
        public_key = data[offset:offset+pub_key_len]
        offset += pub_key_len
        if offset >= len(data):
            debug_println("[NOISE] Error: Not enough data for signingPublicKey length")
            return None
        signing_key_len = data[offset]
        offset += 1
        if offset + signing_key_len > len(data):
            debug_println("[NOISE] Error: Not enough data for signingPublicKey")
            return None
        signing_public_key = data[offset:offset+signing_key_len]
        offset += signing_key_len
        if offset >= len(data):
            debug_println("[NOISE] Error: Not enough data for nickname length")
            return None
        nickname_len = data[offset]
        offset += 1
        nickname = ""
        if nickname_len > 0:
            if offset + nickname_len > len(data):
                debug_println("[NOISE] Error: Not enough data for nickname")
                return None
            nickname = data[offset:offset+nickname_len].decode('utf-8')
            offset += nickname_len
        if offset + 8 > len(data):
            debug_println("[NOISE] Error: Not enough data for timestamp")
            return None
        timestamp_ms = int.from_bytes(data[offset:offset+8], byteorder='big')
        offset += 8
        previous_peer_id = None
        if has_previous_peer_id:
            if offset + 8 > len(data):
                debug_println("[NOISE] Error: Not enough data for previousPeerID")
                return None
            previous_peer_id = data[offset:offset+8].hex()
            offset += 8
        if offset >= len(data):
            debug_println("[NOISE] Error: Not enough data for signature length")
            return None
        sig_len = data[offset]
        offset += 1
        if offset + sig_len > len(data):
            debug_println("[NOISE] Error: Not enough data for signature")
            return None
        signature = data[offset:offset+sig_len]
        return {
            'peerID': peer_id,
            'publicKey': public_key.hex(),
            'signingPublicKey': signing_public_key.hex(),
            'nickname': nickname,
            'timestamp': timestamp_ms / 1000.0,
            'signature': signature.hex(),
            'previousPeerID': previous_peer_id,
            'truncated': False,
        }
    except Exception as e:
        debug_println(f"[NOISE] Error parsing binary announcement: {e}")
        return None


def encode_noise_identity_announcement_binary(
    peer_id: str,
    public_key: bytes,
    signing_public_key: bytes,
    nickname: str,
    timestamp: int,
    signature: bytes,
    previous_peer_id: str | None = None,
) -> bytes:
    data = bytearray()
    flags = 0
    if previous_peer_id:
        flags |= 0x01
    data.append(flags)
    data.extend(bytes.fromhex(peer_id.ljust(16, '0')[:16]))
    data.append(len(public_key))
    data.extend(public_key)
    data.append(len(signing_public_key))
    data.extend(signing_public_key)
    nickname_bytes = nickname.encode('utf-8')
    data.append(len(nickname_bytes))
    data.extend(nickname_bytes)
    timestamp_ms = int(timestamp * 1000)
    for i in range(8):
        data.append((timestamp_ms >> ((7 - i) * 8)) & 0xFF)
    if previous_peer_id:
        data.extend(bytes.fromhex(previous_peer_id.ljust(16, '0')[:16]))
    data.append(len(signature))
    data.extend(signature)
    return bytes(data)


__all__ = [
    'parse_noise_identity_announcement_binary',
    'encode_noise_identity_announcement_binary',
]

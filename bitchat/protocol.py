import time
import uuid
import struct
from typing import Optional, Tuple, Dict, List

from .constants import (
    FLAG_HAS_RECIPIENT,
    FLAG_HAS_SIGNATURE,
    FLAG_IS_COMPRESSED,
    MSG_FLAG_HAS_CHANNEL,
    MSG_FLAG_IS_ENCRYPTED,
    MSG_FLAG_HAS_ORIGINAL_SENDER,
    MSG_FLAG_HAS_RECIPIENT_NICKNAME,
    MSG_FLAG_HAS_SENDER_PEER_ID,
    MSG_FLAG_HAS_MENTIONS,
    MessageType,
    BROADCAST_RECIPIENT,
)
from .models import BitchatPacket, BitchatMessage
from .utils import debug_full_println
from .compression import decompress
from .encryption import EncryptionService


class DeliveryTracker:
    def __init__(self):
        self.pending_messages: Dict[str, Tuple[str, float, bool]] = {}
        self.sent_acks: set[str] = set()

    def track_message(self, message_id: str, content: str, is_private: bool):
        self.pending_messages[message_id] = (content, time.time(), is_private)

    def mark_delivered(self, message_id: str) -> bool:
        return self.pending_messages.pop(message_id, None) is not None

    def should_send_ack(self, ack_id: str) -> bool:
        if ack_id in self.sent_acks:
            return False
        self.sent_acks.add(ack_id)
        return True


class FragmentCollector:
    def __init__(self):
        self.fragments: Dict[str, Dict[int, bytes]] = {}
        self.metadata: Dict[str, Tuple[int, int, str]] = {}

    def add_fragment(
        self,
        fragment_id: bytes,
        index: int,
        total: int,
        original_type: int,
        data: bytes,
        sender_id: str,
    ) -> Optional[Tuple[bytes, str]]:
        fragment_id_hex = fragment_id.hex()

        debug_full_println(
            f"[COLLECTOR] Adding fragment {index + 1}/{total} for ID {fragment_id_hex[:8]}"
        )

        if fragment_id_hex not in self.fragments:
            debug_full_println(
                f"[COLLECTOR] Creating new fragment collection for ID {fragment_id_hex[:8]}"
            )
            self.fragments[fragment_id_hex] = {}
            self.metadata[fragment_id_hex] = (total, original_type, sender_id)

        fragment_map = self.fragments[fragment_id_hex]
        fragment_map[index] = data
        debug_full_println(
            f"[COLLECTOR] Fragment {index + 1} stored. Have {len(fragment_map)}/{total} fragments"
        )

        if len(fragment_map) == total:
            debug_full_println("[COLLECTOR] \u2713 All fragments received! Reassembling...")

            complete_data = bytearray()
            for i in range(total):
                if i in fragment_map:
                    debug_full_println(
                        f"[COLLECTOR] Appending fragment {i + 1} ({len(fragment_map[i])} bytes)"
                    )
                    complete_data.extend(fragment_map[i])
                else:
                    debug_full_println(f"[COLLECTOR] \u2717 Missing fragment {i + 1}")
                    return None

            debug_full_println(
                f"[COLLECTOR] \u2713 Reassembly complete: {len(complete_data)} bytes total"
            )

            sender = self.metadata.get(fragment_id_hex, (0, 0, "Unknown"))[2]

            del self.fragments[fragment_id_hex]
            del self.metadata[fragment_id_hex]

            return (bytes(complete_data), sender)

        return None


def parse_bitchat_packet(data: bytes) -> BitchatPacket:
    HEADER_SIZE = 13
    SENDER_ID_SIZE = 8
    RECIPIENT_ID_SIZE = 8

    if len(data) < HEADER_SIZE + SENDER_ID_SIZE:
        raise ValueError("Packet too small")

    offset = 0

    version = data[offset]
    offset += 1
    if version != 1:
        raise ValueError("Unsupported version")

    msg_type = MessageType(data[offset])
    offset += 1

    ttl = data[offset]
    offset += 1

    offset += 8  # timestamp

    flags = data[offset]
    offset += 1
    has_recipient = (flags & FLAG_HAS_RECIPIENT) != 0
    is_compressed = (flags & FLAG_IS_COMPRESSED) != 0

    payload_len = struct.unpack('>H', data[offset:offset+2])[0]
    offset += 2

    sender_id = data[offset:offset+SENDER_ID_SIZE]
    sender_id_str = sender_id.decode('ascii', errors='ignore').rstrip('\x00')
    offset += SENDER_ID_SIZE

    recipient_id = None
    recipient_id_str = None
    if has_recipient:
        recipient_id = data[offset:offset+RECIPIENT_ID_SIZE]
        recipient_id_str = recipient_id.decode('ascii', errors='ignore').rstrip('\x00')
        offset += RECIPIENT_ID_SIZE

    payload = data[offset:offset+payload_len]
    if is_compressed:
        payload = decompress(payload)

    if isinstance(payload, bytearray):
        payload = bytes(payload)

    return BitchatPacket(
        msg_type,
        sender_id,
        sender_id_str,
        recipient_id,
        recipient_id_str,
        payload,
        ttl,
    )


def parse_bitchat_message_payload(data: bytes) -> BitchatMessage:
    offset = 0
    if len(data) < 1:
        raise ValueError("Payload too short")

    flags = data[offset]
    offset += 1

    has_channel = (flags & MSG_FLAG_HAS_CHANNEL) != 0
    is_encrypted = (flags & MSG_FLAG_IS_ENCRYPTED) != 0
    has_original_sender = (flags & MSG_FLAG_HAS_ORIGINAL_SENDER) != 0
    has_recipient_nickname = (flags & MSG_FLAG_HAS_RECIPIENT_NICKNAME) != 0
    has_sender_peer_id = (flags & MSG_FLAG_HAS_SENDER_PEER_ID) != 0
    has_mentions = (flags & MSG_FLAG_HAS_MENTIONS) != 0

    offset += 8  # timestamp

    id_len = data[offset]
    offset += 1
    id_str = data[offset:offset+id_len].decode('utf-8')
    offset += id_len

    sender_len = data[offset]
    offset += 1 + sender_len

    content_len = struct.unpack('>H', data[offset:offset+2])[0]
    offset += 2

    if is_encrypted:
        content = ""
        encrypted_content = data[offset:offset+content_len]
    else:
        content = data[offset:offset+content_len].decode('utf-8', errors='ignore')
        encrypted_content = None
    offset += content_len

    if has_original_sender:
        orig_sender_len = data[offset]
        offset += 1 + orig_sender_len

    if has_recipient_nickname:
        recipient_len = data[offset]
        offset += 1 + recipient_len

    if has_sender_peer_id:
        peer_id_len = data[offset]
        offset += 1 + peer_id_len

    if has_mentions:
        mentions_count = struct.unpack('>H', data[offset:offset+2])[0]
        offset += 2
        for _ in range(mentions_count):
            mention_len = data[offset]
            offset += 1 + mention_len

    channel = None
    if has_channel:
        channel_len = data[offset]
        offset += 1
        channel = data[offset:offset+channel_len].decode('utf-8')

    return BitchatMessage(id_str, content, channel, is_encrypted, encrypted_content)


def create_bitchat_packet(sender_id: str, msg_type: MessageType, payload: bytes) -> bytes:
    return create_bitchat_packet_with_recipient(sender_id, None, msg_type, payload, None)


def create_bitchat_packet_with_signature(
    sender_id: str,
    msg_type: MessageType,
    payload: bytes,
    signature: Optional[bytes],
) -> bytes:
    return create_bitchat_packet_with_recipient(sender_id, None, msg_type, payload, signature)


def create_bitchat_packet_with_recipient_and_signature(
    sender_id: str,
    recipient_id: str,
    msg_type: MessageType,
    payload: bytes,
    signature: Optional[bytes],
) -> bytes:
    return create_bitchat_packet_with_recipient(sender_id, recipient_id, msg_type, payload, signature)


def create_bitchat_packet_with_recipient(
    sender_id: str,
    recipient_id: Optional[str],
    msg_type: MessageType,
    payload: bytes,
    signature: Optional[bytes],
) -> bytes:
    packet = bytearray()

    packet.append(1)  # version
    packet.append(msg_type.value)
    packet.append(7)  # TTL

    timestamp_ms = int(time.time() * 1000)
    packet.extend(struct.pack('>Q', timestamp_ms))

    flags = 0
    has_recipient = msg_type not in [MessageType.FRAGMENT_START, MessageType.FRAGMENT_CONTINUE, MessageType.FRAGMENT_END]
    if has_recipient:
        flags |= FLAG_HAS_RECIPIENT
    if signature:
        flags |= FLAG_HAS_SIGNATURE
    packet.append(flags)

    packet.extend(struct.pack('>H', len(payload)))

    sender_bytes = sender_id.encode('ascii')[:8]
    sender_bytes += b'\x00' * (8 - len(sender_bytes))
    packet.extend(sender_bytes)

    if has_recipient:
        if recipient_id:
            recipient_bytes = recipient_id.encode('ascii')[:8]
            recipient_bytes += b'\x00' * (8 - len(recipient_bytes))
            packet.extend(recipient_bytes)
        else:
            packet.extend(BROADCAST_RECIPIENT)

    packet.extend(payload)

    if signature:
        packet.extend(signature)

    return bytes(packet)


def create_bitchat_message_payload_full(
    sender: str,
    content: str,
    channel: Optional[str],
    is_private: bool,
    sender_peer_id: str,
) -> Tuple[bytes, str]:
    data = bytearray()

    flags = MSG_FLAG_HAS_SENDER_PEER_ID
    if channel:
        flags |= MSG_FLAG_HAS_CHANNEL
    if is_private:
        flags |= MSG_FLAG_IS_PRIVATE
    data.append(flags)

    timestamp_ms = int(time.time() * 1000)
    data.extend(struct.pack('>Q', timestamp_ms))

    message_id = str(uuid.uuid4())
    data.append(len(message_id))
    data.extend(message_id.encode())

    data.append(len(sender))
    data.extend(sender.encode())

    content_bytes = content.encode()
    data.extend(struct.pack('>H', len(content_bytes)))
    data.extend(content_bytes)

    data.append(len(sender_peer_id))
    data.extend(sender_peer_id.encode())

    if channel:
        data.append(len(channel))
        data.extend(channel.encode())

    return bytes(data), message_id


def create_encrypted_channel_message_payload(
    sender: str,
    content: str,
    channel: str,
    channel_key: bytes,
    encryption_service: EncryptionService,
    sender_peer_id: str,
) -> Tuple[bytes, str]:
    data = bytearray()

    flags = MSG_FLAG_HAS_CHANNEL | MSG_FLAG_IS_ENCRYPTED | MSG_FLAG_HAS_SENDER_PEER_ID
    data.append(flags)

    timestamp_ms = int(time.time() * 1000)
    data.extend(struct.pack('>Q', timestamp_ms))

    message_id = str(uuid.uuid4())
    data.append(len(message_id))
    data.extend(message_id.encode())

    data.append(len(sender))
    data.extend(sender.encode())

    try:
        encrypted_content = encryption_service.encrypt_with_key(content.encode(), channel_key)
    except Exception:
        return create_bitchat_message_payload_full(sender, content, channel, False, sender_peer_id)

    data.extend(struct.pack('>H', len(encrypted_content)))
    data.extend(encrypted_content)

    data.append(len(sender_peer_id))
    data.extend(sender_peer_id.encode())

    data.append(len(channel))
    data.extend(channel.encode())

    return bytes(data), message_id


def unpad_message(data: bytes) -> bytes:
    if not data:
        return data

    padding_length = data[-1]
    if padding_length == 0 or padding_length > len(data) or padding_length > 255:
        return data
    return data[:-padding_length]


def should_fragment(packet: bytes) -> bool:
    return len(packet) > 500


def should_send_ack(
    is_private: bool,
    channel: Optional[str],
    mentions: Optional[List[str]],
    my_nickname: str,
    active_peer_count: int,
) -> bool:
    if is_private:
        return True
    elif channel:
        if active_peer_count < 10:
            return True
        elif mentions and my_nickname in mentions:
            return True
    return False

__all__ = [
    'DeliveryTracker',
    'FragmentCollector',
    'parse_bitchat_packet',
    'parse_bitchat_message_payload',
    'create_bitchat_packet',
    'create_bitchat_packet_with_signature',
    'create_bitchat_packet_with_recipient_and_signature',
    'create_bitchat_packet_with_recipient',
    'create_bitchat_message_payload_full',
    'create_encrypted_channel_message_payload',
    'unpad_message',
    'should_fragment',
    'should_send_ack',
]

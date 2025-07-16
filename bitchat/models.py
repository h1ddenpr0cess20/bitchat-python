from dataclasses import dataclass
from typing import Optional

from .constants import MessageType

@dataclass
class Peer:
    nickname: Optional[str] = None

@dataclass
class BitchatPacket:
    msg_type: MessageType
    sender_id: bytes
    sender_id_str: str
    recipient_id: Optional[bytes]
    recipient_id_str: Optional[str]
    payload: bytes
    ttl: int

@dataclass
class BitchatMessage:
    id: str
    content: str
    channel: Optional[str]
    is_encrypted: bool
    encrypted_content: Optional[bytes]

@dataclass
class DeliveryAck:
    original_message_id: str
    ack_id: str
    recipient_id: str
    recipient_nickname: str
    timestamp: int
    hop_count: int

__all__ = [
    'Peer', 'BitchatPacket', 'BitchatMessage', 'DeliveryAck'
]

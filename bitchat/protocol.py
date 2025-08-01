import os
import time
import struct
import uuid
import hashlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Dict, List, Tuple, Set

from .compression import compress_if_beneficial, decompress

VERSION = "v1.1.0"
BITCHAT_SERVICE_UUID = "f47b5e2d-4a9e-4c5a-9b3f-8e1d2c3a4b5c"
BITCHAT_CHARACTERISTIC_UUID = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"
COVER_TRAFFIC_PREFIX = "\xE2\x98\x82DUMMY\xE2\x98\x82"

FLAG_HAS_RECIPIENT = 0x01
FLAG_HAS_SIGNATURE = 0x02
FLAG_IS_COMPRESSED = 0x04

MSG_FLAG_IS_RELAY = 0x01
MSG_FLAG_IS_PRIVATE = 0x02
MSG_FLAG_HAS_ORIGINAL_SENDER = 0x04
MSG_FLAG_HAS_RECIPIENT_NICKNAME = 0x08
MSG_FLAG_HAS_SENDER_PEER_ID = 0x10
MSG_FLAG_HAS_MENTIONS = 0x20
MSG_FLAG_HAS_CHANNEL = 0x40
MSG_FLAG_IS_ENCRYPTED = 0x80

SIGNATURE_SIZE = 64
BROADCAST_RECIPIENT = b'\xFF' * 8

# Debug levels
class DebugLevel(IntEnum):
    CLEAN = 0
    BASIC = 1
    FULL = 2

DEBUG_LEVEL = DebugLevel.CLEAN

def debug_println(*args, **kwargs):
    if DEBUG_LEVEL >= DebugLevel.BASIC:
        try:
            print(*args, **kwargs)
        except BlockingIOError:
            # Silently ignore blocking errors in debug output
            pass

def debug_full_println(*args, **kwargs):
    if DEBUG_LEVEL >= DebugLevel.FULL:
        try:
            print(*args, **kwargs)
        except BlockingIOError:
            # Silently ignore blocking errors in debug output
            pass

# Message types
class MessageType(IntEnum):
    ANNOUNCE = 0x01
    KEY_EXCHANGE = 0x02
    LEAVE = 0x03
    MESSAGE = 0x04
    FRAGMENT_START = 0x05
    FRAGMENT_CONTINUE = 0x06
    FRAGMENT_END = 0x07
    CHANNEL_ANNOUNCE = 0x08
    CHANNEL_RETENTION = 0x09
    DELIVERY_ACK = 0x0A
    DELIVERY_STATUS_REQUEST = 0x0B
    READ_RECEIPT = 0x0C
    NOISE_HANDSHAKE_INIT = 0x10
    NOISE_HANDSHAKE_RESP = 0x11
    NOISE_ENCRYPTED = 0x12
    NOISE_IDENTITY_ANNOUNCE = 0x13
    CHANNEL_KEY_VERIFY_REQUEST = 0x14
    CHANNEL_KEY_VERIFY_RESPONSE = 0x15
    CHANNEL_PASSWORD_UPDATE = 0x16
    CHANNEL_METADATA = 0x17
    VERSION_HELLO = 0x20
    VERSION_ACK = 0x21

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

class DeliveryTracker:
    def __init__(self):
        self.pending_messages: Dict[str, Tuple[str, float, bool]] = {}
        self.sent_acks: Set[str] = set()
    
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
    
    def add_fragment(self, fragment_id: bytes, index: int, total: int, 
                    original_type: int, data: bytes, sender_id: str) -> Optional[Tuple[bytes, str]]:
        fragment_id_hex = fragment_id.hex()
        
        debug_full_println(f"[COLLECTOR] Adding fragment {index + 1}/{total} for ID {fragment_id_hex[:8]}")
        
        if fragment_id_hex not in self.fragments:
            debug_full_println(f"[COLLECTOR] Creating new fragment collection for ID {fragment_id_hex[:8]}")
            self.fragments[fragment_id_hex] = {}
            self.metadata[fragment_id_hex] = (total, original_type, sender_id)
        
        fragment_map = self.fragments[fragment_id_hex]
        fragment_map[index] = data
        debug_full_println(f"[COLLECTOR] Fragment {index + 1} stored. Have {len(fragment_map)}/{total} fragments")
        
        if len(fragment_map) == total:
            debug_full_println("[COLLECTOR] ✓ All fragments received! Reassembling...")
            
            complete_data = bytearray()
            for i in range(total):
                if i in fragment_map:
                    debug_full_println(f"[COLLECTOR] Appending fragment {i + 1} ({len(fragment_map[i])} bytes)")
                    complete_data.extend(fragment_map[i])
                else:
                    debug_full_println(f"[COLLECTOR] ✗ Missing fragment {i + 1}")
                    return None
            
            debug_full_println(f"[COLLECTOR] ✓ Reassembly complete: {len(complete_data)} bytes total")
            
            sender = self.metadata.get(fragment_id_hex, (0, 0, "Unknown"))[2]
            
            del self.fragments[fragment_id_hex]
            del self.metadata[fragment_id_hex]
            
            return (bytes(complete_data), sender)
        
        return None

# Helper functions

def print_banner():
    """Print the BitChat banner"""
    print("\n\033[38;5;46m##\\       ##\\   ##\\               ##\\                  ##\\")
    print("## |      \\__|  ## |              ## |                 ## |")
    print("#######\\  ##\\ ######\\    #######\\ #######\\   ######\\ ######\\")
    print("##  __##\\ ## |\\_##  _|  ##  _____|##  __##\\  \\____##\\\\_##  _|")
    print("## |  ## |## |  ## |    ## /      ## |  ## | ####### | ## |")
    print("## |  ## |## |  ## |##\\ ## |      ## |  ## |##  __## | ## |##\\")
    print("#######  |## |  \\####  |\\#######\\ ## |  ## |\\####### | \\####  |")
    print("\\_______/ \\__|   \\____/  \\_______|\\__|  \\__| \\_______|  \\____/\033[0m")
    print("\n\033[38;5;40m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m")
    print("\033[37mDecentralized • Encrypted • Peer-to-Peer • Open Source\033[0m")
    print(f"\033[37m         bitchat@-python {VERSION} @kaganisildak\033[0m")
    print("\033[38;5;40m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m\n")

def unpad_packet(data: bytes) -> bytes:
    """Remove PKCS#7 padding from packet data (matching iOS implementation)"""
    if len(data) == 0:
        return data
    
    # Last byte tells us how much padding to remove
    padding_length = int(data[-1])
    
    # Validate padding (matching iOS logic exactly)
    if padding_length <= 0 or padding_length > len(data):
        return data  # No padding or invalid padding
    
    # Remove the indicated number of bytes
    result = data[:-padding_length]
    return result

def parse_bitchat_packet(data: bytes) -> BitchatPacket:
    """Parse a BitChat packet from raw bytes"""
    HEADER_SIZE = 13
    SENDER_ID_SIZE = 8
    RECIPIENT_ID_SIZE = 8
    
    # Don't remove padding here - we need to parse the header first to know the actual packet size
    # The iOS client expects properly structured packets with padding intact during parsing
    
    if len(data) < HEADER_SIZE + SENDER_ID_SIZE:
        raise ValueError("Packet too small")
    
    offset = 0
    
    # Version
    version = data[offset]
    offset += 1
    if version != 1:
        raise ValueError("Unsupported version")
    
    # Type
    msg_type = MessageType(data[offset])
    offset += 1
    
    # TTL
    ttl = data[offset]
    offset += 1
    
    # Timestamp (skip)
    offset += 8
    
    # Flags
    flags = data[offset]
    offset += 1
    has_recipient = (flags & FLAG_HAS_RECIPIENT) != 0
    has_signature = (flags & FLAG_HAS_SIGNATURE) != 0
    is_compressed = (flags & FLAG_IS_COMPRESSED) != 0
    
    # Payload length
    payload_len = struct.unpack('>H', data[offset:offset+2])[0]
    offset += 2
    
    # Sender ID (trim null bytes)
    sender_id_raw = data[offset:offset+SENDER_ID_SIZE]
    # Remove trailing null bytes
    sender_id = sender_id_raw.rstrip(b'\x00')
    sender_id_str = sender_id.hex()
    offset += SENDER_ID_SIZE
    
    # Recipient ID
    recipient_id = None
    recipient_id_str = None
    if has_recipient:
        recipient_id_raw = data[offset:offset+RECIPIENT_ID_SIZE]
        # Remove trailing null bytes
        recipient_id = recipient_id_raw.rstrip(b'\x00')
        recipient_id_str = recipient_id.hex()
        offset += RECIPIENT_ID_SIZE
    
    # Payload
    payload_end = offset + payload_len
    payload = data[offset:payload_end]
    offset = payload_end

    # Signature
    signature = None
    if has_signature:
        if len(data) >= offset + SIGNATURE_SIZE:
            signature = data[offset:offset+SIGNATURE_SIZE]
        else:
            debug_println(f"[WARN] Packet has signature flag but not enough data for signature.")
    
    # Decompress if needed
    if is_compressed:
        payload = decompress(payload)
    
    # Ensure payload is bytes
    if isinstance(payload, bytearray):
        payload = bytes(payload)
    
    return BitchatPacket(
        msg_type, sender_id, sender_id_str,
        recipient_id, recipient_id_str, payload, ttl
    )

def parse_bitchat_message_payload(data: bytes) -> BitchatMessage:
    """Parse message payload, matching Swift implementation"""
    offset = 0

    # 1. Flags
    flags = data[offset]; offset += 1
    is_private = (flags & MSG_FLAG_IS_PRIVATE) != 0
    has_sender_peer_id = (flags & MSG_FLAG_HAS_SENDER_PEER_ID) != 0
    has_channel = (flags & MSG_FLAG_HAS_CHANNEL) != 0
    is_encrypted = (flags & MSG_FLAG_IS_ENCRYPTED) != 0

    # 2. Timestamp
    offset += 8 # Skip timestamp

    # 3. ID
    id_len = data[offset]; offset += 1
    id_str = data[offset:offset+id_len].decode('utf-8'); offset += id_len

    # 4. Sender
    sender_len = data[offset]; offset += 1
    sender = data[offset:offset+sender_len].decode('utf-8'); offset += sender_len

    # 5. Content
    content_len = struct.unpack('>H', data[offset:offset+2])[0]; offset += 2
    content_bytes = data[offset:offset+content_len]; offset += content_len
    content = ""
    encrypted_content = None
    if is_encrypted:
        encrypted_content = content_bytes
    else:
        content = content_bytes.decode('utf-8', errors='ignore')

    # 6. Sender Peer ID
    if has_sender_peer_id:
        peer_id_len = data[offset]; offset += 1
        offset += peer_id_len # Skip peer id

    # 7. Channel
    channel = None
    if has_channel:
        channel_len = data[offset]; offset += 1
        channel = data[offset:offset+channel_len].decode('utf-8')

    return BitchatMessage(id_str, content, channel, is_encrypted, encrypted_content)

def create_bitchat_packet(sender_id: str, msg_type: MessageType, payload: bytes) -> bytes:
    """Create a BitChat packet"""
    return create_bitchat_packet_with_recipient(sender_id, None, msg_type, payload, None)

def create_bitchat_packet_with_signature(sender_id: str, msg_type: MessageType, 
                                        payload: bytes, signature: Optional[bytes]) -> bytes:
    """Create a BitChat packet with signature"""
    return create_bitchat_packet_with_recipient(sender_id, None, msg_type, payload, signature)

def create_bitchat_packet_with_recipient_and_signature(sender_id: str, recipient_id: str,
                                                      msg_type: MessageType, payload: bytes,
                                                      signature: Optional[bytes]) -> bytes:
    """Create a BitChat packet with recipient and signature"""
    return create_bitchat_packet_with_recipient(sender_id, recipient_id, msg_type, payload, signature)

def create_bitchat_packet_with_recipient(sender_id: str, recipient_id: Optional[str],
                                       msg_type: MessageType, payload: bytes,
                                       signature: Optional[bytes]) -> bytes:
    """Create a BitChat packet with all options"""
    debug_full_println(f"[RAW SEND] Creating packet: type={msg_type.name}, payload_len={len(payload)}")
    
    # Create the packet first
    packet = bytearray()
    
    # Version
    packet.append(1)
    
    # Type
    packet.append(msg_type.value)
    
    # TTL
    packet.append(7)
    
    # Timestamp
    timestamp_ms = int(time.time() * 1000)
    packet.extend(struct.pack('>Q', timestamp_ms))
    
    # Flags
    flags = 0
    # Include recipient field if:
    # 1. A specific recipient is provided (targeted message), OR  
    # 2. This is a message type that uses broadcast recipient (not fragments)
    exclude_recipient_types = [MessageType.FRAGMENT_START, MessageType.FRAGMENT_CONTINUE, MessageType.FRAGMENT_END]
    if recipient_id is not None or msg_type not in exclude_recipient_types:
        flags |= FLAG_HAS_RECIPIENT
    if signature:
        flags |= FLAG_HAS_SIGNATURE
    packet.append(flags)
    
    # Payload length
    packet.extend(struct.pack('>H', len(payload)))
    
    # Sender ID (exactly 8 bytes, padded with zeros if needed)
    sender_bytes = bytes.fromhex(sender_id)
    packet.extend(sender_bytes[:8])  # Take first 8 bytes
    if len(sender_bytes) < 8:
        packet.extend(bytes(8 - len(sender_bytes)))  # Pad with zeros
    
    # Recipient ID (exactly 8 bytes if present)
    if flags & FLAG_HAS_RECIPIENT:
        if recipient_id:
            recipient_bytes = bytes.fromhex(recipient_id)
            packet.extend(recipient_bytes[:8])  # Take first 8 bytes
            if len(recipient_bytes) < 8:
                packet.extend(bytes(8 - len(recipient_bytes)))  # Pad with zeros
        else:
            packet.extend(BROADCAST_RECIPIENT)
    
    # Payload
    packet.extend(payload)
    
    # Signature
    if signature:
        packet.extend(signature)
    
    # Apply iOS-style padding to standard block sizes for traffic analysis resistance
    # iOS pads ALL packets to 256 bytes for consistent BLE transmission
    block_sizes = [256, 512, 1024, 2048]
    # Account for encryption overhead (~16 bytes for AES-GCM tag)
    total_size = len(packet) + 16
    
    # Find smallest block that fits
    target_size = None
    for block_size in block_sizes:
        if total_size <= block_size:
            target_size = block_size
            break
    
    if target_size is None:
        # For very large messages, just use the original size (will be fragmented anyway)
        target_size = len(packet)
    
    padding_needed = target_size - len(packet)
    
    # PKCS#7 only supports padding up to 255 bytes
    # If we need more padding than that, don't pad - return original data
    if 0 < padding_needed <= 255:
        # iOS-style PKCS#7 padding: random bytes + padding length as last byte
        padding = bytearray(os.urandom(padding_needed - 1))
        padding.append(padding_needed)
        packet.extend(padding)

    # Add hex logging to match iOS format
    final_packet = bytes(packet)
    hex_string = ' '.join(f'{b:02X}' for b in final_packet)
    debug_full_println(f"[RAW SEND] {hex_string}")
    
    return final_packet

def create_bitchat_message_payload_full(sender: str, content: str, channel: Optional[str],
                                      is_private: bool, sender_peer_id: str, is_encrypted: bool, encrypted_content: Optional[bytes]) -> Tuple[bytes, str]:
    """Create message payload with all fields, matching Swift implementation"""
    data = bytearray()
    message_id = str(uuid.uuid4())

    # 1. Flags
    flags = 0
    if is_private: flags |= MSG_FLAG_IS_PRIVATE
    if sender_peer_id: flags |= MSG_FLAG_HAS_SENDER_PEER_ID
    if channel: flags |= MSG_FLAG_HAS_CHANNEL
    if is_encrypted: flags |= MSG_FLAG_IS_ENCRYPTED
    data.append(flags)

    # 2. Timestamp
    timestamp_ms = int(time.time() * 1000)
    data.extend(struct.pack('>Q', timestamp_ms))

    # 3. ID
    id_bytes = message_id.encode('utf-8')
    data.append(len(id_bytes))
    data.extend(id_bytes)

    # 4. Sender
    sender_bytes = sender.encode('utf-8')
    data.append(len(sender_bytes))
    data.extend(sender_bytes)

    # 5. Content
    payload_bytes = encrypted_content if is_encrypted and encrypted_content else content.encode('utf-8')
    data.extend(struct.pack('>H', len(payload_bytes)))
    data.extend(payload_bytes)

    # 6. Sender Peer ID
    if sender_peer_id:
        peer_id_bytes = sender_peer_id.encode('utf-8')
        data.append(len(peer_id_bytes))
        data.extend(peer_id_bytes)

    # 7. Channel
    if channel:
        channel_bytes = channel.encode('utf-8')
        data.append(len(channel_bytes))
        data.extend(channel_bytes)

    return (bytes(data), message_id)


    
    return (bytes(data), message_id)

def unpad_message(data: bytes) -> bytes:
    """Remove PKCS#7 padding"""
    if not data:
        return data
    
    padding_length = data[-1]
    
    if padding_length == 0 or padding_length > len(data) or padding_length > 255:
        return data
    
    return data[:-padding_length]

def create_encrypted_channel_message_payload(sender: str, content: str, channel: str, key: bytes, encryption_service, sender_peer_id: str) -> Tuple[bytes, str]:
    """Create encrypted channel message payload"""
    encrypted_content = encryption_service.encrypt_with_key(content.encode(), key)
    return create_bitchat_message_payload_full(sender, content, channel, False, sender_peer_id, True, encrypted_content)

def should_fragment(packet: bytes) -> bool:
    """Check if packet needs fragmentation"""
    return len(packet) > 500

def should_send_ack(is_private: bool, channel: Optional[str], mentions: Optional[List[str]],
                   my_nickname: str, active_peer_count: int) -> bool:
    """Determine if we should send an ACK"""
    if is_private:
        return True
    elif channel:
        if active_peer_count < 10:
            return True
        elif mentions and my_nickname in mentions:
            return True
    return False

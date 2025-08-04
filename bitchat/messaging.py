"""Messaging helpers for BitchatClient"""
from __future__ import annotations
from typing import TYPE_CHECKING, Optional
from datetime import datetime
import time
import uuid
import asyncio
import hashlib
import json
from .protocol import debug_println

from .protocol import (
    MessageType,
    create_bitchat_packet,
    create_bitchat_packet_with_recipient,
    create_bitchat_message_payload_full,
    create_encrypted_channel_message_payload,
)
from .terminal_ux import format_message_display, Channel
from .protocol import DeliveryAck

if TYPE_CHECKING:
    from .client import BitchatClient


async def send_public_message(client: 'BitchatClient', content: str) -> None:
    """Send a public or channel message"""
    if not client.client or not client.characteristic:
        print("\033[93m⚠ Not connected to any peers yet.\033[0m")
        print("\033[90mYour message will be sent once a connection is established.\033[0m")
        return

    current_channel: Optional[str] = None
    if isinstance(client.chat_context.current_mode, Channel):
        current_channel = client.chat_context.current_mode.name
        if current_channel in client.password_protected_channels and current_channel not in client.channel_keys:
            print(f"❌ Cannot send to password-protected channel {current_channel}. Join with password first.")
            return

    if current_channel and current_channel in client.channel_keys:
        creator_fingerprint = client.channel_creators.get(current_channel, '')
        encrypted_content = client.encryption_service.encrypt_for_channel(
            content, current_channel, client.channel_keys[current_channel], creator_fingerprint
        )
        payload, message_id = create_bitchat_message_payload_full(
            client.nickname, content, current_channel, False, client.my_peer_id, True, encrypted_content
        )
    else:
        payload, message_id = create_bitchat_message_payload_full(
            client.nickname, content, current_channel, False, client.my_peer_id, False, None
        )

    client.delivery_tracker.track_message(message_id, content, False)

    message_packet = create_bitchat_packet(
        client.my_peer_id, MessageType.MESSAGE, payload
    )
    await client.send_packet(message_packet)

    timestamp = datetime.now()
    display = format_message_display(
        timestamp,
        client.nickname,
        content,
        False,
        bool(current_channel),
        current_channel,
        None,
        client.nickname,
    )
    print(f"\x1b[1A\r\033[K{display}")


async def send_private_message(
    client: 'BitchatClient',
    content: str,
    target_peer_id: str,
    target_nickname: str,
    message_id: Optional[str] = None,
) -> None:
    """Send a private encrypted message"""
    if not client.client or not client.characteristic:
        print("\033[93m⚠ Not connected to any peers yet.\033[0m")
        return

    if not client.encryption_service.is_session_established(target_peer_id):
        debug_println(f"[NOISE] No session with {target_peer_id}, need to establish handshake")
        msg_id = message_id if message_id else str(uuid.uuid4())
        if target_peer_id not in client.pending_private_messages:
            client.pending_private_messages[target_peer_id] = []
        client.pending_private_messages[target_peer_id].append((content, target_nickname, msg_id))
        debug_println(
            f"[NOISE] Queued private message for {target_peer_id}, {len(client.pending_private_messages[target_peer_id])} messages pending"
        )

        debug_println(f"[NOISE] Initiating handshake with {target_peer_id} for private message")
        current_time = time.time()
        if target_peer_id in client.handshake_attempt_times:
            last_attempt = client.handshake_attempt_times[target_peer_id]
            if current_time - last_attempt < client.handshake_timeout:
                debug_println(
                    f"[NOISE] Skipping handshake with {target_peer_id} - too recent (last attempt {current_time - last_attempt:.1f}s ago)"
                )
                print(f"\033[90m» Handshake already in progress with {target_nickname}, please wait...\033[0m")
                return
        client.handshake_attempt_times[target_peer_id] = current_time
        try:
            handshake_message = client.encryption_service.initiate_handshake(target_peer_id)
            handshake_packet = create_bitchat_packet_with_recipient(
                client.my_peer_id,
                target_peer_id,
                MessageType.NOISE_HANDSHAKE_INIT,
                handshake_message,
                None,
            )
            handshake_data = bytearray(handshake_packet)
            handshake_data[2] = 3
            await client.send_packet(bytes(handshake_data))
            debug_println(
                f"[NOISE] Sent handshake init to {target_peer_id}, payload size: {len(handshake_message)}"
            )
        except Exception as e:
            debug_println(f"[NOISE] Failed to initiate handshake: {e}")
            client.handshake_attempt_times.pop(target_peer_id, None)
            print(f"\033[91m✗ Failed to initiate secure connection with {target_nickname}\033[0m")
            return

        print(f"\033[90m» Initiating secure handshake with {target_nickname}...\033[0m")
        print("\033[90m» Your message will be sent automatically once the handshake completes.\033[0m")
        return

    debug_println(f"[PRIVATE] Sending encrypted message to {target_nickname}")

    payload, message_id = create_bitchat_message_payload_full(
        client.nickname, content, None, True, client.my_peer_id, False, None
    )

    client.delivery_tracker.track_message(message_id, content, True)

    inner_packet = create_bitchat_packet_with_recipient(
        client.my_peer_id,
        target_peer_id,
        MessageType.MESSAGE,
        payload,
        None,
    )
    inner_data = bytearray(inner_packet)
    inner_data[2] = 7
    inner_packet = bytes(inner_data)

    try:
        encrypted = client.encryption_service.encrypt_for_peer(target_peer_id, inner_packet)
        packet = create_bitchat_packet_with_recipient(
            client.my_peer_id,
            target_peer_id,
            MessageType.NOISE_ENCRYPTED,
            encrypted,
            None,
        )
        try:
            await client.send_packet(packet)
            timestamp = datetime.now()
            display = format_message_display(
                timestamp,
                client.nickname,
                content,
                True,
                False,
                None,
                target_nickname,
                client.nickname,
            )
            print(f"\x1b[1A\r\033[K{display}")
        except Exception as send_error:
            if "could not complete without blocking" in str(send_error):
                debug_println("[PRIVATE] BLE write blocked, retrying")
                print("\033[90m» Message queued (BLE congestion), retrying...\033[0m")
                await asyncio.sleep(0.5)
                await client.send_packet(packet)
                timestamp = datetime.now()
                display = format_message_display(
                    timestamp,
                    client.nickname,
                    content,
                    True,
                    False,
                    None,
                    target_nickname,
                    client.nickname,
                )
                print(f"\x1b[1A\r\033[K{display}")
            else:
                raise send_error
    except Exception as e:
        debug_println(f"[PRIVATE] Failed to encrypt private message: {e}")
        print(f"\033[91m✗ Failed to send encrypted message to {target_nickname}\033[0m")
        print(f"\033[90m» Error: {e}\033[0m")


def _ack_payload(ack: DeliveryAck) -> bytes:
    return json.dumps(
        {
            "originalMessageID": ack.original_message_id,
            "ackID": ack.ack_id,
            "recipientID": ack.recipient_id,
            "recipientNickname": ack.recipient_nickname,
            "timestamp": ack.timestamp,
            "hopCount": ack.hop_count,
        }
    ).encode()


async def send_delivery_ack(client: 'BitchatClient', message_id: str, sender_id: str, is_private: bool) -> None:
    ack_id = f"{message_id}-{client.my_peer_id}"
    if not client.delivery_tracker.should_send_ack(ack_id):
        return

    debug_println(f"[ACK] Sending delivery ACK for message {message_id}")

    ack = DeliveryAck(
        message_id,
        str(uuid.uuid4()),
        client.my_peer_id,
        client.nickname,
        int(time.time() * 1000),
        1,
    )

    payload = _ack_payload(ack)
    if is_private:
        try:
            payload = client.encryption_service.encrypt(payload, sender_id)
        except Exception:
            pass

    packet = create_bitchat_packet_with_recipient(
        client.my_peer_id,
        sender_id,
        MessageType.DELIVERY_ACK,
        payload,
        None,
    )
    data = bytearray(packet)
    data[2] = 3
    await client.send_packet(bytes(data))


async def send_channel_announce(
    client: 'BitchatClient', channel: str, is_protected: bool, key_commitment: Optional[str]
) -> None:
    payload = f"{channel}|{'1' if is_protected else '0'}|{client.my_peer_id}|{key_commitment or ''}"
    packet = create_bitchat_packet(
        client.my_peer_id,
        MessageType.CHANNEL_ANNOUNCE,
        payload.encode(),
    )
    data = bytearray(packet)
    data[2] = 5
    debug_println(f"[CHANNEL] Sending channel announce for {channel}")
    await client.send_packet(bytes(data))


__all__ = [
    'send_public_message',
    'send_private_message',
    'send_delivery_ack',
    'send_channel_announce',
]

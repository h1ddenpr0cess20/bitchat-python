# Command handling functions for BitchatClient
from __future__ import annotations
from typing import TYPE_CHECKING, Optional
import hashlib
import asyncio

from .encryption import EncryptionService
from .protocol import MessageType, create_bitchat_packet, create_encrypted_channel_message_payload, create_bitchat_message_payload_full, debug_println
from .persistence import encrypt_password
from .terminal_ux import Channel

if TYPE_CHECKING:
    from .client import BitchatClient


async def handle_join_channel(client: 'BitchatClient', line: str) -> None:
    """Handle /j command"""
    parts = line.split()
    if len(parts) < 2:
        print("\033[93m⚠ Usage: /j #<channel> [password]\033[0m")
        print("\033[90mExample: /j #general\033[0m")
        print("\033[90mExample: /j #private mysecret\033[0m")
        return

    channel_name = parts[1]
    password = parts[2] if len(parts) > 2 else None

    if not channel_name.startswith("#"):
        print("\033[93m⚠ Channel names must start with #\033[0m")
        print(f"\033[90mExample: /j #{channel_name}\033[0m")
        return

    if len(channel_name) > 25:
        print("\033[93m⚠ Channel name too long\033[0m")
        print("\033[90mMaximum 25 characters allowed.\033[0m")
        return

    if not all(c.isalnum() or c in '-_' for c in channel_name[1:]):
        print("\033[93m⚠ Invalid channel name\033[0m")
        print("\033[90mChannel names can only contain letters, numbers, hyphens and underscores.\033[0m")
        return

    if channel_name in client.password_protected_channels:
        if channel_name in client.channel_keys:
            client.discovered_channels.add(channel_name)
            client.chat_context.switch_to_channel(channel_name)
            print("> ", end='', flush=True)
            return

        if not password:
            print(f"❌ Channel {channel_name} is password-protected. Use: /j {channel_name} <password>")
            return

        if len(password) < 4:
            print("\033[93m⚠ Password too short\033[0m")
            print("\033[90mMinimum 4 characters required.\033[0m")
            return

        key = EncryptionService.derive_channel_key(password, channel_name)

        if channel_name in client.channel_key_commitments:
            test_commitment = hashlib.sha256(key).hexdigest()
            if test_commitment != client.channel_key_commitments[channel_name]:
                print(f"❌ wrong password for channel {channel_name}. please enter the correct password.")
                return

        client.channel_keys[channel_name] = key
        client.discovered_channels.add(channel_name)

        if client.app_state.identity_key:
            try:
                encrypted = encrypt_password(password, client.app_state.identity_key)
                client.app_state.encrypted_channel_passwords[channel_name] = encrypted
                await client.save_app_state()
            except Exception as e:
                debug_println(f"[CHANNEL] Failed to encrypt password: {e}")

        client.chat_context.switch_to_channel_silent(channel_name)
        print("\r\033[K\033[90m─────────────────────────\033[0m")
        print(f"\033[90m» Joined password-protected channel: {channel_name} 🔒\033[0m")

        if channel_name in client.channel_creators:
            key_commitment = hashlib.sha256(key).hexdigest()
            await client.send_channel_announce(channel_name, True, key_commitment)

        print("> ", end='', flush=True)
    else:
        if password:
            key = EncryptionService.derive_channel_key(password, channel_name)
            client.channel_keys[channel_name] = key
            client.discovered_channels.add(channel_name)
            client.chat_context.switch_to_channel_silent(channel_name)
            print("\r\033[K\033[90m─────────────────────────\033[0m")
            print(f"\033[90m» Joined password-protected channel: {channel_name} 🔒. Just type to send messages.\033[0m")

            if channel_name in client.channel_creators:
                key_commitment = hashlib.sha256(key).hexdigest()
                await client.send_channel_announce(channel_name, True, key_commitment)

            print("> ", end='', flush=True)
        else:
            client.discovered_channels.add(channel_name)
            print("\r\033[K", end='')
            client.chat_context.switch_to_channel(channel_name)
            client.channel_keys.pop(channel_name, None)
            print("> ", end='', flush=True)

    debug_println(client.chat_context.get_status_line())


async def handle_dm_command(client: 'BitchatClient', line: str) -> None:
    """Handle /dm command"""
    if not client.client or not client.client.is_connected:
        print("\033[93m⚠ Not connected to the BitChat network yet.\033[0m")
        print("\033[90mWait for a connection before sending direct messages.\033[0m")
        return

    parts = line.split(maxsplit=2)

    if len(parts) < 2:
        print("\033[93m⚠ Usage: /dm <nickname> [message]\033[0m")
        print("\033[90mExample: /dm Bob Hey there!\033[0m")
        return

    target_nickname = parts[1]
    message = parts[2] if len(parts) > 2 else None

    target_peer_id = None
    for peer_id, peer in client.peers.items():
        if peer.nickname == target_nickname:
            target_peer_id = peer_id
            break

    if not target_peer_id:
        print(f"\033[93m⚠ User '{target_nickname}' not found\033[0m")
        print("\033[90mThey may be offline or using a different nickname.\033[0m")
        return

    if message:
        await client.send_private_message(message, target_peer_id, target_nickname)
    else:
        client.chat_context.enter_dm_mode(target_nickname, target_peer_id)
        debug_println(client.chat_context.get_status_line())


async def handle_block_command(client: 'BitchatClient', line: str) -> None:
    """Handle /block command"""
    parts = line.split()

    if len(parts) == 1:
        if client.blocked_peers:
            blocked_nicks = []
            for peer_id, peer in client.peers.items():
                fingerprint = client.encryption_service.get_peer_fingerprint(peer_id)
                if fingerprint and fingerprint in client.blocked_peers and peer.nickname:
                    blocked_nicks.append(peer.nickname)

            if blocked_nicks:
                print(f"» Blocked peers: {', '.join(blocked_nicks)}")
            else:
                print(f"» Blocked peers (not currently online): {len(client.blocked_peers)}")
        else:
            print("» No blocked peers.")
    elif len(parts) == 2:
        target = parts[1].lstrip('@')
        target_peer_id = None
        for peer_id, peer in client.peers.items():
            if peer.nickname == target:
                target_peer_id = peer_id
                break

        if target_peer_id:
            fingerprint = client.encryption_service.get_peer_fingerprint(target_peer_id)
            if fingerprint:
                if fingerprint in client.blocked_peers:
                    print(f"» {target} is already blocked.")
                else:
                    client.blocked_peers.add(fingerprint)
                    await client.save_app_state()
                    print(f"\n\033[92m✓ Blocked {target}\033[0m")
                    print(f"\033[90m{target} will no longer be able to send you messages.\033[0m")
            else:
                print(f"» Cannot block {target}: No identity key received yet.")
        else:
            print(f"\033[93m⚠ User '{target}' not found\033[0m")
            print("\033[90mThey may be offline or haven't sent any messages yet.\033[0m")
    else:
        print("\033[93m⚠ Usage: /block @<nickname>\033[0m")
        print("\033[90mExample: /block @spammer\033[0m")


async def handle_unblock_command(client: 'BitchatClient', line: str) -> None:
    """Handle /unblock command"""
    parts = line.split()

    if len(parts) != 2:
        print("\033[93m⚠ Usage: /unblock @<nickname>\033[0m")
        print("\033[90mExample: /unblock @friend\033[0m")
        return

    target = parts[1].lstrip('@')
    target_peer_id = None
    for peer_id, peer in client.peers.items():
        if peer.nickname == target:
            target_peer_id = peer_id
            break

    if target_peer_id:
        fingerprint = client.encryption_service.get_peer_fingerprint(target_peer_id)
        if fingerprint:
            if fingerprint in client.blocked_peers:
                client.blocked_peers.remove(fingerprint)
                await client.save_app_state()
                print(f"\n\033[92m✓ Unblocked {target}\033[0m")
                print(f"\033[90m{target} can now send you messages again.\033[0m")
            else:
                print(f"\033[93m⚠ {target} is not blocked\033[0m")
        else:
            print(f"» Cannot unblock {target}: No identity key received.")
    else:
        print(f"\033[93m⚠ User '{target}' not found\033[0m")
        print("\033[90mThey may be offline or haven't sent any messages yet.\033[0m")


async def handle_leave_command(client: 'BitchatClient') -> None:
    """Handle /leave command"""
    if isinstance(client.chat_context.current_mode, Channel):
        channel = client.chat_context.current_mode.name

        leave_payload = channel.encode()
        leave_packet = create_bitchat_packet(
            client.my_peer_id, MessageType.LEAVE, leave_payload
        )

        leave_packet_data = bytearray(leave_packet)
        leave_packet_data[2] = 3

        await client.send_packet(bytes(leave_packet_data))

        client.channel_keys.pop(channel, None)
        client.password_protected_channels.discard(channel)
        client.channel_creators.pop(channel, None)
        client.channel_key_commitments.pop(channel, None)
        client.app_state.encrypted_channel_passwords.pop(channel, None)

        client.chat_context.remove_channel(channel)
        client.chat_context.switch_to_public()

        await client.save_app_state()

        print(f"\033[90m» Left channel {channel}\033[0m")
        print("> ", end='', flush=True)
    else:
        print("» You're not in a channel. Use /j #channel to join one.")


async def handle_pass_command(client: 'BitchatClient', line: str) -> None:
    """Handle /pass command"""
    if not isinstance(client.chat_context.current_mode, Channel):
        print("» You must be in a channel to use /pass.")
        return

    channel = client.chat_context.current_mode.name
    parts = line.split(maxsplit=1)

    if len(parts) < 2:
        print("\033[93m⚠ Usage: /pass <new password>\033[0m")
        print("\033[90mExample: /pass mysecret123\033[0m")
        return

    new_password = parts[1]

    if len(new_password) < 4:
        print("\033[93m⚠ Password too short\033[0m")
        print("\033[90mMinimum 4 characters required.\033[0m")
        return

    owner = client.channel_creators.get(channel)
    if owner and owner != client.my_peer_id:
        print("» Only the channel owner can change the password.")
        return

    if not owner:
        client.channel_creators[channel] = client.my_peer_id
        debug_println(f"[CHANNEL] Claiming ownership of {channel}")

    old_key = client.channel_keys.get(channel)
    new_key = EncryptionService.derive_channel_key(new_password, channel)

    client.channel_keys[channel] = new_key
    client.password_protected_channels.add(channel)

    if client.app_state.identity_key:
        try:
            encrypted = encrypt_password(new_password, client.app_state.identity_key)
            client.app_state.encrypted_channel_passwords[channel] = encrypted
        except Exception as e:
            debug_println(f"[CHANNEL] Failed to encrypt password: {e}")

    commitment_hex = hashlib.sha256(new_key).hexdigest()
    client.channel_key_commitments[channel] = commitment_hex

    if old_key:
        notify_msg = "🔐 Password changed by channel owner. Please update your password."
        try:
            client.encryption_service.encrypt_with_key(notify_msg.encode(), old_key)
            notify_payload, _ = create_encrypted_channel_message_payload(
                client.nickname, notify_msg, channel, old_key, client.encryption_service, client.my_peer_id
            )
            notify_packet = create_bitchat_packet(client.my_peer_id, MessageType.MESSAGE, notify_payload)
            await client.send_packet(notify_packet)
        except Exception:
            pass

    await client.send_channel_announce(channel, True, commitment_hex)

    init_msg = f"🔑 Password {'changed' if old_key else 'set'} | Channel {channel} password {'updated' if old_key else 'protected'} by {client.nickname} | Metadata: {client.my_peer_id.encode().hex()}"
    init_payload, _ = create_encrypted_channel_message_payload(
        client.nickname, init_msg, channel, new_key, client.encryption_service, client.my_peer_id
    )
    init_packet = create_bitchat_packet(client.my_peer_id, MessageType.MESSAGE, init_payload)
    await client.send_packet(init_packet)

    await client.save_app_state()

    print(f"» Password {'changed' if old_key else 'set'} for {channel}.")
    print(f"» Members will need to rejoin with: /j {channel} {new_password}")


async def handle_transfer_command(client: 'BitchatClient', line: str) -> None:
    """Handle /transfer command"""
    if not isinstance(client.chat_context.current_mode, Channel):
        print("» You must be in a channel to use /transfer.")
        return

    channel = client.chat_context.current_mode.name
    parts = line.split()

    if len(parts) != 2:
        print("\033[93m⚠ Usage: /transfer @<username>\033[0m")
        print("\033[90mExample: /transfer @newowner\033[0m")
        return

    owner_id = client.channel_creators.get(channel)
    if owner_id != client.my_peer_id:
        print("» Only the channel owner can transfer ownership.")
        return

    target = parts[1].lstrip('@')
    new_owner_id = None
    for peer_id, peer in client.peers.items():
        if peer.nickname == target:
            new_owner_id = peer_id
            break

    if not new_owner_id:
        print(f"\033[93m⚠ User '{target}' not found\033[0m")
        print("\033[90mMake sure they are online and you have the correct nickname.\033[0m")
        return

    client.channel_creators[channel] = new_owner_id
    await client.save_app_state()

    is_protected = channel in client.password_protected_channels
    key_commitment = None
    if is_protected and channel in client.channel_keys:
        key_commitment = hashlib.sha256(client.channel_keys[channel]).hexdigest()

    await client.send_channel_announce(channel, is_protected, key_commitment)

    print(f"» Transferred ownership of {channel} to {target}")

__all__ = [
    'handle_join_channel',
    'handle_dm_command',
    'handle_block_command',
    'handle_unblock_command',
    'handle_leave_command',
    'handle_pass_command',
    'handle_transfer_command',
]

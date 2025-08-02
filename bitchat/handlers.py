"""Packet handlers for BitchatClient"""
from __future__ import annotations
from typing import TYPE_CHECKING
import asyncio
import json
import random
import struct
import time
import uuid
from datetime import datetime

from bleak import BleakGATTCharacteristic

from .protocol import (
    Peer,
    BitchatPacket,
    BitchatMessage,
    DeliveryAck,
    MessageType,
    BROADCAST_RECIPIENT,
    debug_println,
    debug_full_println,
    parse_bitchat_packet,
    parse_bitchat_message_payload,
    create_bitchat_packet,
    create_bitchat_packet_with_recipient,
    create_bitchat_packet_with_signature,
    should_send_ack,
    unpad_message,
)
from .terminal_ux import format_message_display, PrivateDM

if TYPE_CHECKING:
    from .client import BitchatClient

async def notification_handler(client: "BitchatClient", sender: BleakGATTCharacteristic, data: bytes):
    """Handle incoming BLE notifications"""
    try:
        # Enhanced hex logging to match iOS format
        hex_string = ' '.join(f'{b:02X}' for b in data)
        debug_full_println(f"[RAW RECV] Received {len(data)} bytes")
        debug_full_println(f"[RAW RECV] {hex_string}")
    except BlockingIOError:
        # If even debug printing fails due to blocking, just silently continue
        pass
        
    try:
        packet = parse_bitchat_packet(data)
        
        # Ignore our own messages (they are already displayed when sent)
        if packet.sender_id_str == client.my_peer_id:
            return
        
        await client.handle_packet(packet, data)
        
    except Exception as e:
        try:
            debug_full_println(f"[ERROR] Failed to parse packet: {e}")
        except BlockingIOError:
            # Silently ignore blocking errors
            pass

async def handle_packet(client: "BitchatClient", packet: BitchatPacket, raw_data: bytes):
    """Handle incoming packet"""
    if packet.msg_type == MessageType.ANNOUNCE:
        await client.handle_announce(packet)
    elif packet.msg_type == MessageType.MESSAGE:
        await client.handle_message(packet, raw_data)
    elif packet.msg_type in [MessageType.FRAGMENT_START, MessageType.FRAGMENT_CONTINUE, MessageType.FRAGMENT_END]:
        await client.handle_fragment(packet, raw_data)
    elif packet.msg_type == MessageType.KEY_EXCHANGE:
        await client.handle_key_exchange(packet)
    elif packet.msg_type == MessageType.NOISE_HANDSHAKE_INIT:
        await client.handle_noise_handshake_init(packet)
    elif packet.msg_type == MessageType.NOISE_HANDSHAKE_RESP:
        await client.handle_noise_handshake_resp(packet)
    elif packet.msg_type == MessageType.NOISE_ENCRYPTED:
        await client.handle_noise_encrypted(packet, raw_data)
    elif packet.msg_type == MessageType.LEAVE:
        await client.handle_leave(packet)
    elif packet.msg_type == MessageType.CHANNEL_ANNOUNCE:
        await client.handle_channel_announce(packet)
    elif packet.msg_type == MessageType.NOISE_IDENTITY_ANNOUNCE:
        await client.handle_noise_identity_announce(packet)

async def handle_announce(client: "BitchatClient", packet: BitchatPacket):
    """Handle peer announcement"""
    peer_nickname = packet.payload.decode('utf-8', errors='ignore').strip()
    is_new_peer = packet.sender_id_str not in client.peers
    
    if packet.sender_id_str not in client.peers:
        client.peers[packet.sender_id_str] = Peer()
    
    client.peers[packet.sender_id_str].nickname = peer_nickname
    
    if is_new_peer:
        print(f"\r\033[K\033[33m{peer_nickname} connected\033[0m\n> ", end='', flush=True)
        debug_println(f"[<-- RECV] Announce: Peer {packet.sender_id_str} is now known as '{peer_nickname}'")
        
        # Apply tie-breaker logic like iOS client
        if client.my_peer_id < packet.sender_id_str:
            # We have lower ID, initiate handshake
            debug_println(f"[CRYPTO] Initiating Noise handshake with new peer {packet.sender_id_str} (tie-breaker: we have lower ID)")
            try:
                handshake_message = client.encryption_service.initiate_handshake(packet.sender_id_str)
                handshake_packet = create_bitchat_packet_with_recipient(
                    client.my_peer_id, packet.sender_id_str, MessageType.NOISE_HANDSHAKE_INIT, handshake_message, None
                )
                # Set TTL to 3 like iOS
                handshake_data = bytearray(handshake_packet)
                handshake_data[2] = 3
                handshake_packet = bytes(handshake_data)
                await client.send_packet(handshake_packet)
                debug_println(f"[NOISE] Sent handshake init to {packet.sender_id_str}, payload size: {len(handshake_message)}")
            except Exception as e:
                debug_println(f"[CRYPTO] Failed to initiate handshake: {e}")
                # Fallback to old key exchange
                key_exchange_payload = client.encryption_service.get_combined_public_key_data()
                key_exchange_packet = create_bitchat_packet(
                    client.my_peer_id, MessageType.KEY_EXCHANGE, key_exchange_payload
                )
                await client.send_packet(key_exchange_packet)
        else:
            # We have higher ID, send targeted identity announce to prompt them to initiate
            debug_println(f"[CRYPTO] Sending targeted identity announce to {packet.sender_id_str} (tie-breaker: they have lower ID)")
            try:
                timestamp_ms = int(time.time() * 1000)
                public_key_bytes = client.encryption_service.get_public_key()
                signing_public_key_bytes = client.encryption_service.get_signing_public_key_bytes()
                
                # Create binding data for signature
                timestamp_data = str(timestamp_ms).encode('utf-8')
                binding_data = client.my_peer_id.encode('utf-8') + public_key_bytes + timestamp_data
                signature = client.encryption_service.sign_data(binding_data)
                
                # Encode to binary format
                identity_payload = client.encode_noise_identity_announcement_binary(
                    client.my_peer_id, public_key_bytes, signing_public_key_bytes,
                    client.nickname, timestamp_ms, signature
                )
                
                identity_packet = create_bitchat_packet_with_recipient(
                    client.my_peer_id, packet.sender_id_str, MessageType.NOISE_IDENTITY_ANNOUNCE, 
                    identity_payload, signature
                )
                await client.send_packet(identity_packet)
            except Exception as e:
                debug_println(f"[CRYPTO] Failed to send targeted identity announce: {e}")

async def handle_message(client: "BitchatClient", packet: BitchatPacket, raw_data: bytes):
    """Handle chat message"""
    # Check if sender is blocked
    fingerprint = client.encryption_service.get_peer_fingerprint(packet.sender_id_str)
    if fingerprint and fingerprint in client.blocked_peers:
        debug_println(f"[BLOCKED] Ignoring message from blocked peer: {packet.sender_id_str}")
        return
    
    # Ensure peer exists in our peers dictionary
    if packet.sender_id_str not in client.peers:
        client.peers[packet.sender_id_str] = Peer()
    
    # Check if message is for us
    is_broadcast = packet.recipient_id == BROADCAST_RECIPIENT if packet.recipient_id else True
    is_for_us = is_broadcast or (packet.recipient_id_str == client.my_peer_id)
    
    if not is_for_us:
        # Relay if TTL > 1
        if packet.ttl > 1:
            await asyncio.sleep(random.uniform(0.01, 0.05))
            relay_data = bytearray(raw_data)
            relay_data[2] = packet.ttl - 1
            await client.send_packet(bytes(relay_data))
        return
    is_private_message = not is_broadcast and is_for_us
    decrypted_payload = None
    if is_private_message:
        try:
            decrypted_payload = client.encryption_service.decrypt_from_peer(packet.sender_id_str, packet.payload)
            debug_println("[PRIVATE] Successfully decrypted private message!")
        except NoiseError:
            debug_println("[PRIVATE] Failed to decrypt private message")
            return
    # Parse message first to check if it's actually a private message
    try:
        if is_private_message and decrypted_payload:
            unpadded = unpad_message(decrypted_payload)
            message = parse_bitchat_message_payload(unpadded)
        else:
            message = parse_bitchat_message_payload(packet.payload)
            
        # Update peer nickname from message sender if available
        if message and hasattr(message, 'sender') and message.sender:
            if packet.sender_id_str not in client.peers:
                client.peers[packet.sender_id_str] = Peer()
            client.peers[packet.sender_id_str].nickname = message.sender
            
        # Check for duplicates using both bloom filter and set
        if message.id not in client.processed_messages:
            # Add to bloom filter and set
            client.bloom.add(message.id)
            client.processed_messages.add(message.id)
            
            # Display the message
            await client.display_message(message, packet, is_private_message)
            
            # Send ACK if needed
            if should_send_ack(is_private_message, message.channel, None, client.nickname, len(client.peers)):
                await client.send_delivery_ack(message.id, packet.sender_id_str, is_private_message)
            
            # Relay if TTL > 1
            if packet.ttl > 1:
                await asyncio.sleep(random.uniform(0.01, 0.05))
                relay_data = bytearray(raw_data)
                relay_data[2] = packet.ttl - 1
                await client.send_packet(bytes(relay_data))
        else:
            debug_println(f"[DUPLICATE] Ignoring duplicate message: {message.id}")
                
    except Exception as e:
        debug_full_println(f"[ERROR] Failed to parse message: {e}")

async def display_message(client: "BitchatClient", message: BitchatMessage, packet: BitchatPacket, is_private: bool):
    """Display a message in the terminal"""
    # Get sender nickname from message first, then peers dictionary, finally fallback to packet sender ID
    sender_nick = message.sender or client.peers.get(packet.sender_id_str, Peer()).nickname or packet.sender_id_str
    
    # Track discovered channels
    if message.channel:
        client.discovered_channels.add(message.channel)
        if message.is_encrypted:
            client.password_protected_channels.add(message.channel)
    
    # Decrypt channel messages if we have the key
    display_content = message.content
    if message.is_encrypted and message.channel and message.channel in client.channel_keys:
        try:
            creator_fingerprint = client.channel_creators.get(message.channel, '')
            decrypted = client.encryption_service.decrypt_from_channel(
                message.encrypted_content,
                message.channel,
                client.channel_keys[message.channel],
                creator_fingerprint
            )
            display_content = decrypted
        except:
            display_content = "[Encrypted message - decryption failed]"
    elif message.is_encrypted:
        display_content = "[Encrypted message - join channel with password]"
    
    # Check for cover traffic
    if is_private and display_content.startswith(COVER_TRAFFIC_PREFIX):
        debug_println(f"[COVER] Discarding dummy message from {sender_nick}")
        return
    
    # Update chat context for private messages
    if is_private:
        client.chat_context.last_private_sender = (packet.sender_id_str, sender_nick)
        client.chat_context.add_dm(sender_nick, packet.sender_id_str)
    
    # Format and display
    timestamp = datetime.now()
    display = format_message_display(
        timestamp,
        sender_nick,
        display_content,
        is_private,
        bool(message.channel),
        message.channel,
        client.nickname if is_private else None,
        client.nickname
    )
    
    print(f"\r\033[K{display}")
    
    if is_private and not isinstance(client.chat_context.current_mode, PrivateDM):
        print("\033[90m» /reply to respond\033[0m")
    
    print("> ", end='', flush=True)

async def handle_fragment(client: "BitchatClient", packet: BitchatPacket, raw_data: bytes):
    """Handle message fragment"""
    if len(packet.payload) >= 13:
        fragment_id = packet.payload[0:8]
        index = struct.unpack('>H', packet.payload[8:10])[0]
        total = struct.unpack('>H', packet.payload[10:12])[0]
        original_type = packet.payload[12]
        fragment_data = packet.payload[13:]
        
        result = client.fragment_collector.add_fragment(
            fragment_id, index, total, original_type, fragment_data, packet.sender_id_str
        )
        
        if result:
            complete_data, _ = result
            reassembled_packet = parse_bitchat_packet(complete_data)
            await client.handle_packet(reassembled_packet, complete_data)
    
    # Relay fragment if TTL > 1
    if packet.ttl > 1:
        await asyncio.sleep(random.uniform(0.01, 0.05))
        relay_data = bytearray(raw_data)
        relay_data[2] = packet.ttl - 1
        await client.send_packet(bytes(relay_data))

async def handle_key_exchange(client: "BitchatClient", packet: BitchatPacket):
    """Handle key exchange"""
    try:
        # Convert bytearray to bytes for encryption service
        payload_bytes = bytes(packet.payload) if isinstance(packet.payload, bytearray) else packet.payload
        response = client.encryption_service.process_handshake_message(packet.sender_id_str, payload_bytes)
        if response:
            response_packet = create_bitchat_packet(
                client.my_peer_id, MessageType.KEY_EXCHANGE, response
            )
            await client.send_packet(response_packet)
        
        if client.encryption_service.is_session_established(packet.sender_id_str):
            debug_println(f"[CRYPTO] Handshake completed with {packet.sender_id_str}")
            # If this is a new peer after reconnection, send our key exchange too
            if packet.sender_id_str not in client.peers:
                debug_println(f"[CRYPTO] Sending key exchange response to new peer {packet.sender_id_str}")
                handshake_message = client.encryption_service.initiate_handshake(packet.sender_id_str)
                key_exchange_packet = create_bitchat_packet(
                    client.my_peer_id, MessageType.KEY_EXCHANGE, handshake_message
                )
                await client.send_packet(key_exchange_packet)

    except Exception as e:
        debug_println(f"[CRYPTO] Handshake failed with {packet.sender_id_str}: {e}")

async def handle_noise_handshake_init(client: "BitchatClient", packet: BitchatPacket):
    """Handle Noise handshake initiation"""
    debug_println(f"[NOISE] Received handshake init from {packet.sender_id_str}")
    debug_println(f"[NOISE] Recipient ID: {packet.recipient_id_str}, My ID: {client.my_peer_id}")
    
    # Check if this handshake is for us
    if packet.recipient_id_str and packet.recipient_id_str != client.my_peer_id:
        debug_println(f"[NOISE] Handshake not for us, ignoring")
        return
        
    # Check payload size 
    payload_size = len(packet.payload)
    debug_println(f"[NOISE] Handshake payload size: {payload_size} bytes")
    debug_println(f"[NOISE] Handshake payload hex: {packet.payload.hex()[:64]}...")
    
    try:
        # Convert bytearray to bytes for encryption service
        payload_bytes = bytes(packet.payload) if isinstance(packet.payload, bytearray) else packet.payload
        response = client.encryption_service.process_handshake_message(packet.sender_id_str, payload_bytes)
        debug_println(f"[NOISE] process_handshake_message returned: {bool(response)}, response size: {len(response) if response else 0}")
        
        if response:
            # Send handshake response with proper recipient
            response_packet = create_bitchat_packet_with_recipient(
                client.my_peer_id, packet.sender_id_str, MessageType.NOISE_HANDSHAKE_RESP, response, None
            )
            # Set TTL to 3 like iOS
            response_data = bytearray(response_packet)
            response_data[2] = 3
            await client.send_packet(bytes(response_data))
            debug_println(f"[NOISE] Sent handshake response to {packet.sender_id_str}, payload size: {len(response)}")
        
        if client.encryption_service.is_session_established(packet.sender_id_str):
            debug_println(f"[NOISE] Handshake completed with {packet.sender_id_str}")
            # Clear handshake attempt time on success (matching Swift)
            client.handshake_attempt_times.pop(packet.sender_id_str, None)
            peer_nickname = client.peers.get(packet.sender_id_str, Peer()).nickname or packet.sender_id_str
            print(f"\r\033[K\033[92m✓ Secure session established with {peer_nickname}\033[0m")
            print("> ", end='', flush=True)
            # Add small delay before sending pending messages to avoid BLE congestion
            await asyncio.sleep(0.1)
            # Send any pending private messages
            await client.send_pending_private_messages(packet.sender_id_str)
            
    except Exception as e:
        debug_println(f"[NOISE] Handshake init failed with {packet.sender_id_str}: {e}")
        import traceback
        debug_println(f"[NOISE] Handshake error details: {traceback.format_exc()}")
        # Clear any partial handshake state
        client.encryption_service.clear_handshake_state(packet.sender_id_str)

async def handle_noise_handshake_resp(client: "BitchatClient", packet: BitchatPacket):
    """Handle Noise handshake response"""
    debug_println(f"[NOISE] Received handshake response from {packet.sender_id_str}")
    debug_println(f"[NOISE] Recipient ID: {packet.recipient_id_str}, My ID: {client.my_peer_id}")
    
    # Check if this handshake response is for us
    if packet.recipient_id_str and packet.recipient_id_str != client.my_peer_id:
        debug_println(f"[NOISE] Handshake response not for us, ignoring")
        return
    
    payload_size = len(packet.payload)
    debug_println(f"[NOISE] Handshake response payload size: {payload_size} bytes")
    debug_println(f"[NOISE] Handshake response payload hex: {packet.payload.hex()[:64]}...")
    
    try:
        # Convert bytearray to bytes for encryption service
        payload_bytes = bytes(packet.payload) if isinstance(packet.payload, bytearray) else packet.payload
        response = client.encryption_service.process_handshake_message(packet.sender_id_str, payload_bytes)
        debug_println(f"[NOISE] process_handshake_message returned: {bool(response)}, response size: {len(response) if response else 0}")
        
        if response:
            # Send final handshake message
            final_packet = create_bitchat_packet_with_recipient(
                client.my_peer_id, packet.sender_id_str, MessageType.NOISE_HANDSHAKE_INIT, response, None  # Continue with same type
            )
            # Set TTL to 3 like iOS
            final_data = bytearray(final_packet)
            final_data[2] = 3
            await client.send_packet(bytes(final_data))
            debug_println(f"[NOISE] Sent final handshake message to {packet.sender_id_str}, payload size: {len(response)}")
        
        if client.encryption_service.is_session_established(packet.sender_id_str):
            debug_println(f"[NOISE] Handshake completed with {packet.sender_id_str}")
            # Clear handshake attempt time on success (matching Swift)
            client.handshake_attempt_times.pop(packet.sender_id_str, None)
            peer_nickname = client.peers.get(packet.sender_id_str, Peer()).nickname or packet.sender_id_str
            print(f"\r\033[K\033[92m✓ Secure session established with {peer_nickname}\033[0m")
            print("> ", end='', flush=True)
            # Add small delay before sending pending messages to avoid BLE congestion
            await asyncio.sleep(0.1)
            # Send any pending private messages
            await client.send_pending_private_messages(packet.sender_id_str)
            
    except Exception as e:
        debug_println(f"[NOISE] Handshake response failed with {packet.sender_id_str}: {e}")
        import traceback
        debug_println(f"[NOISE] Handshake error details: {traceback.format_exc()}")
        # Clear any partial handshake state
        client.encryption_service.clear_handshake_state(packet.sender_id_str)

async def handle_noise_encrypted(client: "BitchatClient", packet: BitchatPacket, raw_data: bytes):
    """Handle Noise encrypted message"""
    debug_println(f"[NOISE] Received encrypted message from {packet.sender_id_str}")
    
    # Check if sender is blocked
    fingerprint = client.encryption_service.get_peer_fingerprint(packet.sender_id_str)
    if fingerprint and fingerprint in client.blocked_peers:
        debug_println(f"[BLOCKED] Ignoring encrypted message from blocked peer: {packet.sender_id_str}")
        return
    
    try:
        # Convert bytearray to bytes for encryption service
        payload_bytes = bytes(packet.payload) if isinstance(packet.payload, bytearray) else packet.payload
        
        # Decrypt the Noise encrypted payload using the improved method
        decrypted_payload = client.encryption_service.decrypt_from_peer(packet.sender_id_str, payload_bytes)
        debug_println(f"[NOISE] Successfully decrypted {len(decrypted_payload)} bytes from {packet.sender_id_str}")
        
        # The decrypted payload should be a complete BitchatPacket (matching Swift implementation)
        # Swift creates: BitchatPacket(type: MessageType.message, ...) and encrypts the whole packet
        
        try:
            # Check if the decrypted data starts with version 1 (BitchatPacket)
            if len(decrypted_payload) > 0 and decrypted_payload[0] == 1:
                # Parse the decrypted data as a complete BitchatPacket
                inner_packet = parse_bitchat_packet(decrypted_payload)
                if inner_packet:
                    debug_println(f"[NOISE] Decrypted inner packet: type={inner_packet.msg_type.name if hasattr(inner_packet.msg_type, 'name') else inner_packet.msg_type}, sender={inner_packet.sender_id_str}")
                    
                    # Verify this is a MESSAGE packet (as created by Swift)
                    if inner_packet.msg_type == MessageType.MESSAGE:
                        # Parse the message payload from the inner packet
                        try:
                            message = parse_bitchat_message_payload(inner_packet.payload)
                            
                            # Check for duplicates
                            if message.id not in client.processed_messages:
                                client.bloom.add(message.id)
                                client.processed_messages.add(message.id)
                                
                                # Display the message as private
                                await client.display_message(message, packet, True)
                                
                                # Send ACK
                                await client.send_delivery_ack(message.id, packet.sender_id_str, True)
                            else:
                                debug_println(f"[DUPLICATE] Ignoring duplicate encrypted message: {message.id}")
                                
                        except Exception as e:
                            debug_println(f"[NOISE] Failed to parse inner message payload: {e}")
                    else:
                        debug_println(f"[NOISE] Unexpected inner packet type: {inner_packet.msg_type}, expected MESSAGE")
                        # Handle other types of inner packets if needed
                        await client.handle_packet(inner_packet, decrypted_payload)
                else:
                    debug_println(f"[NOISE] Failed to parse decrypted data as BitchatPacket")
            else:
                # Handle non-BitchatPacket data (likely JSON acknowledgments or receipts)
                debug_println(f"[NOISE] Decrypted data does not start with version 1, likely acknowledgment/receipt")
                try:
                    # Try to parse as JSON (iOS read receipts/acks start with newline + JSON)
                    data_str = decrypted_payload.decode('utf-8').strip()
                    if data_str.startswith('{') and data_str.endswith('}'):
                        import json
                        ack_data = json.loads(data_str)
                        debug_println(f"[NOISE] Received acknowledgment: {ack_data}")
                        # Handle acknowledgment data if needed
                    else:
                        debug_println(f"[NOISE] Unknown decrypted data format")
                except Exception as json_e:
                    debug_println(f"[NOISE] Failed to parse as JSON acknowledgment: {json_e}")
                    
        except Exception as e:
            debug_println(f"[NOISE] Error parsing decrypted inner packet: {e}")
            # Log the first few bytes for debugging
            preview = decrypted_payload[:50] if len(decrypted_payload) >= 50 else decrypted_payload
            debug_println(f"[NOISE] Decrypted data preview: {preview.hex() if isinstance(preview, bytes) else preview}")
            
    except Exception as e:
        debug_println(f"[NOISE] Failed to decrypt message from {packet.sender_id_str}: {e}")
        # Check if we have a session with this peer
        if not client.encryption_service.is_session_established(packet.sender_id_str):
            debug_println(f"[NOISE] No session established with {packet.sender_id_str}")
        else:
            debug_println(f"[NOISE] Session exists but decryption failed - possible key sync issue")
            # If it's an InvalidTag error, it might be a nonce sync issue
            if "InvalidTag" in str(e):
                debug_println(f"[NOISE] InvalidTag suggests nonce desync - this could be from iOS sending acknowledgments")
                # Don't reset the session here, just log it
                # The nonce is already incremented by the failed decrypt attempt

async def handle_leave(client: "BitchatClient", packet: BitchatPacket):
    """Handle leave notification"""
    payload_str = packet.payload.decode('utf-8', errors='ignore').strip()
    
    if payload_str.startswith('#'):
        # Channel leave
        channel = payload_str
        sender_nick = client.peers.get(packet.sender_id_str, Peer()).nickname or packet.sender_id_str
        
        if isinstance(client.chat_context.current_mode, Channel) and \
           client.chat_context.current_mode.name == channel:
            print(f"\r\033[K\033[90m« {sender_nick} left {channel}\033[0m\n> ", end='', flush=True)
        
        debug_println(f"[<-- RECV] {sender_nick} left channel {channel}")
    else:
        # Peer disconnect
        disconnected_peer = client.peers.pop(packet.sender_id_str, None)
        if disconnected_peer and disconnected_peer.nickname:
            print(f"\r\033[K\033[33m{disconnected_peer.nickname} disconnected\033[0m\n> ", end='', flush=True)
            
            # Remove from active DMs
            if disconnected_peer.nickname in client.chat_context.active_dms:
                del client.chat_context.active_dms[disconnected_peer.nickname]
                
            # Clear pending messages for this peer
            if packet.sender_id_str in client.pending_private_messages:
                del client.pending_private_messages[packet.sender_id_str]
                
            # Clear encryption session for this peer
            client.encryption_service.remove_session(packet.sender_id_str)
            debug_println(f"[NOISE] Cleared session for disconnected peer {packet.sender_id_str}")
                
            # If we're in a DM with this peer, switch to public
            if isinstance(client.chat_context.current_mode, PrivateDM) and \
               client.chat_context.current_mode.peer_id == packet.sender_id_str:
                client.chat_context.switch_to_public()
                print("\033[90m» Switched to public chat (peer disconnected)\033[0m\n> ", end='', flush=True)
                
        debug_println(f"[<-- RECV] Peer {packet.sender_id_str} ({payload_str}) has left")
        
        # If this was the last peer, we might be alone now
        if len(client.peers) == 0:
            print("\033[90m» You're now the only one in the network.\033[0m\n> ", end='', flush=True)

async def handle_channel_announce(client: "BitchatClient", packet: BitchatPacket):
    """Handle channel announcement"""
    payload_str = packet.payload.decode('utf-8', errors='ignore')
    parts = payload_str.split('|')
    
    if len(parts) >= 3:
        channel = parts[0]
        is_protected = parts[1] == '1'
        creator_id = parts[2]
        key_commitment = parts[3] if len(parts) > 3 else ""
        
        debug_println(f"[<-- RECV] Channel announce: {channel} (protected: {is_protected}, owner: {creator_id})")
        
        if creator_id:
            client.channel_creators[channel] = creator_id
        
        if is_protected:
            client.password_protected_channels.add(channel)
            if key_commitment:
                client.channel_key_commitments[channel] = key_commitment
        else:
            client.password_protected_channels.discard(channel)
            client.channel_keys.pop(channel, None)
            client.channel_key_commitments.pop(channel, None)
        
        client.chat_context.add_channel(channel)
        await client.save_app_state()

async def handle_delivery_ack(client: "BitchatClient", packet: BitchatPacket, raw_data: bytes):
    """Handle delivery acknowledgment"""
    is_for_us = packet.recipient_id_str == client.my_peer_id if packet.recipient_id_str else False
    
    if is_for_us:
        # Decrypt if needed
        ack_payload = packet.payload
        if packet.ttl == 3 and client.encryption_service.is_session_established(packet.sender_id_str):
            try:
                ack_payload = client.encryption_service.decrypt_from_peer(packet.sender_id_str, packet.payload)
            except:
                pass
        
        # Parse ACK
        try:
            ack_data = json.loads(ack_payload)
            ack = DeliveryAck(
                ack_data['originalMessageID'],
                ack_data['ackID'],
                ack_data['recipientID'],
                ack_data['recipientNickname'],
                ack_data['timestamp'],
                ack_data['hopCount']
            )
            
            if client.delivery_tracker.mark_delivered(ack.original_message_id):
                print(f"\r\u001b[K\u001b[90m✓ Delivered to {ack.recipient_nickname}\u001b[0m\n> ", end='', flush=True)
                
        except Exception as e:
            debug_println(f"[ACK] Failed to parse delivery ACK: {e}")
            
    elif packet.ttl > 1:
        # Relay ACK
        relay_data = bytearray(raw_data)
        relay_data[2] = packet.ttl - 1
        await client.send_packet(bytes(relay_data))

async def handle_noise_identity_announce(client: "BitchatClient", packet: BitchatPacket):
    """Handle Noise identity announcement"""
    try:
        sender_id = packet.sender_id_str
        debug_println(f"[NOISE] Received identity announcement from {sender_id}")
        
        # Skip if it's from ourselves
        if sender_id == client.my_peer_id:
            return
            
        # Try to decode the identity announcement
        announcement = None
        
        # First try binary format, then JSON fallback
        try:
            announcement = client.parse_noise_identity_announcement_binary(packet.payload)
        except Exception as be:
            debug_println(f"[NOISE] Binary decode failed: {be}")
            # Try JSON fallback for compatibility
            try:
                announcement_data = json.loads(packet.payload.decode('utf-8'))
                announcement = {
                    'peerID': announcement_data.get('peerID', sender_id),
                    'nickname': announcement_data.get('nickname', 'Unknown'),
                    'publicKey': announcement_data.get('publicKey', ''),
                    'signingPublicKey': announcement_data.get('signingPublicKey', ''),
                    'timestamp': announcement_data.get('timestamp', 0),
                    'signature': announcement_data.get('signature', '')
                }
            except Exception as je:
                debug_println(f"[NOISE] JSON decode also failed: {je}")
                debug_println(f"[NOISE] Raw payload (first 32 bytes): {packet.payload[:32].hex()}")
                return
        
        if not announcement:
            debug_println(f"[NOISE] Failed to decode identity announcement from {sender_id}")
            return
            
        peer_id = announcement['peerID']
        nickname = announcement['nickname']
        
        debug_println(f"[NOISE] Identity announcement: {peer_id} -> {nickname}")
        
        # Check if this is a new peer
        is_new_peer = peer_id not in client.peers
        
        # Update peer info
        if peer_id not in client.peers:
            client.peers[peer_id] = Peer()
        client.peers[peer_id].nickname = nickname
        
        if is_new_peer:
            print(f"\r\033[K\033[33m{nickname} connected\033[0m\n> ", end='', flush=True)
            debug_println(f"[<-- RECV] Announce: Peer {peer_id} is now known as '{nickname}'")
        
        # Check if we should initiate handshake (lexicographic comparison)
        if client.my_peer_id < peer_id:
            debug_println(f"[NOISE] We should initiate handshake with {peer_id}")
            # Check if we already have a session or ongoing handshake
            if not client.encryption_service.is_session_established(peer_id):
                try:
                    handshake_message = client.encryption_service.initiate_handshake(peer_id)
                    handshake_packet = create_bitchat_packet_with_recipient(
                        client.my_peer_id, peer_id, MessageType.NOISE_HANDSHAKE_INIT, handshake_message, None
                    )
                    # Set TTL to 3 like iOS
                    handshake_data = bytearray(handshake_packet)
                    handshake_data[2] = 3
                    handshake_packet = bytes(handshake_data)
                    await client.send_packet(handshake_packet)
                    debug_println(f"[NOISE] Initiated handshake with {peer_id}")
                except Exception as e:
                    debug_println(f"[NOISE] Failed to initiate handshake: {e}")
        else:
            debug_println(f"[NOISE] Waiting for {peer_id} to initiate handshake")
                
    except Exception as e:
        debug_println(f"[NOISE] Error handling identity announcement: {e}")
        import traceback
        debug_println(f"[NOISE] Identity announce error details: {traceback.format_exc()}")

__all__ = [
    "notification_handler",
    "handle_packet",
    "handle_announce",
    "handle_message",
    "display_message",
    "handle_fragment",
    "handle_key_exchange",
    "handle_noise_handshake_init",
    "handle_noise_handshake_resp",
    "handle_noise_encrypted",
    "handle_leave",
    "handle_channel_announce",
    "handle_noise_identity_announce",
]

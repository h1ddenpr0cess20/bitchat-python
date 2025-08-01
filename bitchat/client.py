import asyncio
import sys
import os
import time
import json
import uuid
import struct
import hashlib
import random
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Set
from collections import defaultdict
import logging
import base64

from bleak import BleakClient, BleakScanner, BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
import aioconsole
from pybloom_live import BloomFilter

from .encryption import EncryptionService, NoiseError
from .compression import compress_if_beneficial
from .fragmentation import Fragment, FragmentType, fragment_payload
from .terminal_ux import ChatContext, ChatMode, Public, Channel, PrivateDM, format_message_display, print_help, clear_screen
from . import commands, messaging, noise, handlers, user_input
from .persistence import AppState, load_state, save_state, encrypt_password, decrypt_password
from .protocol import (Peer, BitchatPacket, BitchatMessage, DeliveryAck, DeliveryTracker, FragmentCollector, MessageType, DebugLevel, debug_println, debug_full_println, parse_bitchat_packet, parse_bitchat_message_payload, create_bitchat_packet, create_bitchat_packet_with_signature, create_bitchat_message_payload_full, create_encrypted_channel_message_payload, should_fragment, should_send_ack, unpad_message, print_banner, BITCHAT_SERVICE_UUID, BITCHAT_CHARACTERISTIC_UUID)

class BitchatClient:
    def __init__(self):
        self.my_peer_id = os.urandom(8).hex()
        self.nickname = "my-python-client"
        self.peers: Dict[str, Peer] = {}
        self.bloom = BloomFilter(capacity=500, error_rate=0.01)
        self.processed_messages: Set[str] = set()  # Backup for message IDs
        self.fragment_collector = FragmentCollector()
        self.delivery_tracker = DeliveryTracker()
        self.chat_context = ChatContext()
        self.channel_keys: Dict[str, bytes] = {}
        self.app_state = AppState()
        self.blocked_peers: Set[str] = set()
        self.channel_creators: Dict[str, str] = {}
        self.password_protected_channels: Set[str] = set()
        self.channel_key_commitments: Dict[str, str] = {}
        self.discovered_channels: Set[str] = set()
        self.encryption_service = EncryptionService()
        self.client: Optional[BleakClient] = None
        self.characteristic: Optional[BleakGATTCharacteristic] = None
        self.running = True
        self.background_scanner_task = None  # Track background scanner task
        self.disconnection_callback_registered = False
        
        # Handshake timing tracking (like Swift implementation)
        self.handshake_attempt_times: Dict[str, float] = {}
        self.handshake_timeout = 5.0  # 5 seconds before retrying, matching Swift
        
        # Pending private messages waiting for handshake completion
        self.pending_private_messages: Dict[str, List[Tuple[str, str, str]]] = {}  # peer_id -> [(content, nickname, message_id)]
        
        # Setup encryption service callbacks for better handshake handling
        self.encryption_service.on_peer_authenticated = self._on_peer_authenticated
        self.encryption_service.on_handshake_required = self._on_handshake_required
    
    def _on_peer_authenticated(self, peer_id: str, fingerprint: str):
        """Callback when a peer is authenticated via Noise protocol"""
        debug_println(f"[NOISE] Peer {peer_id} authenticated with fingerprint: {fingerprint[:16]}...")
        
        # Send any pending private messages for this peer
        asyncio.create_task(self.send_pending_private_messages(peer_id))
        
    def _on_handshake_required(self, peer_id: str):
        """Callback when handshake is required for a peer"""
        debug_println(f"[NOISE] Handshake required for peer {peer_id}")
        # The handshake will be initiated when trying to send private messages
    
    async def send_pending_private_messages(self, peer_id: str):
        """Send all pending private messages for a peer after handshake completes"""
        if peer_id not in self.pending_private_messages:
            return
        
        pending_messages = self.pending_private_messages.pop(peer_id, [])
        if not pending_messages:
            return
        
        debug_println(f"[NOISE] Sending {len(pending_messages)} pending messages to {peer_id}")
        
        for content, nickname, message_id in pending_messages:
            try:
                # Add longer delay before sending to allow BLE queue to clear
                await asyncio.sleep(0.3)
                # Call the actual send function with established session
                await self.send_private_message(content, peer_id, nickname, message_id)
                # Small delay between messages
                await asyncio.sleep(0.2)
            except Exception as e:
                debug_println(f"[NOISE] Failed to send pending message to {peer_id}: {e}")
                # Re-queue the message if it's a temporary error
                if "blocking" in str(e).lower():
                    debug_println(f"[NOISE] Re-queuing message due to BLE congestion")
                    if peer_id not in self.pending_private_messages:
                        self.pending_private_messages[peer_id] = []
                    self.pending_private_messages[peer_id].append((content, nickname, message_id))
                    # Don't retry immediately, let it retry later
                    break
        
    async def find_device(self) -> Optional[BLEDevice]:
        """Scan for BitChat service"""
        debug_println("[1] Scanning for bitchat service...")
        
        devices = await BleakScanner.discover(
            timeout=5.0,
            service_uuids=[BITCHAT_SERVICE_UUID]
        )
        
        for device in devices:
            debug_full_println(f"Found device: {device.name} - {device.address}")
            return device
        
        return None
    
    def handle_disconnect(self, client: BleakClient):
        """Handle disconnection from peer"""
        print(f"\r\033[K\033[91m✗ Disconnected from BitChat network\033[0m")
        print("\033[90m» Scanning for other devices...\033[0m")
        print("> ", end='', flush=True)
        
        # Clear connection state
        self.client = None
        self.characteristic = None
        self.peers.clear()  # Clear peer list since we're disconnected
        self.chat_context.active_dms.clear()  # Clear DM list
        
        # Clear encryption sessions (but keep our own identity)
        self.encryption_service.sessions.clear()
        self.encryption_service.handshake_states.clear()
        
        # Clear pending private messages
        self.pending_private_messages.clear()
        
        # If in a DM, switch to public
        if isinstance(self.chat_context.current_mode, PrivateDM):
            self.chat_context.switch_to_public()
        
        # Restart background scanner if not already running
        if not self.background_scanner_task or self.background_scanner_task.done():
            self.background_scanner_task = asyncio.create_task(self.background_scanner())
    
    async def connect(self):
        """Connect to BitChat service"""
        print("\033[90m» Scanning for bitchat service...\033[0m")
        
        scan_attempts = 0
        max_initial_attempts = 10  # Try for ~10 seconds initially
        
        device = None
        while not device and self.running:
            device = await self.find_device()
            if not device:
                scan_attempts += 1
                if scan_attempts == max_initial_attempts:
                    print("\033[93m» No other BitChat devices found yet.\033[0m")
                    print("\033[90m» This might be because:\033[0m")
                    print("\033[90m  • You're the first one here (that's okay!)\033[0m")
                    print("\033[90m  • Other devices are out of Bluetooth range\033[0m")
                    print("\033[90m  • The iOS/Android app needs to be open\033[0m")
                    print("\033[90m» Continuing to scan in the background...\033[0m")
                    print("\033[90m» You can start using commands while waiting.\033[0m")
                    # Return True to continue without connection
                    return True
                await asyncio.sleep(1)
        
        if not self.running:
            return False
        
        print("\033[90m» Found bitchat service! Connecting...\033[0m")
        debug_println("[1] Match Found! Connecting...")
        
        try:
            self.client = BleakClient(device.address, disconnected_callback=self.handle_disconnect)
            await self.client.connect()
            
            # Find characteristic
            services = self.client.services
            if not services:
                raise Exception("No services found on device")
                
            for service in services:
                for char in service.characteristics:
                    if char.uuid.lower() == BITCHAT_CHARACTERISTIC_UUID.lower():
                        self.characteristic = char
                        debug_println(f"[2] Found characteristic: {char.uuid}")
                        break
                if self.characteristic:
                    break
            
            if not self.characteristic:
                raise Exception("Characteristic not found")
            
            # Subscribe to notifications
            await self.client.start_notify(self.characteristic, self.notification_handler)
            
            debug_println("[2] Connection established.")
            return True
            
        except Exception as e:
            print(f"\n\033[91m❌ Connection failed\033[0m")
            print(f"\033[90mReason: {e}\033[0m")
            print("\033[90mPlease check:\033[0m")
            print("\033[90m  • Bluetooth is enabled\033[0m")
            print("\033[90m  • The other device is running BitChat\033[0m")
            print("\033[90m  • You're within range\033[0m")
            print("\n\033[90mTry running the command again.\033[0m")
            return False
    
    async def handshake(self):
        """Perform initial handshake"""
        debug_println("[3] Performing handshake...")
        
        # Load persisted state
        self.app_state = load_state()
        if self.app_state.nickname:
            self.nickname = self.app_state.nickname
        
        # If we have a connection, send Noise identity announce and regular announce
        if self.client and self.characteristic:
            # Send Noise identity announcement first
            try:
                # Create a proper timestamp that matches iOS (milliseconds since epoch)
                timestamp_ms = int(time.time() * 1000)
                public_key_bytes = self.encryption_service.get_public_key()
                signing_public_key_bytes = self.encryption_service.get_signing_public_key_bytes()
                
                # Create binding data for signature (matching iOS)
                # iOS uses: peerID + publicKey + timestamp (as string)
                timestamp_data = str(timestamp_ms).encode('utf-8')
                binding_data = self.my_peer_id.encode('utf-8') + public_key_bytes + timestamp_data
                signature = self.encryption_service.sign_data(binding_data)
                
                # Encode to binary format
                identity_payload = self.encode_noise_identity_announcement_binary(
                    self.my_peer_id, public_key_bytes, signing_public_key_bytes,
                    self.nickname, timestamp_ms, signature
                )
                
                identity_packet = create_bitchat_packet_with_signature(
                    self.my_peer_id, MessageType.NOISE_IDENTITY_ANNOUNCE, identity_payload, signature
                )
                await self.send_packet(identity_packet)
                debug_println("[3] Sent Noise identity announcement (binary format)")
            except Exception as e:
                debug_println(f"[3] Failed to send identity announcement: {e}")
                import traceback
                debug_println(f"[3] Traceback: {traceback.format_exc()}")
                # Fallback to old key exchange
                key_data = self.encryption_service.get_combined_public_key_data()
                key_packet = create_bitchat_packet(
                    self.my_peer_id, MessageType.KEY_EXCHANGE, key_data
                )
                await self.send_packet(key_packet)
            
            # Wait a bit between packets
            await asyncio.sleep(0.5)
            
            # Send announce
            announce_packet = create_bitchat_packet(
                self.my_peer_id, MessageType.ANNOUNCE, self.nickname.encode()
            )
            await self.send_packet(announce_packet)
            
            debug_println("[3] Handshake sent. You can now chat.")
        else:
            debug_println("[3] No connection yet. Skipping handshake.")
            print("\033[90m» Running in offline mode. Waiting for peers...\033[0m")
        
        if self.app_state.nickname:
            print(f"\033[90m» Using saved nickname: {self.nickname}\033[0m")
        print("\033[90m» Type /status to see connection info\033[0m")
        
        # Restore state
        self.blocked_peers = self.app_state.blocked_peers
        self.channel_creators = self.app_state.channel_creators
        self.password_protected_channels = self.app_state.password_protected_channels
        self.channel_key_commitments = self.app_state.channel_key_commitments
        
        # Restore channel keys from saved passwords
        if self.app_state.identity_key:
            for channel, encrypted_password in self.app_state.encrypted_channel_passwords.items():
                try:
                    password = decrypt_password(encrypted_password, self.app_state.identity_key)
                    key = EncryptionService.derive_channel_key(password, channel)
                    self.channel_keys[channel] = key
                    debug_println(f"[CHANNEL] Restored key for password-protected channel: {channel}")
                except Exception as e:
                    debug_println(f"[CHANNEL] Failed to restore key for {channel}: {e}")
    
    async def send_packet(self, packet: bytes):
        """Send packet, with fragmentation if needed"""
        debug_full_println(f"[RAW SEND] {packet.hex()}")
        if not self.client or not self.characteristic:
            debug_println("[!] No connection available. Message queued.")
            # In a real implementation, we might queue messages here
            return
        
        # Check if still connected before sending
        if not self.client.is_connected:
            debug_println("[!] Connection lost. Cannot send packet.")
            # Trigger disconnection handling if not already done
            if self.client:
                self.handle_disconnect(self.client)
            return
            
        if should_fragment(packet):
            await self.send_packet_with_fragmentation(packet)
        else:
            write_with_response = len(packet) > 512
            try:
                # Add small delay to prevent blocking errors
                await asyncio.sleep(0.01)
                await self.client.write_gatt_char(
                    self.characteristic, 
                    packet, 
                    response=write_with_response
                )
            except Exception as e:
                # Check if this is a connection error
                if "not connected" in str(e).lower():
                    debug_println("[!] Lost connection while sending")
                    if self.client:
                        self.handle_disconnect(self.client)
                    return
                
                # Handle blocking errors by retrying without response
                if "could not complete without blocking" in str(e) or write_with_response:
                    try:
                        debug_println(f"[!] Write blocked, retrying without response after delay")
                        await asyncio.sleep(0.1)  # Longer delay for retry
                        await self.client.write_gatt_char(
                            self.characteristic, 
                            packet, 
                            response=False
                        )
                        debug_println(f"[!] Retry successful")
                    except Exception as e2:
                        if "not connected" in str(e2).lower():
                            debug_println("[!] Lost connection while sending")
                            if self.client:
                                self.handle_disconnect(self.client)
                        elif "could not complete without blocking" in str(e2):
                            debug_println(f"[!] Write still blocked after retry, dropping packet")
                            # Don't raise, just log and continue
                        else:
                            raise e2
                else:
                    raise e
    
    async def send_packet_with_fragmentation(self, packet: bytes):
        """Fragment and send large packets"""
        if not self.client or not self.characteristic:
            debug_println("[!] No connection available. Cannot send fragmented message.")
            return
        
        # Check if still connected
        if not self.client.is_connected:
            debug_println("[!] Connection lost. Cannot send fragmented packet.")
            if self.client:
                self.handle_disconnect(self.client)
            return
            
        debug_println(f"[FRAG] Original packet size: {len(packet)} bytes")
        
        fragment_size = 150  # Conservative size for iOS BLE
        chunks = [packet[i:i+fragment_size] for i in range(0, len(packet), fragment_size)]
        total_fragments = len(chunks)
        
        fragment_id = os.urandom(8)
        debug_println(f"[FRAG] Fragment ID: {fragment_id.hex()}")
        debug_println(f"[FRAG] Total fragments: {total_fragments}")
        
        for index, chunk in enumerate(chunks):
            if index == 0:
                fragment_type = MessageType.FRAGMENT_START
            elif index == len(chunks) - 1:
                fragment_type = MessageType.FRAGMENT_END
            else:
                fragment_type = MessageType.FRAGMENT_CONTINUE
            
            # Create fragment payload
            fragment_payload = bytearray()
            fragment_payload.extend(fragment_id)
            fragment_payload.extend(struct.pack('>H', index))
            fragment_payload.extend(struct.pack('>H', total_fragments))
            fragment_payload.append(MessageType.MESSAGE.value)
            fragment_payload.extend(chunk)
            
            fragment_packet = create_bitchat_packet(
                self.my_peer_id,
                fragment_type,
                bytes(fragment_payload)
            )
            
            try:
                await self.client.write_gatt_char(
                    self.characteristic,
                    fragment_packet,
                    response=False
                )
                
                debug_println(f"[FRAG] ✓ Fragment {index + 1}/{total_fragments} sent")
                
                if index < len(chunks) - 1:
                    await asyncio.sleep(0.02)  # 20ms delay
            except Exception as e:
                if "not connected" in str(e).lower():
                    debug_println(f"[FRAG] Connection lost while sending fragment {index + 1}")
                    if self.client:
                        self.handle_disconnect(self.client)
                    return
                else:
                    raise e
    
    async def notification_handler(self, sender: BleakGATTCharacteristic, data: bytes):
        await handlers.notification_handler(self, sender, data)

    async def handle_packet(self, packet: BitchatPacket, raw_data: bytes):
        await handlers.handle_packet(self, packet, raw_data)

    async def handle_announce(self, packet: BitchatPacket):
        await handlers.handle_announce(self, packet)

    async def handle_message(self, packet: BitchatPacket, raw_data: bytes):
        await handlers.handle_message(self, packet, raw_data)

    async def display_message(self, message: BitchatMessage, packet: BitchatPacket, is_private: bool):
        await handlers.display_message(self, message, packet, is_private)

    async def handle_fragment(self, packet: BitchatPacket, raw_data: bytes):
        await handlers.handle_fragment(self, packet, raw_data)

    async def handle_key_exchange(self, packet: BitchatPacket):
        await handlers.handle_key_exchange(self, packet)

    async def handle_noise_handshake_init(self, packet: BitchatPacket):
        await handlers.handle_noise_handshake_init(self, packet)

    async def handle_noise_handshake_resp(self, packet: BitchatPacket):
        await handlers.handle_noise_handshake_resp(self, packet)

    async def handle_noise_encrypted(self, packet: BitchatPacket, raw_data: bytes):
        await handlers.handle_noise_encrypted(self, packet, raw_data)

    async def handle_leave(self, packet: BitchatPacket):
        await handlers.handle_leave(self, packet)

    async def handle_channel_announce(self, packet: BitchatPacket):
        await handlers.handle_channel_announce(self, packet)

    async def handle_delivery_ack(self, packet: BitchatPacket, raw_data: bytes):
        await handlers.handle_delivery_ack(self, packet, raw_data)

    async def handle_noise_identity_announce(self, packet: BitchatPacket):
        await handlers.handle_noise_identity_announce(self, packet)


    async def handle_join_channel(self, line: str):
        await commands.handle_join_channel(self, line)
    async def handle_user_input(self, line: str):
        await user_input.handle_user_input(self, line)

    async def handle_dm_command(self, line: str):
        await commands.handle_dm_command(self, line)

    async def handle_block_command(self, line: str):
        await commands.handle_block_command(self, line)

    async def handle_unblock_command(self, line: str):
        await commands.handle_unblock_command(self, line)

    async def handle_leave_command(self):
        await commands.handle_leave_command(self)

    async def handle_pass_command(self, line: str):
        await commands.handle_pass_command(self, line)

    async def handle_transfer_command(self, line: str):
        await commands.handle_transfer_command(self, line)

    async def send_public_message(self, content: str):
        await messaging.send_public_message(self, content)

    async def send_private_message(self, content: str, target_peer_id: str, target_nickname: str, message_id: Optional[str] = None):
        await messaging.send_private_message(self, content, target_peer_id, target_nickname, message_id)

    async def send_delivery_ack(self, message_id: str, sender_id: str, is_private: bool):
        await messaging.send_delivery_ack(self, message_id, sender_id, is_private)

    async def send_channel_announce(self, channel: str, is_protected: bool, key_commitment: Optional[str]):
        await messaging.send_channel_announce(self, channel, is_protected, key_commitment)

    def parse_noise_identity_announcement_binary(self, data: bytes):
        return noise.parse_noise_identity_announcement_binary(data)

    def encode_noise_identity_announcement_binary(self, peer_id: str, public_key: bytes, signing_public_key: bytes, nickname: str, timestamp: int, signature: bytes, previous_peer_id: str = None):
        return noise.encode_noise_identity_announcement_binary(peer_id, public_key, signing_public_key, nickname, timestamp, signature, previous_peer_id)
    async def background_scanner(self):
        """Background task to scan for peers when not connected"""
        last_cleanup = time.time()
        
        while self.running:
            # Clean up old sessions periodically (every 5 minutes)
            current_time = time.time()
            if current_time - last_cleanup > 300:  # 5 minutes
                self.encryption_service.cleanup_old_sessions()
                last_cleanup = current_time
                debug_println(f"[CLEANUP] Cleaned up old encryption sessions")
            
            if not self.client or not self.client.is_connected:
                # Try to find and connect to a peer
                device = await self.find_device()
                if device:
                    print(f"\r\033[K\033[92m» Found a BitChat device! Connecting...\033[0m")
                    try:
                        self.client = BleakClient(device.address, disconnected_callback=self.handle_disconnect)
                        await self.client.connect()
                        
                        # Find characteristic
                        services = self.client.services
                        for service in services:
                            for char in service.characteristics:
                                if char.uuid.lower() == BITCHAT_CHARACTERISTIC_UUID.lower():
                                    self.characteristic = char
                                    break
                            if self.characteristic:
                                break
                        
                        if self.characteristic:
                            # Subscribe to notifications
                            await self.client.start_notify(self.characteristic, self.notification_handler)
                            print(f"\r\033[K\033[92m✓ Connected to BitChat network!\033[0m")
                            
                            # Clear any stale peers from previous connection
                            self.peers.clear()
                            
                            # Send Noise identity announcement
                            try:
                                timestamp_ms = int(time.time() * 1000)
                                public_key_bytes = self.encryption_service.get_public_key()
                                signing_public_key_bytes = self.encryption_service.get_signing_public_key_bytes()
                                
                                # Create binding data for signature
                                timestamp_data = str(timestamp_ms).encode('utf-8')
                                binding_data = self.my_peer_id.encode('utf-8') + public_key_bytes + timestamp_data
                                signature = self.encryption_service.sign_data(binding_data)
                                
                                # Encode to binary format
                                identity_payload = self.encode_noise_identity_announcement_binary(
                                    self.my_peer_id, public_key_bytes, signing_public_key_bytes,
                                    self.nickname, timestamp_ms, signature
                                )
                                
                                identity_packet = create_bitchat_packet_with_signature(
                                    self.my_peer_id, MessageType.NOISE_IDENTITY_ANNOUNCE, identity_payload, signature
                                )
                                await self.send_packet(identity_packet)
                            except Exception as e:
                                debug_println(f"[SCANNER] Failed to send identity: {e}")
                                # Fallback
                                key_exchange_payload = self.encryption_service.get_combined_public_key_data()
                                key_exchange_packet = create_bitchat_packet(
                                    self.my_peer_id, MessageType.KEY_EXCHANGE, key_exchange_payload
                                )
                                await self.send_packet(key_exchange_packet)
                            
                            await asyncio.sleep(0.5)
                            
                            announce_packet = create_bitchat_packet(
                                self.my_peer_id, MessageType.ANNOUNCE, self.nickname.encode()
                            )
                            await self.send_packet(announce_packet)
                            
                            print("> ", end='', flush=True)
                    except Exception as e:
                        debug_println(f"[SCANNER] Connection attempt failed: {e}")
                        self.client = None
                        self.characteristic = None
            
            # Wait before next scan
            await asyncio.sleep(5)  # Scan every 5 seconds when not connected
    
    async def input_loop(self):
        """Handle user input asynchronously"""
        while self.running:
            try:
                line = await aioconsole.ainput("> ")
                await self.handle_user_input(line)
            except KeyboardInterrupt:
                self.running = False
                break
            except Exception as e:
                debug_println(f"[ERROR] Input error: {e}")
    
    async def run(self):
        """Main run loop"""
        print_banner()
        
        # Parse command line arguments
        global DEBUG_LEVEL
        if "-dd" in sys.argv or "--debug-full" in sys.argv:
            DEBUG_LEVEL = DebugLevel.FULL
            print("🐛 Debug mode: FULL (verbose output)")
        elif "-d" in sys.argv or "--debug" in sys.argv:
            DEBUG_LEVEL = DebugLevel.BASIC
            print("🐛 Debug mode: BASIC (connection info)")
        
        # Connect to BLE
        connected = await self.connect()
        
        # Perform handshake (will work even without connection)
        await self.handshake()
        
        # Start background scanner if not connected
        scanner_task = None
        if not connected or not self.client:
            scanner_task = asyncio.create_task(self.background_scanner())
        
        # Run input loop
        try:
            await self.input_loop()
        except KeyboardInterrupt:
            pass
        finally:
            debug_println("\n[+] Disconnecting...")
            self.running = False
            
            # Send leave notification if connected
            if self.client and self.client.is_connected:
                pass
                try:
                    leave_packet = create_bitchat_packet(
                        self.my_peer_id, MessageType.LEAVE, self.nickname.encode()
                    )
                    await self.send_packet(leave_packet)
                    await asyncio.sleep(0.1)  # Give time for the packet to send
                except:
                    pass  # Ignore errors during shutdown
            
            # Cancel background scanner
            if scanner_task:
                scanner_task.cancel()
                try:
                    await scanner_task
                except asyncio.CancelledError:
                    pass
            
            if self.client and self.client.is_connected:
                pass
async def main():
    """Main entry point"""
    client = BitchatClient()
    await client.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[+] Exiting...")

"""Simple asynchronous wrapper around :class:`BitchatClient` for bot authors."""

from __future__ import annotations

import asyncio
from typing import Optional, Dict, Any

from .client import BitchatClient, BitchatMessage, BitchatPacket, Peer, COVER_TRAFFIC_PREFIX


class BitChatAPI:
    """High level non-interactive interface to interact with BitChat."""

    def __init__(self) -> None:
        self.client = BitchatClient()
        self._message_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

        # Override the client's display function to capture messages instead of
        # printing them to the terminal.  The original method performs a lot of
        # work, so we replicate only the essential parts here.
        async def _capture_message(
            message: BitchatMessage, packet: BitchatPacket, is_private: bool
        ) -> None:
            sender_nick = (
                self.client.peers.get(packet.sender_id_str, Peer()).nickname
                or packet.sender_id_str
            )

            content = message.content

            if (
                message.is_encrypted
                and message.channel
                and message.channel in self.client.channel_keys
            ):
                try:
                    creator_fp = self.client.channel_creators.get(message.channel, "")
                    content = self.client.encryption_service.decrypt_from_channel(
                        message.encrypted_content,
                        message.channel,
                        self.client.channel_keys[message.channel],
                        creator_fp,
                    )
                except Exception:
                    content = "[Encrypted message - decryption failed]"
            elif message.is_encrypted:
                content = "[Encrypted message - join channel with password]"

            # Ignore cover traffic used by iOS implementation
            if is_private and isinstance(content, str) and content.startswith(COVER_TRAFFIC_PREFIX):
                return

            await self._message_queue.put(
                {
                    "id": message.id,
                    "content": content,
                    "channel": message.channel,
                    "private": is_private,
                    "sender_id": packet.sender_id_str,
                    "sender_nickname": sender_nick,
                }
            )

        # Monkey patch the method
        self.client.display_message = _capture_message  # type: ignore[assignment]

    async def connect(self) -> bool:
        """Connect to the BitChat network."""
        connected = await self.client.connect()
        await self.client.handshake()

        if not connected or not self.client.client:
            # Start background scanner if no connection was made so the client
            # keeps trying to find peers.
            if not self.client.background_scanner_task or self.client.background_scanner_task.done():
                self.client.background_scanner_task = asyncio.create_task(
                    self.client.background_scanner()
                )

        return connected

    async def disconnect(self) -> None:
        """Disconnect from the network."""
        self.client.running = False
        if self.client.background_scanner_task:
            self.client.background_scanner_task.cancel()
            try:
                await self.client.background_scanner_task
            except asyncio.CancelledError:
                pass

        if self.client.client and self.client.client.is_connected:
            await self.client.client.disconnect()

    async def send_public_message(self, text: str) -> None:
        await self.client.send_public_message(text)

    async def send_private_message(
        self, text: str, peer_id: str, nickname: Optional[str] = None
    ) -> None:
        await self.client.send_private_message(text, peer_id, nickname or peer_id)

    async def send_message(
        self, text: str, peer_id: Optional[str] = None, nickname: Optional[str] = None
    ) -> None:
        """Send a public or private message."""
        if peer_id:
            await self.send_private_message(text, peer_id, nickname)
        else:
            await self.send_public_message(text)

    async def next_message(self) -> Dict[str, Any]:
        """Wait for and return the next received message."""
        return await self._message_queue.get()

    async def run_forever(self) -> None:
        """Keep the event loop alive until :meth:`disconnect` is called."""
        try:
            while self.client.running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass


class BitchatBotAPI(BitchatClient):
    """Simplified subclass of :class:`BitchatClient` for building bots."""

    def __init__(self, on_message=None) -> None:
        super().__init__()
        self.on_message = on_message
        self._message_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

    async def display_message(self, message: BitchatMessage, packet: BitchatPacket, is_private: bool):
        """Forward incoming messages to the callback and queue."""
        if callable(self.on_message):
            await self.on_message(message, packet, is_private)

        sender_nick = self.peers.get(packet.sender_id_str, Peer()).nickname or packet.sender_id_str
        content = message.content

        if (
            message.is_encrypted
            and message.channel
            and message.channel in self.channel_keys
        ):
            try:
                creator_fp = self.channel_creators.get(message.channel, "")
                content = self.encryption_service.decrypt_from_channel(
                    message.encrypted_content,
                    message.channel,
                    self.channel_keys[message.channel],
                    creator_fp,
                )
            except Exception:
                content = "[Encrypted message - decryption failed]"
        elif message.is_encrypted:
            content = "[Encrypted message - join channel with password]"

        if is_private and isinstance(content, str) and content.startswith(COVER_TRAFFIC_PREFIX):
            return

        await self._message_queue.put(
            {
                "id": message.id,
                "content": content,
                "channel": message.channel,
                "private": is_private,
                "sender_id": packet.sender_id_str,
                "sender_nickname": sender_nick,
            }
        )

    async def send_public_message(self, content: str):
        await super().send_public_message(content)

    async def send_private_message(self, content: str, target_peer_id: str, target_nickname: str):
        await super().send_private_message(content, target_peer_id, target_nickname)

    async def next_message(self) -> Dict[str, Any]:
        return await self._message_queue.get()

    async def run_bot(self) -> None:
        """Connect to the BitChat network and process events until stopped."""
        connected = await self.connect()
        await self.handshake()

        scanner_task = None
        if not connected or not self.client:
            scanner_task = asyncio.create_task(self.background_scanner())
        try:
            while self.running:
                await asyncio.sleep(0.1)
        finally:
            self.running = False
            if scanner_task:
                scanner_task.cancel()
                try:
                    await scanner_task
                except asyncio.CancelledError:
                    pass
            if self.client and self.client.is_connected:
                await self.client.disconnect()


__all__ = ["BitChatAPI", "BitchatBotAPI"]



"""Simple API wrapper around :class:`BitchatClient` for bot developers."""

import asyncio
from typing import Optional

from .client import BitchatClient


class BitChatAPI:
    """High level interface to interact with BitChat."""

    def __init__(self) -> None:
        self.client = BitchatClient()

    async def connect(self) -> bool:
        """Connect to the BitChat network."""
        connected = await self.client.connect()
        if connected:
            await self.client.handshake()
        return connected

    async def run(self) -> None:
        """Run the main event loop."""
        await self.client.run()

    async def send_message(self, text: str, peer_id: Optional[str] = None, nickname: Optional[str] = None) -> None:
        """Send a public or private message."""
        if peer_id:
            await self.client.send_private_message(text, peer_id, nickname or peer_id)
        else:
            await self.client.send_public_message(text)

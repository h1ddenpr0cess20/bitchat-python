import asyncio
from .client import BitchatClient

class BitchatBotAPI(BitchatClient):
    """Simplified API wrapper around :class:`BitchatClient` for chatbot usage."""
    def __init__(self, on_message=None):
        super().__init__()
        self.on_message = on_message

    async def display_message(self, message, packet, is_private):
        """Forward incoming messages to the callback instead of printing."""
        if callable(self.on_message):
            await self.on_message(message, packet, is_private)

    async def send_public_message(self, content: str):
        """Send a public or channel message without terminal output."""
        await super().send_public_message(content)

    async def send_private_message(self, content: str, target_peer_id: str, target_nickname: str):
        """Send a private message without terminal output."""
        await super().send_private_message(content, target_peer_id, target_nickname)

    async def run_bot(self):
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

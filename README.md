# BitChat Python

This project is a Python implementation of the BitChat decentralized, peer-to-peer, encrypted chat application over BLE.
It is based on the original [bitchat-terminal](https://github.com/ShilohEye/bitchat-terminal) client.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

Run the interactive terminal client:

```bash
python -m bitchat
```

## API

A small wrapper around `BitchatClient` is provided for bot authors.

```python
import asyncio
from bitchat.api import BitChatAPI

async def main():
    api = BitChatAPI()
    await api.connect()
    await api.send_message("Hello from a bot!")
    await api.run()

asyncio.run(main())
```

See the built in command help (`/help`) for a list of chat commands.

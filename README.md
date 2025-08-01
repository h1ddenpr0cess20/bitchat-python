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

A lightweight wrapper around ``BitchatClient`` called ``BitchatBotAPI`` is
included for building bots.  It exposes async helpers for sending messages and
consuming received ones.

```python
import asyncio
from bitchat.api import BitchatBotAPI

async def bot():
    api = BitchatBotAPI()
    # Run connection and scanning loop in background
    asyncio.create_task(api.run_bot())

    await api.send_public_message("Hello from a bot!")

    while True:
        msg = await api.next_message()
        print(f"{msg['sender_nickname']}: {msg['content']}")

asyncio.run(bot())
```

See the built in command help (`/help`) for a list of chat commands.

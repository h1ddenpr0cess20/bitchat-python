#!/usr/bin/env python3
"""Command-line entry point for BitChat."""
import asyncio
from bitchat.client import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[+] Exiting...")

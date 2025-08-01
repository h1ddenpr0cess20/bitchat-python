#!/usr/bin/env python3
"""Main entry point for BitChat package execution."""
import asyncio
from .client import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[+] Exiting...")

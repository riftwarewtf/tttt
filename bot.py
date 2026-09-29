#!/usr/bin/env python3
"""Start the bot.

    python bot.py

The token comes from DISCORD_TOKEN, either in the environment or in a .env
file next to this one (copy .env.example). Everything else has a default;
see README.md.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.client import run  # noqa: E402

if __name__ == "__main__":
    run()

#!/usr/bin/env python3
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.notifications import NtfyProvider  # noqa: E402


async def run(topic: str) -> None:
    await NtfyProvider().send_test(topic)
    print(f"Test notification sent to {topic}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("topic")
    asyncio.run(run(parser.parse_args().topic))

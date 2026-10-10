"""Compatibility entry point for the redesigned isolated UI checks using RETAIL_TEST_URL (default port 8785)."""
import asyncio
from smoke_redesign import main
if __name__ == "__main__": asyncio.run(main())

"""Run directly so Windows uses the subprocess-capable Proactor event loop."""
import asyncio
import argparse
import os
import uvicorn


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retail Desk local automation workspace")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", help="Optional isolated data directory")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["RETAIL_DATA"] = args.data_dir
    asyncio.run(uvicorn.Server(uvicorn.Config("retail.app:app", host="127.0.0.1", port=args.port, loop="asyncio")).serve())

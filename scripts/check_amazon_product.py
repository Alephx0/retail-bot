"""Read-only Amazon product diagnostic/canary. Never logs in, carts, or purchases by default."""
import asyncio
import argparse
import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patchright.async_api import async_playwright
from retail.amazon import Amazon, ChallengeDetected, BackoffRequired, AccessDenied


def load_state(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def shape_signature(product, selectors):
    """Track extraction/DOM shape, not volatile price or inventory values."""
    return {
        "title_present": bool(product.get("title")),
        "price_present": product.get("price") is not None,
        "seller_present": product.get("seller") not in ("", "Unknown", None),
        "offer_present": bool(product.get("offer_id")),
        "selectors": {key: bool(value) for key, value in selectors.items()},
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asin")
    parser.add_argument("--task", help="Use the saved session of this local task for a read-only check")
    parser.add_argument("--canary", action="store_true", help="Persist a low-frequency DOM/extraction shape baseline")
    parser.add_argument(
        "--canary-state",
        default=str(Path(__file__).resolve().parents[1] / "data" / "amazon-canary.json"),
        help="JSON state file used by --canary",
    )
    parser.add_argument(
        "--min-interval-seconds",
        type=int,
        default=900,
        help="Minimum interval between real-site canary requests (minimum 300 seconds)",
    )
    args = parser.parse_args()

    if args.canary and args.min_interval_seconds < 300:
        parser.error("--min-interval-seconds must be at least 300 for real-site canaries")

    options, region = {}, "US"
    if args.task:
        from retail.store import Store
        store = Store(Path(__file__).resolve().parents[1] / "data")
        try:
            task = store.get("tasks", args.task)
            account = store.get("accounts", task["account_id"])
            if account.get("session"):
                options["storage_state"] = account["session"]
            region = account["region"]
        finally:
            store.db.close()

    state_path = Path(args.canary_state)
    state = load_state(state_path) if args.canary else {}
    key = f"{region}:{args.asin.upper()}"
    previous = state.get(key, {}) if args.canary else {}
    now_ts = int(time.time())
    if args.canary and previous.get("attempted_at"):
        remaining = args.min_interval_seconds - (now_ts - int(previous["attempted_at"]))
        if remaining > 0:
            print(json.dumps({
                "status": "skipped",
                "reason": "canary_cooldown",
                "retry_after_seconds": remaining,
                "asin": args.asin.upper(),
                "region": region,
            }))
            return

    if args.canary:
        state[key] = {**previous, "attempted_at": now_ts}
        save_state(state_path, state)

    result = {"asin": args.asin.upper(), "region": region}
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True)
        try:
            context = await browser.new_context(**options)
            page = await context.new_page()
            adapter = Amazon(None)
            try:
                product = await adapter.inspect(page, {"asin": args.asin.upper()}, region)
                selectors = {
                    "product_title": await page.locator("#productTitle").count(),
                    "price_block": await page.locator("#corePrice_feature_div, #corePriceDisplay_desktop_feature_div").count(),
                    "merchant_info": await page.locator("#merchantInfoFeature_feature_div, #merchant-info, #tabular-buybox").count(),
                    "add_to_cart": await page.locator("#add-to-cart-button, input[name='submit.add-to-cart']").count(),
                    "quantity": await page.locator("select#quantity").count(),
                }
                signature = shape_signature(product, selectors)
                changed = bool(previous.get("signature")) and previous.get("signature") != signature
                result.update(
                    status="changed" if changed else "ok",
                    changed=changed,
                    signature=signature,
                    observed_at=int(time.time()),
                    product={k: v for k, v in product.items() if k not in ("offer_id", "image")},
                )
            except ChallengeDetected as exc:
                result.update(status="challenge", challenge_kind=exc.kind, message=str(exc))
            except BackoffRequired as exc:
                result.update(
                    status="rate_limited",
                    http_status=exc.status,
                    retry_after_seconds=exc.retry_after_seconds,
                    message=str(exc),
                )
            except AccessDenied as exc:
                result.update(status="access_denied", message=str(exc))
        finally:
            await browser.close()

    if args.canary:
        state[key] = {
            **state.get(key, {}),
            "attempted_at": now_ts,
            "observed_at": result.get("observed_at", now_ts),
            "status": result["status"],
            "signature": result.get("signature"),
        }
        save_state(state_path, state)

    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())

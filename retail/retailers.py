"""Capability registry. A listed retailer is not necessarily an implemented adapter."""
RETAILERS = {
    "amazon": {"name": "Amazon US", "domain": "www.amazon.com", "input": "ASIN / URL;max price;offer ID", "automation": True},
    "bestbuy": {"name": "Best Buy US", "domain": "www.bestbuy.com", "input": "SKU or product URL"},
    "nvidia": {"name": "NVIDIA", "domain": "marketplace.nvidia.com", "input": "Product URL"},
    "bhphoto": {"name": "B&H Photo", "domain": "www.bhphotovideo.com", "input": "Product URL"},
    "costco": {"name": "Costco US", "domain": "www.costco.com", "input": "Item ID or product URL"},
    "gamestop": {"name": "GameStop", "domain": "www.gamestop.com", "input": "Product ID or URL"},
    "newegg": {"name": "Newegg", "domain": "www.newegg.com", "input": "Item number or URL"},
    "pokemoncenter": {"name": "Pokémon Center", "domain": "www.pokemoncenter.com", "input": "Product ID or URL"},
    "walmart": {"name": "Walmart US", "domain": "www.walmart.com", "input": "Product ID or URL"},
    "samsclub": {"name": "Sam’s Club", "domain": "www.samsclub.com", "input": "Product ID or URL"},
    "target": {"name": "Target", "domain": "www.target.com", "input": "TCIN or product URL", "notes": "Target checkout and Shape integration planned"},
}


def retailer_id(value):
    if value not in RETAILERS:
        raise ValueError("Unknown retailer")
    return value


def catalog():
    return [{"id": key, "automation": False, "status": "planned", **value,
             **({"status": "browser automation"} if value.get("automation") else {})}
            for key, value in RETAILERS.items()]

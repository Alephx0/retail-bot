import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from retail.app import create_app
from retail.engine import Engine
from retail.models import Group, Task, eligible, inputs, proxy_config
from retail.store import Store


def test_inputs_and_caps():
    result = inputs("B012345678;19.99;offer123\nhttps://www.amazon.com/dp/B087654321?ref=test\nB011111111;opaqueOffer")
    assert result[0] == {"asin": "B012345678", "max_price": 19.99, "offer_id": "offer123"}
    assert result[1]["asin"] == "B087654321"
    assert result[2]["offer_id"] == "opaqueOffer"


@pytest.mark.parametrize("value", ["", "bad-asin", "https://example.com/dp/B012345678", "B012345678;-1", "B012345678;NaN", "B012345678;abc;offer", "B012345678;1;offer;extra"])
def test_invalid_inputs(value):
    with pytest.raises(ValueError):
        inputs(value)


def test_offer_seller_and_price_filters_fail_closed():
    group = Group(name="test", products="B012345678;30;offer").model_dump()
    item = inputs(group["products"])[0]
    product = {"available": True, "price": 29.99, "amazon_seller": True, "condition": "new", "offer_id": "offer"}
    assert eligible(product, item, group)
    for patch in [{"price": None}, {"price": 30.01}, {"amazon_seller": False}, {"condition": "used"}, {"offer_id": "different"}, {"available": False}]:
        assert not eligible({**product, **patch}, item, group)


def test_unknown_seller_may_cart_but_known_filter_failures_still_block():
    from retail.models import rejection_reasons
    group = Group(name='checkout', products='B012345678;30').model_dump()
    item = inputs(group['products'])[0]
    product = {'available': True, 'price': 20, 'seller': 'Unknown', 'amazon_seller': False, 'condition': 'new'}
    assert not eligible(product, item, group)
    assert eligible(product, item, group, defer_unknown_seller=True)
    assert not eligible({**product, 'seller': 'Other merchant'}, item, group, defer_unknown_seller=True)
    assert not eligible({**product, 'price': 31}, item, group, defer_unknown_seller=True)
    assert not eligible({**product, 'available': False}, item, group, defer_unknown_seller=True)
    assert 'Seller could not be read' in rejection_reasons(product, item, group)
    assert any('exceeds limit' in reason for reason in rejection_reasons({**product, 'price': 31}, item, group))


def test_deal_filters_are_combined():
    group = Group(name="deals", products="B012345678", mode="deals", only_freebies=False, min_discount=80, min_savings=70).model_dump()
    item = inputs(group["products"])[0]
    product = {"available": True, "price": 10, "original_price": 100, "amazon_seller": True, "condition": "new"}
    assert eligible(product, item, group)
    assert not eligible({**product, "original_price": None}, item, group)
    assert not eligible({**product, "price": 21}, item, group)
    group["only_freebies"] = True
    assert not eligible(product, item, group)
    assert eligible({**product, "price": 0}, item, group)


def test_proxy_password_can_contain_colon():
    assert proxy_config("proxy.example:8080:user:pass:word")["password"] == "pass:word"
    with pytest.raises(ValueError):
        proxy_config("proxy.example:99999")


def test_encrypted_persistence(tmp_path):
    store = Store(tmp_path)
    value = store.put("accounts", {"email": "sensitive@example.com", "session": {"cookies": ["secret-cookie"]}})
    assert b"secret-cookie" not in store.db.execute("SELECT payload FROM records").fetchone()[0]
    store.db.close()
    reopened = Store(tmp_path)
    assert reopened.get("accounts", value["id"])["email"] == "sensitive@example.com"
    reopened.db.close()


def test_api_security_validation_and_references(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/api/demo/load").status_code == 403
        client.headers["X-Retail-Client"] = "dashboard"
        assert client.post("/api/demo/load", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 400
        assert client.post("/api/groups", json={"name": "x", "products": "bad"}).status_code == 422
        group = client.post("/api/groups", json={"name": "valid", "products": "B012345678"}).json()
        assert client.post("/api/tasks", json={"group_id": group["id"], "simulation": False}).status_code == 422
        task = client.post("/api/tasks", json={"group_id": group["id"]}).json()
        assert client.delete(f"/api/groups/{group['id']}").status_code == 409
        assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
        assert client.delete(f"/api/groups/{group['id']}").status_code == 200


def test_secrets_redacted_and_preserved_on_edit(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.headers["X-Retail-Client"] = "dashboard"
        record = client.post("/api/accounts", json={"name": "main", "proxy": "host:80:user:secret"}).json()
        assert "proxy" not in record
        assert record["has_proxy"]
        client.put(f"/api/accounts/{record['id']}", json={"name": "changed"})
        assert client.app.state.store.get("accounts", record["id"])["proxy"] == "host:80:user:secret"
        assert "secret" not in client.get("/api/state").text


def test_naive_schedule_rejected():
    with pytest.raises(ValueError):
        Task(group_id="g", scheduled_at="2026-10-01T12:00:00")


def test_engine_simulation_schedule_stop_and_restart(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        group = store.put("groups", Group(name="test", products="B012345678", max_price=30).model_dump())
        task = store.put("tasks", {**Task(group_id=group["id"]).model_dump(mode="json"), "status": "scheduled", "scheduled_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
        engine = Engine(store)
        await engine.boot()
        for _ in range(60):
            if store.get("tasks", task["id"])["status"] == "completed":
                break
            await asyncio.sleep(.05)
        assert store.get("tasks", task["id"])["status"] == "completed"
        assert len(store.all("checkouts")) == 1
        assert store.all("checkouts")[0]["simulation"] is True
        await engine.start(task["id"])
        await asyncio.sleep(.01)
        await engine.stop(task["id"])
        assert task["id"] not in engine.jobs
        assert store.get("tasks", task["id"])["status"] == "stopped"
        await engine.start(task["id"])
        await engine.stop(task["id"])
        assert task["id"] not in engine.jobs, "Immediate cancellation must release the task slot"
        await engine.close()
        store.put("tasks", {**store.get("tasks", task["id"]), "status": "carting"})
        engine = Engine(store)
        await engine.boot()
        assert store.get("tasks", task["id"])["status"] == "stopped"
        await engine.close()
        store.db.close()
    asyncio.run(scenario())

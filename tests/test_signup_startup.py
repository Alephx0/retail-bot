import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from patchright.async_api import async_playwright, expect

from retail import account_creation as creation
from retail.app import create_app
from retail.proxy_pool import ProxyPool, ProxySelectionError
from retail.proxy_protocol import parse_proxy
from retail.proxy_testing import ProxyTesting
from retail.store import now
from test_account_care import fixture


def stale_pool(store):
    pool=store.put('proxies',{'name':'Signup connection','entries':'proxy.example:8080:user:private-secret'})
    parsed=parse_proxy(pool['entries'])
    observation={**parsed.public(),'status':'healthy','observed_at':'2026-01-01T00:00:00+00:00','latency_ms':3}
    store.put('proxy_endpoints',{**parsed.public(),'status':'healthy','history':[observation],
              'fresh_until':'2026-01-01T00:15:00+00:00'},'endpoint-'+parsed.fingerprint)
    store.put('proxy_health',{'status':'completed','results':[observation]},'health-'+pool['id'])
    return pool


@pytest.mark.parametrize('outcome',['healthy','authentication_failed','unreachable'])
def test_stale_proxy_rechecked_before_signup_launch(tmp_path,monkeypatch,outcome):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        pool=stale_pool(store)
        account=store.put('accounts',{**account,'proxy':'','proxy_list_id':pool['id']})
        health=engine.proxy_health=ProxyTesting(store)
        async def measured(line,target,**kwargs):
            assert line==pool['entries'] and target=='https://api.ipify.org/?format=json'
            return {**parse_proxy(line).public(),'status':outcome,'latency_ms':2,'observed_at':now()}
        probe=AsyncMock(side_effect=measured)
        monkeypatch.setattr('retail.proxy_testing.probe',probe)
        engine.amazon.context=AsyncMock(side_effect=RuntimeError('stop before external navigation private-secret'))
        with pytest.raises(ValueError):await creation.create_account(engine,account)
        assert probe.await_count==1 and engine.browser_slots._value==1
        if outcome=='healthy':
            assert engine.amazon.context.await_count==1
            assert engine.amazon.context.call_args.kwargs['proxy']==pool['entries']
            assert await health.ready_route(pool['id'],account['id'])==pool['entries']
            assert probe.await_count==1
        else:
            engine.amazon.context.assert_not_awaited()
            assert not store.all('network_routes')
        record=creation.snapshot(engine)[0]
        assert record['status']=='startup_failed' and not record['submitted']
        assert 'private-secret' not in json.dumps(record)
        assert store.get('accounts',other['id'])==other
        await health.close();await engine.close();store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['cancel','shared','fresh','bound','missing','incompatible'])
def test_proxy_startup_lifecycle(tmp_path,monkeypatch,mode):
    async def scenario():
        store,engine,(account,_)=fixture(tmp_path);pool=stale_pool(store)
        health=engine.proxy_health=ProxyTesting(store)
        entered=asyncio.Event();release=asyncio.Event()
        async def measured(line,*args,**kw):
            entered.set();await release.wait()
            return {**parse_proxy(line).public(),'status':'healthy','latency_ms':2,'observed_at':now()}
        probe=AsyncMock(side_effect=measured);monkeypatch.setattr('retail.proxy_testing.probe',probe)
        if mode in ('fresh','bound'):
            item=store.all('proxy_endpoints')[0]
            store.put('proxy_endpoints',{**item,'fresh_until':'2999-01-01T00:00:00+00:00'})
            if mode=='bound':
                chosen=ProxyPool(store).choose(pool['id'],account['id'])
                store.put('proxy_endpoints',{**item,'status':'unreachable'})
            assert await health.ready_route(pool['id'],account['id'])==pool['entries']
            probe.assert_not_awaited()
        elif mode in ('missing','incompatible'):
            if mode=='missing':store.delete('proxies',pool['id'])
            else:
                store.put('proxies',{**pool,'entries':'socks5://user:password@proxy.example:1080'})
                store.delete('proxy_health','health-'+pool['id'])
            with pytest.raises(ProxySelectionError):await health.ready_route(pool['id'],account['id'])
            probe.assert_not_awaited()
        elif mode=='shared':
            first=asyncio.create_task(health.ready_route(pool['id'],'first'))
            await entered.wait()
            second=asyncio.create_task(health.ready_route(pool['id'],'second'))
            await asyncio.sleep(0);release.set()
            assert await first==await second==pool['entries']
            assert probe.await_count==1 and not health.jobs
        else:
            account=store.put('accounts',{**account,'proxy':'','proxy_list_id':pool['id']})
            engine.amazon.context=AsyncMock()
            task=asyncio.create_task(creation.create_account(engine,account))
            await entered.wait();task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert engine.browser_slots._value==1 and not engine.amazon.logins
            engine.amazon.context.assert_not_awaited()
            # A shared bounded connectivity check may finish independently;
            # cancellation cannot later open a signup browser.
            release.set();await asyncio.gather(*health.jobs.values())
            engine.amazon.context.assert_not_awaited()
        await health.close();await engine.close();store.db.close()
    asyncio.run(scenario())


def test_startup_failure_persists_in_api_and_browser(tmp_path):
    with TestClient(create_app(tmp_path),headers={'X-Retail-Client':'dashboard'}) as client:
        engine=client.app.state.engine
        account=client.post('/api/accounts',json={'name':'Launch fixture','email':'fixture@example.test','password':'fixture-secret'}).json()
        engine.amazon.context=AsyncMock(side_effect=ProxySelectionError('proxy_unreachable','The assigned proxy could not connect. Check it in Proxies.'))
        response=client.post(f"/api/accounts/{account['id']}/register-auto")
        assert response.status_code==409 and 'assigned proxy' in response.text
        record=client.get('/api/state').json()['account_creation'][0]
        assert record['status']=='startup_failed' and not record['submitted']
        async def scenario():
            async with async_playwright() as driver:
                browser=await driver.chromium.launch(headless=True)
                page=await browser.new_page()
                from urllib.parse import urlsplit
                async def route(route):
                    response=client.request(route.request.method,urlsplit(route.request.url).path,content=route.request.post_data)
                    await route.fulfill(status=response.status_code,body=response.content,headers={'content-type':response.headers.get('content-type','text/plain')})
                await page.route('**/*',route)
                await page.goto('http://127.0.0.1/')
                await page.locator('[data-view=accounts]').click()
                await page.get_by_role('tab',name='Account creation',exact=True).click()
                output=Path('artifacts/signup-startup');output.mkdir(parents=True,exist_ok=True)
                for width in (1440,390):
                    await page.set_viewport_size({'width':width,'height':1000})
                    await expect(page.locator('.creation-table')).to_contain_text('Signup could not start')
                    await expect(page.locator('.creation-table')).to_contain_text('The assigned proxy could not connect.')
                    await expect(page.get_by_role('button',name='Start signup',exact=True)).to_be_enabled()
                    await page.screenshot(path=str(output/f'failure-{width}.png'),full_page=True)
                await browser.close()
        asyncio.run(scenario())


@pytest.mark.parametrize('error',[TimeoutError(),ValueError('private launch arguments: secret-token')])
def test_resume_startup_failure_preserves_submission_and_stage(tmp_path,error):
    async def scenario():
        store,engine,(account,_)=fixture(tmp_path)
        key='creation-'+account['id']
        store.put('account_creation',{'account_id':account['id'],'submitted':True,'claim_submitted':True,
                  'status':'review','message':'Interrupted'},key)
        async def context(*args,**kw):
            kw['progress']('Loading account browser extensions')
            raise error
        engine.amazon.context=AsyncMock(side_effect=context)
        with pytest.raises(ValueError,match='Loading account browser extensions'):
            await creation.create_account(engine,account,resume=True)
        record=creation.snapshot(engine)[0]
        assert record['status']=='startup_failed' and record['submitted'] and record['claim_submitted']
        assert 'secret-token' not in json.dumps(record)
        with pytest.raises(ValueError,match='already submitted'):await creation.create_account(engine,account)
        assert engine.browser_slots._value==1
        await engine.close();store.db.close()
    asyncio.run(scenario())

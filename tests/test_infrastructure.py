import asyncio
from email.message import EmailMessage

import httpx
import pytest
from fastapi.testclient import TestClient

from retail.app import create_app
from retail.identity import extract_otp, totp, IdentityService
from retail.models import Account, Profile, Solver
from retail.services import SolverService, ProxyHealth, proxy_url, classify_browser_probe
from retail.store import Store


@pytest.mark.parametrize(('stamp','expected'), [(59,'94287082'), (1111111109,'07081804'), (1234567890,'89005924')])
def test_totp_rfc6238_vectors(stamp, expected):
    assert totp('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ', stamp, 8) == expected


def message(sender='account-update@amazon.com', recipient='buyer@example.com', body='Your verification code is 123456.'):
    msg = EmailMessage()
    msg['From'], msg['To'] = sender, recipient
    msg.set_content(body)
    return msg.as_bytes()


def test_otp_scope_and_code_context():
    assert extract_otp(message(), 'buyer@example.com', ('amazon.com',)) == '123456'
    for raw in [message(sender='attacker@example.com'), message(recipient='someoneelse@example.com'), message(body='Your order is 123456.')]:
        assert extract_otp(raw, 'buyer@example.com', ('amazon.com',)) is None
    assert extract_otp(message(sender='sender@evilamazon.com'), 'buyer@example.com', ('amazon.com',)) is None


def test_imap_replay_protection(monkeypatch, tmp_path):
    calls = []
    def fake_read(mailbox, recipient, since, used):
        calls.append((recipient, used.copy()))
        return None if 'uid1' in used else {'code': '123456', 'token': 'uid1'}
    monkeypatch.setattr('retail.identity.read_code', fake_read)
    async def scenario():
        store = Store(tmp_path)
        box = store.put('mailboxes', {'name': 'mail', 'max_age_seconds': 300})
        service = IdentityService(store)
        account = {'mailbox_id': box['id'], 'email': 'buyer@example.com'}
        assert (await service.code(account))['code'] == '123456'
        with pytest.raises(ValueError, match='No fresh'):
            await service.code(account)
        assert '123456' not in str(store.all('otp_receipts'))
        store.db.close()
    asyncio.run(scenario())


def test_secrets_and_references_for_new_collections(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.headers['X-Retail-Client'] = 'dashboard'
        mailbox = client.post('/api/mailboxes', json={'name':'mail','host':'imap.example.com','username':'user','password':'MAIL_SECRET'}).json()
        solver = client.post('/api/solvers', json={'name':'solver','provider':'capmonster','api_key':'SOLVER_SECRET'}).json()
        account = client.post('/api/accounts', json={'name':'account','password':'ACCOUNT_SECRET','totp_secret':'JBSWY3DPEHPK3PXP','mailbox_id':mailbox['id'],'solver_id':solver['id']}).json()
        profile = client.post('/api/profiles', json={'name':'profile','card_number':'4111111111111111'}).json()
        assert profile['card_last4'] == '1111'
        state = client.get('/api/state').text
        for value in ['MAIL_SECRET','SOLVER_SECRET','ACCOUNT_SECRET','JBSWY3DPEHPK3PXP','4111111111111111']:
            assert value not in state
        assert client.delete('/api/mailboxes/'+mailbox['id']).status_code == 409
        assert client.delete('/api/solvers/'+solver['id']).status_code == 409
        assert client.put('/api/accounts/'+account['id'], json={'name':'renamed'}).status_code == 200
        assert client.app.state.store.get('accounts',account['id'])['password'] == 'ACCOUNT_SECRET'
        assert len(client.get('/api/state').json()['retailers']) == 11


def test_registry_input_lists_and_planned_module(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.headers['X-Retail-Client'] = 'dashboard'
        product_list = client.post('/api/input_lists',json={'name':'sku list','products':'B012345678;25'}).json()
        group = client.post('/api/groups', json={'name':'linked','products':'','input_list_id':product_list['id']}).json()
        assert 'id' in group
        assert client.delete('/api/input_lists/'+product_list['id']).status_code == 409
        response = client.post('/api/groups',json={'name':'wrong site','retailer':'target','products':'12345678','input_list_id':product_list['id']})
        assert response.status_code == 422
        target = client.post('/api/groups',json={'name':'Target setup','retailer':'target','products':'12345678'}).json()
        assert 'id' in target
        assert client.post('/api/tasks',json={'group_id':target['id'],'simulation':False}).status_code == 422


def test_import_validates_entire_batch_before_writing(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.headers['X-Retail-Client'] = 'dashboard'
        response = client.post('/api/import/accounts',json=[{'name':'good'}, {'name':''}])
        assert response.status_code == 422
        assert client.get('/api/state').json()['accounts'] == []


def test_live_task_batches_validate_all_accounts_and_allow_login_credentials(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.headers['X-Retail-Client'] = 'dashboard'
        group = client.post('/api/groups',json={'name':'drop','products':'B012345678'}).json()
        first = client.post('/api/accounts',json={'name':'first','group':'Drop','password':'secret'}).json()
        second = client.post('/api/accounts',json={'name':'second','group':'Drop'}).json()
        settings = {'group_id':group['id'],'account_group_scope':'Drop','simulation':False}
        assert client.post('/api/task-batches/create',json=settings).status_code == 422
        assert client.get('/api/state').json()['tasks'] == []
        client.put('/api/accounts/'+second['id'],json={'password':'secret'})
        response = client.post('/api/task-batches/create',json=settings)
        assert response.status_code == 200
        tasks = response.json()['created']
        assert len(tasks) == 2
        assert {t['account_id'] for t in tasks} == {first['id'],second['id']}
        assert len({t['id'] for t in tasks}) == 2
        scheduled = client.post('/api/tasks',json={'group_id':group['id'],'scheduled_at':'2030-01-01T12:00:00Z'}).json()
        assert client.post('/api/control/stop-all').json()['stopped'] == 1
        assert client.app.state.store.get('tasks',scheduled['id'])['status'] == 'stopped'


def test_invalid_secrets_and_card():
    with pytest.raises(ValueError):
        Account(name='x',totp_secret='invalid')
    with pytest.raises(ValueError):
        Profile(name='x',card_number='4111111111111112')
    with pytest.raises(ValueError):
        Solver(name='x',provider='capmonster')
    with pytest.raises(ValueError):
        Solver(name='x',provider='flaresolverr',endpoint='http://external.example.com')
    assert proxy_url('host:80:a@b:p:a/s') == 'http://a%40b:p%3Aa%2Fs@host:80'


def test_solver_provider_adapter(monkeypatch):
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path == '/getBalance':
            return httpx.Response(200,json={'errorId':0,'balance':10})
        return httpx.Response(200,json={'errorId':0,'status':'ready','solution':{'text':'abc123'}})
    original = httpx.AsyncClient
    monkeypatch.setattr('retail.services.httpx.AsyncClient',lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    async def scenario():
        provider=Solver(name='test',provider='capmonster',api_key='key').model_dump()
        service=SolverService()
        assert (await service.health(provider))['balance'] == 10
        assert await service.solve_image(provider,b'image bytes') == 'abc123'
        assert requests[0].url.host == 'api.capmonster.cloud'
    asyncio.run(scenario())


def test_proxy_health_results_redact_credentials(monkeypatch,tmp_path):
    original = httpx.AsyncClient
    monkeypatch.setattr('retail.services.httpx.AsyncClient',lambda **kwargs: original(transport=httpx.MockTransport(lambda r:httpx.Response(403))))
    async def scenario():
        store=Store(tmp_path)
        record=store.put('proxies',{'name':'pool','entries':'example.com:80:user:SECRET'})
        health=ProxyHealth(store)
        await health.start(record,'amazon')
        await asyncio.gather(*list(health.jobs.values()))
        results=store.all('proxy_health')
        assert results[0]['kind'] == 'connectivity'
        assert results[0]['results'][0]['status'] == 'reachable'
        assert results[0]['results'][0]['http_status'] == 403
        assert results[0]['results'][0]['transport'] == 'httpx'
        assert 'SECRET' not in str(results)
        assert 'latency_ms' in results[0]['results'][0]
        store.db.close()
    asyncio.run(scenario())


def test_browser_proxy_probe_classification_is_non_circumventing():
    assert classify_browser_probe(200, '<body>Product page</body>') == 'ok'
    assert classify_browser_probe(429, '<body></body>') == 'rate_limited'
    assert classify_browser_probe(403, '<body></body>') == 'access_denied'
    assert classify_browser_probe(200, '<body>Robot Check</body>') == 'challenge'
    assert classify_browser_probe(None, '') == 'navigation_error'

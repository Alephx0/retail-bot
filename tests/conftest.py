import pytest


@pytest.fixture(autouse=True)
def owned_mcp_test_connections(monkeypatch, request):
    if request.module.__name__ not in {"test_browser_agent","test_browser_recovery"}:return
    import socket
    from patchright.async_api import BrowserType, Browser
    launch=BrowserType.launch
    create=Browser.new_context
    create_page=Browser.new_page
    async def owned_launch(self,*args,**kwargs):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        kwargs['args']=[*kwargs.get('args',[]),f'--remote-debugging-port={port}']
        browser=await launch(self,*args,**kwargs)
        browser._fixture_recovery_endpoint=f'http://127.0.0.1:{port}'
        return browser
    async def owned_context(self,*args,**kwargs):
        context=await create(self,*args,**kwargs)
        context._retail_recovery_endpoint=getattr(self,'_fixture_recovery_endpoint','')
        return context
    async def owned_page(self,*args,**kwargs):
        page=await create_page(self,*args,**kwargs)
        page.context._retail_recovery_endpoint=getattr(self,'_fixture_recovery_endpoint','')
        return page
    monkeypatch.setattr(Browser,'new_page',owned_page)
    monkeypatch.setattr(BrowserType,'launch',owned_launch)
    monkeypatch.setattr(Browser,'new_context',owned_context)

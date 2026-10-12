"""Task-owned Chrome DevTools MCP transport. No model can select a page or run JS."""
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .browser_mcp import BrowserTools
from .interactions import InteractionError

ENTRY=Path(__file__).resolve().parents[1]/'node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js'


class ScopedMCP:
    def __init__(self,page):
        self.page=page;self.context=page.context;self.url=page.url
        self.owner=getattr(self.context,'_retail_task_id','')
        self.stack=AsyncExitStack();self.session=None;self.page_id=None;self.pages=[]

    def guard(self, *, navigation=False):
        if (self.page.is_closed() or self.page.context is not self.context
            or getattr(self.context,'_retail_task_id','')!=self.owner
            or not navigation and self.page.url!=self.url):
            raise InteractionError('Recovery lost its task page; no further input was sent')

    async def __aenter__(self):
        self.guard()
        if getattr(self.context,'_retail_mcp_active',False):
            raise InteractionError('This browser already has a recovery owner')
        endpoint=getattr(self.context,'_retail_recovery_endpoint','')
        parsed=urlsplit(endpoint)
        if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.path not in ('','/'):
            raise InteractionError('This task has no isolated recovery connection; restart it with AI recovery enabled')
        if not ENTRY.is_file():raise InteractionError('Install the pinned Chrome DevTools MCP dependency before enabling recovery')
        self.context._retail_mcp_active=True
        try:
            environment={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','LOCALAPPDATA'}}
            environment.update(CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS='1',CHROME_DEVTOOLS_MCP_DISABLE_UPDATE_CHECK='1')
            params=StdioServerParameters(command='node',args=[str(ENTRY),'--browser-url='+endpoint,
                '--experimentalStructuredContent','--no-usage-statistics','--no-performance-crux','--no-javascript-evaluation',
                '--no-category-network','--no-category-performance','--no-source-maps'],env=environment)
            errors=self.stack.enter_context(open(os.devnull,'w'))
            reader,writer=await self.stack.enter_async_context(stdio_client(params,errlog=errors))
            self.session=await self.stack.enter_async_context(ClientSession(reader,writer))
            await self.session.initialize()
            await self.call('list_pages',{})
            # The text format includes the page title and truncates it. Exact
            # identity must come from the pinned server's structured page list.
            matches=[entry['id'] for entry in self.pages
                     if isinstance(entry,dict) and entry.get('url')==self.url
                     and isinstance(entry.get('id'),int) and not isinstance(entry.get('id'),bool)]
            if len(matches)!=1:raise InteractionError('Recovery page is missing or ambiguous')
            self.page_id=matches[0]
            await self.call('select_page',{'pageId':self.page_id})
            self.guard()
            return self
        except BaseException:
            await self.stack.aclose();self.context._retail_mcp_active=False
            raise

    async def __aexit__(self,*args):
        try:await self.stack.aclose()
        finally:self.context._retail_mcp_active=False

    async def call(self,name,arguments):
        if name not in {'list_pages','select_page','take_snapshot','click'}:
            raise InteractionError('Recovery tool is outside its capability')
        self.guard()
        result=await self.session.call_tool(name,arguments)
        if result.isError:raise InteractionError('Chrome DevTools could not complete this recovery step')
        if name=='list_pages':
            structured=getattr(result,'structuredContent',None) or {}
            self.pages=structured.get('pages',[]) if isinstance(structured,dict) else []
            if not isinstance(self.pages,list):raise InteractionError('Chrome DevTools returned invalid page identities')
        return '\n'.join(item.text for item in result.content if item.type=='text')

    async def controls(self,pattern):
        raw=await self.call('take_snapshot',{'pageId':self.page_id,'verbose':False})
        if len(raw)>250000:raise InteractionError('Recovery snapshot exceeds its evidence budget')
        candidates=[]
        for line in raw.splitlines():
            match=re.match(r'^\s*uid=(\d+_\d+) (button|link) ("(?:[^"\\]|\\.)*")',line)
            if not match:continue
            label=json.loads(match[3])
            if len(label)<=100 and re.fullmatch(pattern,label,re.I) and 'disabled' not in line[len(match[0]):]:
                candidates.append({'uid':match[1],'role':match[2],'label':label})
            if len(candidates)>40:raise InteractionError('Too many recovery candidates')
        self.guard()
        return candidates

    async def click(self,uid):
        await self.call('click',{'pageId':self.page_id,'uid':uid,'includeSnapshot':False})
        self.guard(navigation=True)


class MCPBrowserTools(BrowserTools):
    def __init__(self,page,action,domains,policies,scope):
        super().__init__(page,action,domains,policies)
        self.scope=scope;self.uids={}

    async def observe_controls(self):
        # Raw snapshots remain inside the local MCP process/client. Only policy-matched
        # labels and opaque references enter the provider conversation.
        candidates=await self.scope.controls(self.policies[self.action])
        observed=await super().observe_controls()
        self.uids={}
        for control in observed['controls']:
            matches=[c for c in candidates if c['label']==control['label']]
            if len(matches)==1:self.uids[control['ref']]=matches[0]['uid']
        self.evidence=[c for c in observed['controls'] if c['ref'] in self.uids]
        return {**observed,'controls':self.evidence,'transport':'chrome-devtools-mcp'}

    async def click_validated(self,ref):
        if not (await self.validate_control(ref)).get('validated'):
            raise InteractionError('Recovery control changed before dispatch')
        # Re-observe the AX UID immediately before dispatch; a stale UID is never reused.
        label=next(c['label'] for c in self.evidence if c['ref']==ref)
        candidates=await self.scope.controls(self.policies[self.action])
        matches=[c for c in candidates if c['label']==label]
        if len(matches)!=1:raise InteractionError('Recovery action became ambiguous')
        await self.scope.click(matches[0]['uid'])

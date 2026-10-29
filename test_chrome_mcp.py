"""Protocol tests with a local fake stdio MCP server; no browser or external API."""
import json
import sys
import threading
import time
import unittest
from unittest.mock import patch
import chrome_mcp as m
import test_orbita as base

FAKE_SERVER=r'''
import sys,json,time
initialized=False
for line in sys.stdin:
    data=json.loads(line)
    method=data.get('method');ident=data.get('id')
    if method=='notifications/initialized':initialized=True;continue
    if ident is None:continue
    if method=='initialize':result={'protocolVersion':'2025-06-18','serverInfo':{'name':'fake-chrome','version':'test'},'capabilities':{'tools':{}}}
    elif method=='tools/list':
        result={'tools':[{'name':'list_pages','description':'List pages','inputSchema':{'type':'object','properties':{}}},{'name':'take_snapshot','description':'Snapshot','inputSchema':{'type':'object','properties':{'pageId':{'type':'number'},'filePath':{'type':'string'}},'required':['pageId']}},{'name':'evaluate_script','description':'Not exposed','inputSchema':{'type':'object','properties':{}}}]}
    elif method=='tools/call':
        if not initialized:
            print(json.dumps({'jsonrpc':'2.0','id':ident,'error':{'code':-1,'message':'not initialized'}}),flush=True);continue
        result={'content':[{'type':'text','text':'1: https://example.com [selected]' if data['params']['name']=='list_pages' else 'uid=1_0 heading Example'}]}
    elif method=='never_reply':continue
    else:result={}
    print(json.dumps({'jsonrpc':'2.0','id':ident,'result':result}),flush=True)
'''

class MCPTests(unittest.TestCase):
    def client(self):return m.StdioClient([sys.executable,'-u','-c',FAKE_SERVER])
    def test_stdio_initialization_and_tool_call(self):
        c=self.client()
        try:
            self.assertEqual(c.initialize()['serverInfo']['name'],'fake-chrome')
            tools=c.request('tools/list')['tools'];self.assertEqual(len(tools),3)
            result=c.request('tools/call',{'name':'list_pages','arguments':{}})
            self.assertIn('https://example.com',m.plain_text(result))
        finally:c.close()
        self.assertIsNotNone(c.process.poll())
    def test_timeout_and_cancellation(self):
        c=self.client()
        try:
            c.initialize()
            with self.assertRaises(m.MCPError):c.request('never_reply',timeout=.05)
            event=threading.Event();event.set()
            with self.assertRaises(m.MCPError):c.request('never_reply',timeout=2,cancel=event)
            self.assertFalse(c.pending)
        finally:c.close()
    def test_manager_is_connected_only_after_browser_handshake(self):
        manager=m.ChromeManager()
        with patch.object(m,'chrome_command',return_value=[sys.executable,'-u','-c',FAKE_SERVER]):
            manager.connect()
            for _ in range(100):
                if manager.status()['state']!='connecting':break
                threading.Event().wait(.01)
            self.assertEqual(manager.status()['state'],'connected')
            defs=manager.definitions();names={t['function']['name'] for t in defs}
            self.assertEqual(names,{'chrome_list_pages','chrome_take_snapshot'})
            schema=next(t['function']['parameters'] for t in defs if t['function']['name']=='chrome_take_snapshot')
            self.assertNotIn('filePath',schema['properties'])
            self.assertIn('uid=1_0',m.plain_text(manager.call('take_snapshot',{'pageId':1})))
            with self.assertRaises(m.MCPError):manager.call('evaluate_script',{})
            with self.assertRaises(m.MCPError):manager.call('take_snapshot',{'pageId':1,'filePath':'/tmp/x'})
            manager.disconnect();self.assertFalse(manager.status()['connected'])
    def test_missing_node_is_visible_error(self):
        with patch.object(m.shutil,'which',return_value=None):
            with self.assertRaisesRegex(m.MCPError,'Node.js'):m.chrome_command()
    def test_browser_actions_are_serialized(self):
        manager=m.ChromeManager();manager.state='connected';manager.tools={'list_pages':{}}
        active=[0];maximum=[0]
        class Client:
            def request(self,*a,**k):
                active[0]+=1;maximum[0]=max(maximum[0],active[0]);threading.Event().wait(.02);active[0]-=1
                return {'content':[{'type':'text','text':'pages'}]}
        manager.client=Client()
        threads=[threading.Thread(target=manager.call,args=('list_pages',{})) for _ in range(4)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(maximum[0],1)
    def test_browser_permissions_and_http_bypass(self):
        s=base.s
        job={'config':{**s.DEFAULT,'workspace':base.TEMP.name},'permissions':{'browser':False,'web':True,'files':True},'cancel':threading.Event(),'artifacts':[]}
        with self.assertRaisesRegex(ValueError,'Chrome выключен'):s.execute_tool('chrome_list_pages',{},job)
        for name,args in [('web_search',{'query':'example'}),('fetch_page',{'url':'https://example.com'})]:
            with self.assertRaisesRegex(ValueError,'только'):s.execute_tool(name,args,job)
        job['permissions']['browser']=True
        with patch.object(s.CHROME,'call',return_value={'content':[{'type':'text','text':'pages'}]}) as call:
            result=s.execute_tool('chrome_list_pages',{},job)
            self.assertEqual(m.plain_text(result),'pages');call.assert_called_once()
    def test_start_requires_connected_browser(self):
        s=base.s
        with patch.dict(s.CONFIG,{'api_key':'test-only'}):
            with patch.object(s.CHROME,'status',return_value={'connected':False}):
                with self.assertRaisesRegex(ValueError,'подключите Chrome'):s.start_job({'prompt':'test','permissions':{'browser':True}})
    def test_browser_team_uses_one_role_at_a_time(self):
        s=base.s
        job={'config':s.CONFIG,'permissions':{'browser':True},'prompt':'Research example','mode':'team','selected':[],'events':[],'artifacts':[],'calls':0,'tokens':0,'cancel':threading.Event(),'status':'running'}
        order=[]
        def fake(role,prompt,job,tools=True):order.append(role);return 'ГОТОВО' if role=='reviewer' else 'Result'
        with patch.object(s,'run_agent',fake):s.run_job(job)
        self.assertEqual(order,['coordinator','researcher','analyst','writer','reviewer']);self.assertEqual(job['status'],'completed')

if __name__=='__main__':unittest.main()

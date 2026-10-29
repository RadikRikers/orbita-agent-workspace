import importlib.util, json, os, tempfile, threading, unittest, urllib.request, urllib.error, zipfile
from pathlib import Path
from unittest.mock import patch

TEMP=tempfile.TemporaryDirectory()
os.environ['ORBITA_DATA_DIR']=str(Path(TEMP.name)/'config')
spec=importlib.util.spec_from_file_location('orbita',Path(__file__).resolve().parent/'server.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
class OrbitaTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(dir=TEMP.name));s.CONFIG={**s.DEFAULT,'workspace':str(self.root),'models':dict(s.DEFAULT['models'])};s.JOBS.clear()
        (self.root/'table.csv').write_text('name;views;likes\nA;100;3\nB;200;4\nA;100;3\n',encoding='utf-8')
    def test_csv_exact(self):
        stats=s.csv_stats(self.root/'table.csv');self.assertEqual(stats['rows'],3);self.assertEqual(stats['duplicates'],1);self.assertEqual(stats['columns'][1]['sum'],400);self.assertEqual(stats['columns'][2]['sum'],10)
    def test_scope_and_symlinks(self):
        for name in ['../secret.txt','.env','a/../../b','.ssh/id_rsa']:
            with self.assertRaises(ValueError):s.safe_path(name)
        outside=Path(TEMP.name)/'secret.txt';outside.write_text('private')
        (self.root/'link.txt').symlink_to(outside)
        with self.assertRaises(ValueError):s.safe_path('link.txt')
        self.assertFalse(any(i['name']=='link.txt' for i in s.list_files()))
    def test_docx_xlsx(self):
        with zipfile.ZipFile(self.root/'a.docx','w') as z:z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Тестовый документ</w:t></w:r></w:p></w:body></w:document>')
        self.assertIn('Тестовый документ',s.extract(self.root/'a.docx'))
        with zipfile.ZipFile(self.root/'a.xlsx','w') as z:z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Имя</t></is></c><c r="B1"><v>42</v></c></row></sheetData></worksheet>')
        self.assertIn('B1=42',s.extract(self.root/'a.xlsx'));self.assertIn('A1=Имя',s.extract(self.root/'a.xlsx'))
    def test_permissions_and_no_overwrite(self):
        job={'config':s.CONFIG,'cancel':threading.Event(),'permissions':{'files':False,'web':False,'write':False},'artifacts':[]}
        for name,args in [('read_file',{'path':'table.csv'}),('web_search',{'query':'test'}),('write_output',{'filename':'out.md','content':'x'})]:
            with self.assertRaises(ValueError):s.execute_tool(name,args,job)
        job['permissions']['write']=True
        first=s.execute_tool('write_output',{'filename':'out.md','content':'first'},job)
        second=s.execute_tool('write_output',{'filename':'out.md','content':'second'},job)
        self.assertNotEqual(first['created'],second['created']);self.assertEqual((self.root/first['created']).read_text(),'first')
        self.assertEqual(s.extract(self.root/'table.csv').splitlines()[1],'A;100;3')
    def test_private_urls(self):
        for url in ['file:///etc/passwd','http://127.0.0.1/x','http://localhost/x','http://169.254.169.254/latest/','http://10.0.0.1/','https://example.com:8765/','https://user:password@example.com']:
            with self.assertRaises(ValueError):s.validate_url(url)
    def test_team_pipeline_and_tools(self):
        s.CONFIG['api_key']='test-only';s.CONFIG['max_tokens']=500
        captured=[]
        def fake_hubris(path,config,payload):
            prompt=payload['messages'][0]['content'];captured.append(prompt)
            if prompt.startswith('Проверь'):text='ГОТОВО. Исходные сведения сверены.'
            elif prompt.startswith('Подготовь'):text='Итоговый результат'
            else:text='Материалы этапа'
            return {'choices':[{'message':{'role':'assistant','content':text}}],'usage':{'total_tokens':10}}
        with patch.object(s,'hubris',fake_hubris):
            job=s.start_job({'prompt':'Проанализируй таблицу','mode':'team','permissions':{'files':True}})
            thread_event=threading.Event()
            for _ in range(100):
                snap=s.snapshot(s.JOBS[job['id']])
                if snap['status']!='running':break
                thread_event.wait(.01)
        self.assertEqual(snap['status'],'completed');self.assertEqual(snap['calls'],5);self.assertEqual(snap['tokens'],50)
        self.assertEqual(len([e for e in snap['events'] if e['kind']=='result']),5)
        self.assertNotIn('api_key',json.dumps(snap));self.assertNotIn('test-only',json.dumps(snap))
    def test_tool_roundtrip_and_call_cap(self):
        s.CONFIG['api_key']='test-only'
        counter=[0]
        def fake(path,config,payload):
            counter[0]+=1
            if counter[0]==1:return {'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'c1','type':'function','function':{'name':'analyze_csv','arguments':'{"path":"table.csv"}'}}]}}],'usage':{'total_tokens':5}}
            self.assertEqual(payload['messages'][-1]['role'],'tool');self.assertIn('400',payload['messages'][-1]['content'])
            return {'choices':[{'message':{'role':'assistant','content':'Сумма 400'}}],'usage':{'total_tokens':8}}
        job={'config':s.CONFIG,'cancel':threading.Event(),'permissions':{'files':True,'web':False,'write':False},'artifacts':[],'events':[],'calls':0,'tokens':0,'cost_kopecks':0,'cost_reported':False}
        with patch.object(s,'hubris',fake):self.assertEqual(s.run_agent('analyst','Проверь таблицу',job),'Сумма 400')
        self.assertEqual(job['calls'],2);self.assertEqual(job['tokens'],13)
        job['calls']=s.CONFIG['max_calls']
        with self.assertRaises(ValueError):s.run_agent('analyst','test',job)
        job['cancel'].set()
        with self.assertRaises(s.Cancelled):s.run_agent('analyst','test',job)
    def test_http_auth_and_public_config(self):
        server=s.ThreadingHTTPServer(('127.0.0.1',0),s.Handler);port=server.server_port;host='127.0.0.1:'+str(port);s.ALLOWED_HOSTS.add(host)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        def req(path,headers=None):return urllib.request.urlopen(urllib.request.Request('http://'+host+path,headers=headers or {}))
        try:
            with req('/') as r:
                page=r.read().decode();self.assertIn('window.ORBITA_LOCAL=',page);self.assertIn(s.SESSION,page)
            with self.assertRaises(urllib.error.HTTPError) as ctx:req('/api/status')
            self.assertEqual(ctx.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError):req('/api/files',{'X-Orbita-Token':s.SESSION,'Origin':'https://evil.example'})
            with self.assertRaises(urllib.error.HTTPError):req('/',{'Host':'evil.example'})
            with req('/api/status',{'X-Orbita-Token':s.SESSION}) as r:
                data=json.load(r);self.assertNotIn('api_key',data['config']);self.assertTrue(data['ok'])
            with req('/api/files',{'X-Orbita-Token':s.SESSION}) as r:self.assertEqual(json.load(r)['files'][0]['name'],'table.csv')
        finally:server.shutdown();server.server_close()
if __name__=='__main__':unittest.main()

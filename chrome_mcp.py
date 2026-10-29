"""Chrome DevTools MCP client over newline-delimited stdio JSON-RPC.
Only connects to an already running Chrome session (--autoConnect).
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time

ALLOWED_TOOLS = frozenset({'list_pages','new_page','navigate_page','select_page','close_page','take_snapshot','take_screenshot','click','drag','hover','fill','fill_form','press_key','type_text','wait_for','handle_dialog','upload_file'})
MAX_MESSAGE = 16 * 1024 * 1024

class MCPError(RuntimeError): pass

class StdioClient:
    def __init__(self, command, cwd=None):
        self.pending = {}
        self.lock = threading.RLock()
        self.counter = 0
        self.closed = False
        self.stderr_tail = ''
        env = {**os.environ, 'CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS':'1', 'CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS':'1'}
        options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session':True}
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', bufsize=1, **options)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _write(self, message):
        with self.lock:
            if self.closed or self.process.poll() is not None: raise MCPError('Chrome DevTools MCP отключён. Подключите его заново.')
            try:
                self.process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(',',':')) + '\n')
                self.process.stdin.flush()
            except (OSError, ValueError): raise MCPError('Соединение с Chrome DevTools MCP прервано.')

    def _stderr(self):
        # Never expose server logs to a model: they can include local paths or URLs.
        try:
            while True:
                chunk = self.process.stderr.read(512)
                if not chunk: break
                with self.lock: self.stderr_tail = (self.stderr_tail + chunk)[-2048:]
        except (OSError, ValueError): pass

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_MESSAGE + 1)
                if not line: break
                if len(line) > MAX_MESSAGE: raise MCPError('Ответ Chrome MCP превышает лимит 16 МБ.')
                try: data = json.loads(line)
                except ValueError: continue
                if not isinstance(data, dict): continue
                if 'id' in data and 'method' not in data:
                    with self.lock:
                        pending = self.pending.get(data['id'])
                        if pending:
                            pending['response'] = data
                            pending['event'].set()
                elif 'id' in data and 'method' in data:
                    # We don't provide sampling, filesystem roots, or secret prompts.
                    response = {'jsonrpc':'2.0', 'id':data['id']}
                    if data['method'] == 'ping': response['result'] = {}
                    else: response['error'] = {'code':-32601, 'message':'Client capability not available'}
                    self._write(response)
        except (OSError, ValueError, MCPError): pass
        finally:
            with self.lock:
                for pending in self.pending.values():
                    pending['response'] = {'error':{'message':'Chrome DevTools MCP завершил работу. Проверьте Node.js LTS, Chrome и разрешение подключения.'}}
                    pending['event'].set()

    def notify(self, method, params=None):
        message = {'jsonrpc':'2.0','method':method}
        if params is not None: message['params'] = params
        self._write(message)

    def request(self, method, params=None, timeout=90, cancel=None):
        with self.lock:
            self.counter += 1
            ident = self.counter
            pending = {'event':threading.Event()}
            self.pending[ident] = pending
        try:
            self._write({'jsonrpc':'2.0','id':ident,'method':method,'params':params or {}})
            deadline = time.monotonic() + timeout
            while not pending['event'].wait(.2):
                if cancel and cancel.is_set():
                    self.notify('notifications/cancelled',{'requestId':ident,'reason':'User cancelled'})
                    raise MCPError('Действие в Chrome остановлено. Уже отправленное действие могло выполниться.')
                if time.monotonic() > deadline:
                    self.notify('notifications/cancelled',{'requestId':ident,'reason':'Timeout'})
                    raise MCPError('Chrome MCP не ответил вовремя. Проверьте диалог разрешения в Chrome и подключитесь заново.')
            response = pending['response']
            if response.get('error'): raise MCPError(str(response['error'].get('message','Ошибка MCP'))[:1500])
            return response.get('result', {})
        finally:
            with self.lock: self.pending.pop(ident, None)

    def initialize(self):
        result = self.request('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'orbita','version':'1.1.0'}},timeout=120)
        if result.get('protocolVersion') not in ('2024-11-05','2025-03-26','2025-06-18','2025-11-25'):
            raise MCPError('Версия протокола MCP не поддерживается этим приложением.')
        self.notify('notifications/initialized')
        return result

    def close(self):
        with self.lock:
            if self.closed: return
            self.closed = True
            proc = self.process
        if proc.poll() is None:
            try:
                if os.name == 'nt':
                    subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
                else: os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=4)
            except (OSError, subprocess.TimeoutExpired):
                try: proc.kill()
                except OSError: pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try: stream.close()
            except (OSError, ValueError): pass


def chrome_command():
    npx = shutil.which('npx')
    if not npx: raise MCPError('Не найден npx. Установите Node.js LTS и перезапустите Орбиту.')
    args = ['-y','chrome-devtools-mcp@latest','--autoConnect','--no-usage-statistics','--no-performance-crux']
    if os.name == 'nt' and Path(npx).suffix.lower() == '.cmd':
        node = shutil.which('node')
        cli = Path(npx).parent / 'node_modules' / 'npm' / 'bin' / 'npx-cli.js'
        if node and cli.is_file(): return [node,str(cli),*args]
        # Fixed arguments only. There is no user-supplied shell command or flag.
        return ['cmd.exe','/d','/c',npx,*args]
    return [npx,*args]


def plain_text(result):
    return '\n'.join(str(c.get('text','')) for c in result.get('content',[]) if c.get('type')=='text')[:40000]


class ChromeManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.actions = threading.RLock()
        self.client = None
        self.state = 'disconnected'
        self.error = ''
        self.tools = {}
        self.server_info = {}
        self.generation = 0
        self.pages_text = ''

    def status(self):
        with self.lock:
            return {'state':self.state,'connected':self.state=='connected','error':self.error,'server':self.server_info,'tool_count':len(self.tools),'tool_names':sorted(self.tools),'transport':'stdio','mode':'autoConnect'}

    def connect(self):
        with self.lock:
            if self.state in ('connecting','connected'): return self.status()
            self.generation += 1
            generation = self.generation
            self.state = 'connecting'
            self.error = ''
        threading.Thread(target=self._connect,args=(generation,),daemon=True).start()
        return self.status()

    def _connect(self, generation):
        client = None
        try:
            client = StdioClient(chrome_command())
            with self.lock:
                if generation != self.generation: client.close(); return
                self.client = client
            initialized = client.initialize()
            tools = {}
            cursor = None
            for _ in range(10):
                response = client.request('tools/list',{'cursor':cursor} if cursor else {})
                for tool in response.get('tools',[]):
                    if tool.get('name') in ALLOWED_TOOLS: tools[tool['name']] = tool
                cursor = response.get('nextCursor')
                if not cursor: break
            if not {'list_pages','take_snapshot'} <= tools.keys(): raise MCPError('Сервер не предоставил инструменты Chrome. Обновите chrome-devtools-mcp.')
            pages = client.request('tools/call',{'name':'list_pages','arguments':{}},timeout=120)
            if pages.get('isError'): raise MCPError(plain_text(pages) or 'Chrome не разрешил подключение.')
            with self.lock:
                if generation != self.generation: client.close(); return
                self.tools = tools
                self.pages_text = plain_text(pages)
                self.server_info = initialized.get('serverInfo',{})
                self.state = 'connected'
        except Exception as exc:
            if client: client.close()
            with self.lock:
                if generation == self.generation:
                    self.state = 'error'
                    self.error = str(exc)[:1500]
                    self.client = None
                    self.tools = {}

    def disconnect(self):
        with self.lock:
            self.generation += 1
            client = self.client
            self.client = None
            self.state = 'disconnected'
            self.tools = {}
            self.error = ''
            self.pages_text = ''
        if client: client.close()
        return self.status()

    def definitions(self):
        with self.lock:
            if self.state != 'connected': raise MCPError('Сначала подключите Chrome в разделе «Chrome и MCP».')
            definitions = []
            for name,tool in self.tools.items():
                schema = json.loads(json.dumps(tool.get('inputSchema',{'type':'object','properties':{}})))
                # Don't offer arbitrary scripts or filesystem output paths to agents.
                for key in ('filePath','initScript','isolatedContext'):
                    schema.get('properties',{}).pop(key,None)
                    if key in schema.get('required',[]): schema['required'].remove(key)
                definitions.append({'type':'function','function':{'name':'chrome_'+name,'description':str(tool.get('description',name))[:4500],'parameters':schema}})
            return definitions

    def call(self,name,args,cancel=None):
        if name not in ALLOWED_TOOLS: raise MCPError('Инструмент Chrome недоступен.')
        if any(k in args for k in ('filePath','initScript','isolatedContext')): raise MCPError('Этот параметр Chrome недоступен.')
        if name in ('new_page','navigate_page') and args.get('url'):
            from urllib.parse import urlsplit
            if urlsplit(str(args['url'])).scheme not in ('http','https'): raise MCPError('Открывайте только HTTP(S)-страницы в Chrome.')
        with self.actions:
            if cancel and cancel.is_set(): raise MCPError('Задача остановлена.')
            with self.lock:
                if self.state != 'connected' or not self.client: raise MCPError('Chrome не подключён.')
                if name not in self.tools: raise MCPError('Сервер не предоставляет этот инструмент.')
                client = self.client
            try:
                result = client.request('tools/call',{'name':name,'arguments':args},timeout=100,cancel=cancel)
            except MCPError as exc:
                if client.process.poll() is not None or (cancel and cancel.is_set()) or 'вовремя' in str(exc):
                    self.disconnect()
                    with self.lock:
                        self.state = 'error'
                        self.error = str(exc)
                raise
            if result.get('isError'): raise MCPError(plain_text(result) or 'Chrome сообщил об ошибке действия.')
            if name == 'list_pages':
                with self.lock: self.pages_text = plain_text(result)
            return result

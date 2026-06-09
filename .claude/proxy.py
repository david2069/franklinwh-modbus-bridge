"""Simple reverse proxy: 8199 -> 8100"""
import http.server
import urllib.request
import sys

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self._proxy()
    def do_POST(self):
        self._proxy()
    def do_PUT(self):
        self._proxy()
    def do_PATCH(self):
        self._proxy()
    def do_DELETE(self):
        self._proxy()
    def _proxy(self):
        target = f'http://127.0.0.1:8100{self.path}'
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length else None
            req = urllib.request.Request(target, data=body, method=self.command)
            for k, v in self.headers.items():
                if k.lower() not in ('host', 'content-length'):
                    req.add_header(k, v)
            if body:
                req.add_header('Content-Length', str(len(body)))
            with urllib.request.urlopen(req) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ('transfer-encoding',):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())
    def log_message(self, fmt, *args):
        pass

print(f'Proxy on 8199 -> 8100', flush=True)
http.server.HTTPServer(('127.0.0.1', 8199), ProxyHandler).serve_forever()

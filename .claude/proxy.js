const http = require('http');
http.createServer((req, res) => {
  const opts = { hostname: '127.0.0.1', port: 8100, path: req.url, method: req.method, headers: req.headers };
  const proxy = http.request(opts, (pRes) => {
    res.writeHead(pRes.statusCode, pRes.headers);
    pRes.pipe(res);
  });
  proxy.on('error', (e) => { res.writeHead(502); res.end('Proxy error: ' + e.message); });
  req.pipe(proxy);
}).listen(8199, () => console.log('Proxy on 8199 -> 8100'));

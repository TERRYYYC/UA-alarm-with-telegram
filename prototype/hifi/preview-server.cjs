// Read-only, loopback-only preview. Only the generated prototype is served.
// No directory browsing, arbitrary file reads, API calls or persistent auto-start.
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
http.createServer((req,res)=>{
  if(req.method!=='GET'&&req.method!=='HEAD'){res.writeHead(405);return res.end();}
  const url=new URL(req.url,'http://127.0.0.1:4181');
  if(url.pathname!=='/'&&url.pathname!=='/situation-monitor.html'){res.writeHead(404);return res.end('Not found');}
  const body=fs.readFileSync(path.join(__dirname,'situation-monitor.html'));
  res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Content-Length':body.length,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; base-uri 'none'; form-action 'none'"});
  res.end(req.method==='HEAD'?undefined:body);
}).listen(4181,'127.0.0.1',()=>console.log('Prototype only: http://127.0.0.1:4181/situation-monitor.html'));

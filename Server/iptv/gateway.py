import sqlite3, base64, concurrent.futures, hashlib, hmac, http.server, ipaddress, json, os, re, secrets, socket, subprocess, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from collections import OrderedDict
from pathlib import Path

HOST=os.environ.get('JEALOUSFIN_BIND','127.0.0.1')
PORT=int(os.environ.get('JEALOUSFIN_PORT','18796'))
ROOT=Path(os.environ['JEALOUSFIN_STATE_DIR']).expanduser().resolve()
ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
MAX_BODY=32*1024*1024
MAX_PLAYLIST=2*1024*1024
TEST_USER=os.environ['JEALOUSFIN_USERNAME']
TEST_PASSWORD=os.environ['JEALOUSFIN_PASSWORD']
TOKEN=os.environ['JEALOUSFIN_TOKEN']
USER_ID=os.environ['JEALOUSFIN_SERVER_ID']
SAMPLES=[]
lock=threading.RLock()
resources=OrderedDict()
reverse={}
counts={}
favorites=set()
def log(msg):
 print(time.strftime('%Y-%m-%dT%H:%M:%S'),msg,flush=True)
def validate(url):
 p=urllib.parse.urlsplit(url)
 if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.port not in (None,80,443):
  raise ValueError('Unsupported upstream URL')
 ips=socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)
 if not ips or any(not ipaddress.ip_address(x[4][0]).is_global for x in ips):
  raise ValueError('Non-public upstream address')
 return url
class Redirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  return super().redirect_request(req,fp,code,msg,headers,validate(urllib.parse.urljoin(req.full_url,newurl)))
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),Redirect())
def fetch(url,limit,range_header=None):
 validate(url)
 headers={'User-Agent':'Jealousfin-IPTV-Trial/1.0','Accept':'*/*','Accept-Encoding':'identity'}
 if range_header:headers['Range']=range_header
 with opener.open(urllib.request.Request(url,headers=headers),timeout=10) as f:
  data=f.read(limit+1)
  if len(data)>limit:raise ValueError('Response too large')
  return data,f.headers,f.status,f.geturl()
def register(url,cid):
 validate(url)
 with lock:
  key=(cid,url)
  if key in reverse and reverse[key] in resources:
   rid=reverse[key];resources.move_to_end(rid)
  else:
   rid=secrets.token_hex(16);resources[rid]=key;reverse[key]=rid
   while len(resources)>10000:
    old,u=resources.popitem(last=False);reverse.pop(u,None)
 return '/media/'+TOKEN+'/'+rid+('.m3u8' if '.m3u8' in urllib.parse.urlsplit(url).path.lower() else '.bin')
def rewrite(data,base,cid):
 text=data.decode('utf-8-sig')
 if not text.lstrip().startswith('#EXTM3U'):raise ValueError('Invalid playlist')
 out=[]
 for line in text.splitlines():
  if line.startswith('#'):
   line=re.sub(r'URI="([^"]+)"',lambda m:'URI="'+register(urllib.parse.urljoin(base,m[1]),cid)+'"',line)
  elif line.strip():line=register(urllib.parse.urljoin(base,line.strip()),cid)
  out.append(line)
 return ('\n'.join(out)+'\n').encode()
def provider():
 base=os.environ['JEALOUSFIN_PROVIDER_URL'].rstrip('/')
 if base.endswith('/manifest.json'):base=base[:-14].rstrip('/')
 return validate(base)
PROVIDER=provider()
def provider_json(path):
 url=validate(PROVIDER+path)
 req=urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'Jealousfin-Catalogue-Diagnostics/1.0'})
 with opener.open(req,timeout=8) as f:
  data=f.read(MAX_PLAYLIST+1)
  if len(data)>MAX_PLAYLIST:raise ValueError('Provider response too large')
  return json.loads(data)
def catalogue():
 found={}
 for skip in range(0,10000,100):
  path='/catalog/tv/nltv_channels'+('/skip='+str(skip) if skip else '')+'.json'
  rows=provider_json(path).get('metas',[])
  if not rows:break
  prior=len(found)
  for row in rows:
   if isinstance(row.get('id'),str) and row['id'].startswith('nltv:') and isinstance(row.get('name'),str):found[row['id']]=row
  if len(found)==prior:break
 if len(found)<100:raise ValueError('Incomplete channel catalogue')
 return found
raw=catalogue()
channels={}
sources={}
preferred_hosts={s[0]:s[2] for s in SAMPLES}
attempts={}
for i,(source_id,row) in enumerate(raw.items(),1):
 cid=hashlib.md5(source_id.encode()).hexdigest()
 genres=row.get('genres',[])
 if not isinstance(genres,list):genres=[]
 channels[cid]={'Id':cid,'Name':row['name'],'ChannelNumber':str(i),'Type':'TvChannel','Genres':[v for v in genres if isinstance(v,str)],'Tags':[],'UserData':{'IsFavorite':False}}
 sources[cid]=source_id
def resolve(cid):
 source_id=sources[cid]
 candidates=[]
 for v in provider_json('/stream/tv/'+urllib.parse.quote(source_id,safe=':')+'.json').get('streams',[]):
  url=v.get('url')
  if not isinstance(url,str) or url in candidates:continue
  try:validate(url)
  except Exception:continue
  candidates.append(url)
 if not candidates:raise ValueError('No usable stream source')
 prefer=preferred_hosts.get(source_id)
 if prefer:candidates.sort(key=lambda u:urllib.parse.urlsplit(u).hostname!=prefer)
 with lock:
  idx=attempts.get(cid,0)%len(candidates)
  attempts[cid]=idx+1
 return candidates[idx]
ROTATION_FILE=ROOT/'rotation-history.json'
rotation=json.loads(ROTATION_FILE.read_text()) if ROTATION_FILE.exists() else {'schemaVersion':1,'sequence':0,'attemptOrder':{}}
if rotation.get('schemaVersion')!=1 or not isinstance(rotation.get('attemptOrder'),dict):raise ValueError('Invalid rotation history')
def record_attempt(cid):
 with lock:
  updated={'schemaVersion':1,'sequence':rotation['sequence']+1,'attemptOrder':dict(rotation['attemptOrder'])}
  updated['attemptOrder'][cid]=updated['sequence']
  temp=ROTATION_FILE.with_suffix('.tmp')
  temp.write_text(json.dumps(updated,separators=(',',':')))
  os.replace(temp,ROTATION_FILE)
  rotation.update(updated)
def ordered_catalogue():
 # Consult the observer's durable policy database without a restart.
 # Keep the last valid policy if SQLite is momentarily busy.
 global policy_snapshot
 try:
  with sqlite3.connect((ROOT/'policy.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=1) as db:
   rows=db.execute('SELECT id,last_attempt,hiatus_until,banned FROM channel').fetchall()
  if len(rows)>=len(channels):
   policy_snapshot={cid:(attempt,until,banned) for cid,attempt,until,banned in rows}
 except (sqlite3.Error,OSError):pass
 now=time.time()
 valid=[item for item in channels.items() if not policy_snapshot.get(item[0],(0,0,0))[2] and policy_snapshot.get(item[0],(0,0,0))[1]<=now]
 return sorted(valid,key=lambda item:policy_snapshot.get(item[0],(0,0,0))[0])
policy_snapshot={}
def record_source_error(cid,url,phase,error):
 # Never persist stream URLs, query parameters, headers or credentials.
 host=urllib.parse.urlsplit(url).hostname if url else None
 entry={'at':time.time(),'channel_id':cid,'host':host,'phase':phase,'error':error}
 with lock:
  p=ROOT/'source-errors.jsonl'
  if p.exists() and p.stat().st_size>4*1024*1024:
   os.replace(p,ROOT/'source-errors.previous.jsonl')
  with p.open('a') as f:f.write(json.dumps(entry,separators=(',',':'))+'\n')


class Server(http.server.ThreadingHTTPServer):
 daemon_threads=True
 allow_reuse_address=True
class Handler(http.server.BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*args):pass
 def reply(self,code,body,mime='application/json',extra=None):
  if not isinstance(body,bytes):body=json.dumps(body,separators=(',',':')).encode()
  self.send_response(code);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('Connection','close')
  for k,v in (extra or {}).items():self.send_header(k,v)
  self.end_headers();self.close_connection=True
  if self.command!='HEAD':
   try:self.wfile.write(body)
   except (BrokenPipeError,ConnectionResetError):pass
 def authorized(self,q):
  h=self.headers.get('Authorization','')+' '+self.headers.get('X-Emby-Authorization','')
  return hmac.compare_digest(q.get('api_key',[''])[0],TOKEN) or hmac.compare_digest(self.headers.get('X-Emby-Token',''),TOKEN) or ('Token="'+TOKEN+'"') in h
 def handle_request(self):
  p=urllib.parse.urlsplit(self.path);path=p.path;q=urllib.parse.parse_qs(p.query)
  with lock:counts[path.split('/')[1] if path.startswith('/') else 'other']=counts.get(path.split('/')[1] if path.startswith('/') else 'other',0)+1
  if path=='/System/Info/Public':return self.reply(200,{'ServerName':'Jealousfin IPTV Trial','Version':'10.10.0','Id':USER_ID})
  if path=='/trial-status' and self.client_address[0] in (HOST,'127.0.0.1'):
   return self.reply(200,{'channels':len(channels),'requests':counts,'resources':len(resources),'policyRevision':'sqlite-policy-v1','policyChannels':len(policy_snapshot)})
  if path=='/Users/AuthenticateByName' and self.command=='POST':
   n=int(self.headers.get('Content-Length','0'))
   if n>65536:return self.reply(413,{})
   d=json.loads(self.rfile.read(n))
   if not hmac.compare_digest(str(d.get('Username','')),TEST_USER) or not hmac.compare_digest(str(d.get('Pw','')),TEST_PASSWORD):return self.reply(401,{})
   return self.reply(200,{'AccessToken':TOKEN,'User':{'Id':USER_ID,'Name':TEST_USER}})
  if path.startswith('/media/'):
   parts=path.split('/')
   if len(parts)!=4 or not hmac.compare_digest(parts[2],TOKEN):return self.reply(401,{})
   rid=parts[3].split('.')[0]
   with lock:
    entry=resources.get(rid)
    if entry:resources.move_to_end(rid)
   if not entry:return self.reply(404,{})
   cid,url=entry
   try:
    rg=self.headers.get('Range')
    if rg and not re.fullmatch(r'bytes=\d*-\d*',rg):return self.reply(416,{})
    data,headers,status,final=fetch(url,MAX_BODY,rg)
    if data.lstrip(b'\xef\xbb\xbf \r\n').startswith(b'#EXTM3U'):
     if len(data)>MAX_PLAYLIST:return self.reply(502,{})
     return self.reply(200,rewrite(data,final,cid),'application/vnd.apple.mpegurl')
    extra={k:headers[k] for k in ('Content-Range','Accept-Ranges') if headers.get(k)}
    return self.reply(status,data,headers.get('Content-Type','application/octet-stream'),extra)
   except urllib.error.HTTPError as e:
    record_source_error(cid,url,'media','HTTP '+str(e.code));log('upstream HTTP '+str(e.code));return self.reply(502,{})
   except Exception as e:
    record_source_error(cid,url,'media',type(e).__name__);log('upstream failure '+type(e).__name__);return self.reply(502,{})
  if not self.authorized(q):return self.reply(401,{})
  if path=='/LiveTv/Channels':
   out=[]
   with lock:
    for cid,c in ordered_catalogue():out.append(dict(c,UserData={'IsFavorite':cid in favorites}))
   return self.reply(200,{'Items':out,'TotalRecordCount':len(out),'JealousfinPolicyRevision':'sqlite-policy-v1'})
  if path in ('/LiveTv/Programs','/LiveTv/Recordings'):return self.reply(200,{'Items':[],'TotalRecordCount':0})
  m=re.fullmatch(r'/Items/([a-f0-9]{32})/PlaybackInfo',path)
  if m and m[1] in sources:
   n=int(self.headers.get('Content-Length','0'))
   if n>262144:return self.reply(413,{})
   if n:self.rfile.read(n)
   cid=m[1];record_attempt(cid)
   try:url=register(resolve(cid),cid);url=url.rsplit('.',1)[0]+'.m3u8'
   except Exception as e:
    record_source_error(cid,None,'resolve',type(e).__name__);return self.reply(502,{})
   return self.reply(200,{'PlaySessionId':uuid.uuid4().hex,'MediaSources':[{'Id':cid,'TranscodingUrl':url,'SupportsTranscoding':True,'SupportsDirectPlay':False,'SupportsDirectStream':False}]})
  if path.startswith('/Sessions/Playing') or path=='/LiveStreams/Close':
   n=int(self.headers.get('Content-Length','0'))
   if n>262144:return self.reply(413,{})
   if n:self.rfile.read(n)
   return self.reply(200,{})
  m=re.fullmatch(r'/Users/[^/]+/FavoriteItems/([a-f0-9]{32})',path)
  if m and m[1] in channels:
   with lock:
    if self.command=='DELETE':favorites.discard(m[1])
    else:favorites.add(m[1])
   return self.reply(200,{})
  return self.reply(404,{})
 def do_GET(self):
  try:self.handle_request()
  except Exception as e:
   log('request failure '+type(e).__name__)
   try:self.reply(500,{})
   except Exception:pass
 do_POST=do_GET
 do_DELETE=do_GET
 do_HEAD=do_GET
server=Server((HOST,PORT),Handler)
(ROOT/'trial-info.json').write_text(json.dumps({'url':f'http://{HOST}:{PORT}','pid':os.getpid(),'expiresAt':None,'channels':[{'id':x,'name':c['Name']} for x,c in channels.items()]}))
log('READY channels='+str(len(channels)))
try:server.serve_forever(poll_interval=.5)
finally:
 server.server_close()
 try:(ROOT/'trial-info.json').unlink()
 except FileNotFoundError:pass

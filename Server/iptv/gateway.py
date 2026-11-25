import sqlite3, base64, concurrent.futures, hashlib, hmac, http.server, ipaddress, json, os, re, secrets, socket, subprocess, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from collections import OrderedDict
from pathlib import Path
from policy_observer import connect as policy_connect, failure as policy_failure, penalty as policy_penalty

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
resource_touched={}
RESOURCE_TTL=900
RESOURCE_LIMIT=10000
active_channels={}
CONFIG_FILE=ROOT/'gateway-runtime.json'
CONFIG_DEFAULTS={'schemaVersion':1,'revision':'native-baseline','upstreamTimeoutSeconds':10,
                 'providerTimeoutSeconds':8,'upstreamRetries':0,'resourceTTLSeconds':900,
                 'resourceLimit':10000,'activeProtectionSeconds':120,'sourceSelection':'round-robin','preferredHosts':{}}
gateway_config=dict(CONFIG_DEFAULTS)
config_fingerprint=None
config_checked_at=0.0
config_fault=None
resource_correlation={}
def validate_gateway_config(raw):
 if not isinstance(raw,dict) or set(raw)-set(CONFIG_DEFAULTS):raise ValueError('Unknown gateway configuration fields')
 result=dict(CONFIG_DEFAULTS,**raw)
 if type(result['schemaVersion']) is not int or result['schemaVersion']!=1:raise ValueError('Unknown configuration schema')
 if not isinstance(result['revision'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',result['revision']):raise ValueError('Invalid revision')
 for key,lo,hi in [('upstreamTimeoutSeconds',2,30),('providerTimeoutSeconds',2,30),('upstreamRetries',0,2),('resourceTTLSeconds',120,3600),('resourceLimit',500,50000),('activeProtectionSeconds',30,300)]:
  value=result[key]
  if type(value) is not int or not lo<=value<=hi:raise ValueError('Invalid '+key)
 if result['activeProtectionSeconds']>result['resourceTTLSeconds']:raise ValueError('Protection exceeds resource TTL')
 if result['sourceSelection'] not in ('round-robin','preferred-first','first'):raise ValueError('Unknown selection strategy')
 hosts=result['preferredHosts']
 if not isinstance(hosts,dict) or len(hosts)>1000:raise ValueError('Invalid host preference map')
 for cid,host in hosts.items():
  if not re.fullmatch(r'[a-f0-9]{32}',cid) or not isinstance(host,str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}',host):raise ValueError('Invalid preferred host')
 return result
def reload_gateway_config(force=False):
 global config_checked_at,config_fingerprint,config_fault,gateway_config,RESOURCE_TTL,RESOURCE_LIMIT
 with lock:
  now=time.monotonic()
  if not force and now-config_checked_at<2:return False
  config_checked_at=now
  try:
   if not CONFIG_FILE.exists():
    # Removal does not silently revert the running configuration.
    return False
   st=CONFIG_FILE.stat();fingerprint=(st.st_mtime_ns,st.st_size,st.st_ino)
   if not force and fingerprint==config_fingerprint:return False
   config_fingerprint=fingerprint
   if st.st_size>65536:raise ValueError('Configuration too large')
   candidate=validate_gateway_config(json.loads(CONFIG_FILE.read_text()))
   if candidate==gateway_config:
    config_fault=None;return False
   if candidate['revision']==gateway_config['revision']:raise ValueError('Use a new revision')
   gateway_config=candidate;RESOURCE_TTL=candidate['resourceTTLSeconds'];RESOURCE_LIMIT=candidate['resourceLimit']
   config_fault=None
   evict_resources(now)
   log('gateway configuration activated '+candidate['revision'])
   return True
  except (ValueError,OSError,TypeError) as error:
   config_fault=type(error).__name__
   log('gateway configuration rejected '+config_fault)
   return False
def request_correlation(value):
 return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value) else uuid.uuid4().hex
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
 config=gateway_config
 for attempt in range(config['upstreamRetries']+1):
  try:
   with opener.open(urllib.request.Request(url,headers=headers),timeout=config['upstreamTimeoutSeconds']) as f:
    data=f.read(limit+1)
    if len(data)>limit:raise ValueError('Response too large')
    return data,f.headers,f.status,f.geturl()
  except urllib.error.HTTPError as error:
   if attempt>=config['upstreamRetries'] or error.code not in (408,429,500,502,503,504):raise
   error.close()
  except (urllib.error.URLError,TimeoutError):
   if attempt>=config['upstreamRetries']:raise
def evict_resources(now,required=0):
 # Idle expiry plus LRU under pressure. Protect active-channel media touched
 # recently; if all slots are protected, reject growth rather than corrupt playback.
 for rid,(cid,url) in list(resources.items()):
  age=now-resource_touched.get(rid,0)
  active=active_channels.get(cid,0)>now and age<gateway_config['activeProtectionSeconds']
  if not active and (age>RESOURCE_TTL or len(resources)+required>RESOURCE_LIMIT):
   resources.pop(rid,None);resource_touched.pop(rid,None);resource_correlation.pop(rid,None);reverse.pop((cid,url),None)
 for cid,until in list(active_channels.items()):
  if until<=now:active_channels.pop(cid,None)
def register(url,cid,correlation=None):
 validate(url)
 with lock:
  key=(cid,url);now=time.monotonic()
  if key in reverse and reverse[key] in resources:
   rid=reverse[key];resources.move_to_end(rid)
  else:
   if len(resources)>=RESOURCE_LIMIT:evict_resources(now,1)
   if len(resources)>=RESOURCE_LIMIT:raise RuntimeError("Resource registry saturated")
   rid=secrets.token_hex(16);resources[rid]=key;reverse[key]=rid
  resource_touched[rid]=now
  if correlation:resource_correlation[rid]=correlation
 return '/media/'+TOKEN+'/'+rid+('.m3u8' if '.m3u8' in urllib.parse.urlsplit(url).path.lower() else '.bin')
def rewrite(data,base,cid,correlation=None):
 text=data.decode('utf-8-sig')
 if not text.lstrip().startswith('#EXTM3U'):raise ValueError('Invalid playlist')
 out=[]
 for line in text.splitlines():
  if line.startswith('#'):
   line=re.sub(r'URI="([^"]+)"',lambda m:'URI="'+register(urllib.parse.urljoin(base,m[1]),cid,correlation)+'"',line)
  elif line.strip():line=register(urllib.parse.urljoin(base,line.strip()),cid,correlation)
  out.append(line)
 return ('\n'.join(out)+'\n').encode()
def provider():
 base=os.environ['JEALOUSFIN_PROVIDER_URL'].rstrip('/')
 if base.endswith('/manifest.json'):base=base[:-14].rstrip('/')
 return validate(base)
PROVIDER=None
def provider_json(path):
 if PROVIDER is None:raise RuntimeError('Provider is not initialized')
 url=validate(PROVIDER+path)
 req=urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'Jealousfin-Catalogue-Diagnostics/1.0'})
 with opener.open(req,timeout=gateway_config['providerTimeoutSeconds']) as f:
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
channels={}
sources={}
preferred_hosts={s[0]:s[2] for s in SAMPLES}
attempts={}
def initialize_catalogue():
 raw=catalogue()
 for i,(source_id,row) in enumerate(raw.items(),1):
  cid=hashlib.md5(source_id.encode()).hexdigest()
  genres=row.get('genres',[])
  if not isinstance(genres,list):genres=[]
  channels[cid]={'Id':cid,'Name':row['name'],'ChannelNumber':str(i),'Type':'TvChannel','Genres':[v for v in genres if isinstance(v,str)],'Tags':[],'UserData':{'IsFavorite':False}}
  sources[cid]=source_id
 # A pre-existing initialized database may acquire new channels. Preserve every
 # prior penalty and insert only newly discovered IDs; the observer handles initial migration.
 with policy_connect(ROOT/'policy.sqlite3') as db:
  if db.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
   db.executemany("INSERT OR IGNORE INTO channel(id,name) VALUES(?,?)",[(cid,c['Name']) for cid,c in channels.items()])
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
 config=gateway_config
 prefer=config['preferredHosts'].get(cid) or preferred_hosts.get(source_id)
 if prefer and config['sourceSelection']!='first':candidates.sort(key=lambda u:urllib.parse.urlsplit(u).hostname!=prefer)
 with lock:
  idx=attempts.get(cid,0)%len(candidates) if config['sourceSelection']=='round-robin' else 0
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
def policy_rows():
 # No false readiness marker or silent fail-open when policy storage fails.
 with sqlite3.connect((ROOT/'policy.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=1) as db:
  if not db.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():raise RuntimeError("Policy migration pending")
  columns={r[1] for r in db.execute("PRAGMA table_info(channel)")}
  policy_penalty(db)
  approval="approved_at" if "approved_at" in columns else "0"
  success="last_success" if "last_success" in columns else "0"
  rows=db.execute("SELECT id,last_attempt,hiatus_until,banned,failures,last_failure,reason,sustained,"+approval+","+success+" FROM channel").fetchall()
 result={r[0]:dict(lastAttempt=r[1],hiatusUntil=r[2],banned=bool(r[3]),failures=r[4],lastFailure=r[5],reason=r[6],successes=r[7],approvedAt=r[8],lastSuccess=r[9]) for r in rows if r[0] in channels}
 if not set(channels).issubset(result):raise RuntimeError("Policy catalogue incomplete")
 return result
def policy_eligible(row,now):
 return not row["banned"] and row["hiatusUntil"]<=now
def ordered_catalogue(snapshot,include_excluded=False):
 now=time.time()
 return sorted((item for item in channels.items() if include_excluded or policy_eligible(snapshot[item[0]],now)),key=lambda item:(snapshot[item[0]]["lastAttempt"],item[0]))
def policy_payload(snapshot):
 with sqlite3.connect((ROOT/'policy.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=1) as db:
  days,ban=policy_penalty(db)
 return {"revision":"sqlite-policy-v2","channels":snapshot,"penalty":{"baseDays":days,"banAfter":ban}}
def policy_event(cid,trace,action,reason):
 with policy_connect(ROOT/'policy.sqlite3') as db:
  if not db.execute("SELECT 1 FROM channel WHERE id=?",(cid,)).fetchone():raise ValueError("Unknown policy channel")
  now=time.time()
  event_id="native-policy:"+trace+":"+action
  prior=db.execute("SELECT cid FROM observed WHERE id=?",(event_id,)).fetchone()
  if prior:
   if prior[0]!=cid:raise ValueError("Policy event identity mismatch")
   return
  db.execute("INSERT INTO observed VALUES(?,?,?,?,?,?)",(event_id,now,"NATIVE_POLICY_"+action,cid,trace,reason[:300]))
  if action=="failure":
   db.execute("INSERT OR IGNORE INTO sessions(trace,cid,opened) VALUES(?,?,?)",(trace,cid,now))
   if db.execute("SELECT cid FROM sessions WHERE trace=?",(trace,)).fetchone()[0]!=cid:raise ValueError("Trace/channel mismatch")
   policy_failure(db,cid,now,reason,trace)
  elif action=="success":
   # Native client sends success only after sustained decoded playback.
   db.execute("INSERT OR IGNORE INTO sessions(trace,cid,opened,confirmed) VALUES(?,?,?,?)",(trace,cid,now,now))
   row=db.execute("SELECT cid,failed,outcome,opened FROM sessions WHERE trace=?",(trace,)).fetchone()
   if row[0]!=cid:raise ValueError("Trace/channel mismatch")
   if not row[1] and row[2]!='sustained':
    updated=db.execute("UPDATE channel SET sustained=sustained+1,failures=0,hiatus_until=0,last_success=? WHERE id=? AND banned=0 AND hiatus_until<=? AND last_failure<?",(now,cid,now,row[3]))
    if updated.rowcount:db.execute("UPDATE sessions SET outcome='sustained' WHERE trace=?",(trace,))
  elif action=="approve":
   db.execute("UPDATE channel SET banned=0,failures=0,hiatus_until=0,approved_at=? WHERE id=? AND banned=1",(now,cid))
  else:raise ValueError("Unknown policy action")
def record_source_error(cid,url,phase,error,correlation=None):
 # Never persist stream URLs, query parameters, headers or credentials.
 host=urllib.parse.urlsplit(url).hostname if url else None
 entry={'at':time.time(),'channel_id':cid,'host':host,'phase':phase,'error':error,'request_id':correlation}
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
  reload_gateway_config()
  correlation=request_correlation(self.headers.get('X-Jealousfin-Request-ID'))
  p=urllib.parse.urlsplit(self.path);path=p.path;q=urllib.parse.parse_qs(p.query)
  with lock:counts[path.split('/')[1] if path.startswith('/') else 'other']=counts.get(path.split('/')[1] if path.startswith('/') else 'other',0)+1
  if path=='/System/Info/Public':return self.reply(200,{'ServerName':'Jealousfin IPTV Trial','Version':'10.10.0','Id':USER_ID})
  if path=='/trial-status' and self.client_address[0] in (HOST,'127.0.0.1'):
   try:snapshot=policy_rows();ready=True
   except (sqlite3.Error,OSError,RuntimeError,ValueError):snapshot={};ready=False
   return self.reply(200,{'channels':len(channels),'requests':counts,'resources':len(resources),'policyRevision':'sqlite-policy-v2','policyReady':ready,'policyChannels':len(snapshot),'configurationRevision':gateway_config['revision'],'configurationFault':config_fault,'resourceLimit':RESOURCE_LIMIT})
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
    if entry:
     now=time.monotonic()
     if now-resource_touched.get(rid,0)>RESOURCE_TTL:
      resources.pop(rid,None);reverse.pop(entry,None);resource_touched.pop(rid,None);resource_correlation.pop(rid,None);entry=None
     else:
      resources.move_to_end(rid);resource_touched[rid]=now;active_channels[entry[0]]=now+gateway_config['activeProtectionSeconds']
      correlation=resource_correlation.get(rid,correlation)
   if not entry:return self.reply(404,{})
   cid,url=entry
   try:
    rg=self.headers.get('Range')
    if rg and not re.fullmatch(r'bytes=\d*-\d*',rg):return self.reply(416,{})
    data,headers,status,final=fetch(url,MAX_BODY,rg)
    if data.lstrip(b'\xef\xbb\xbf \r\n').startswith(b'#EXTM3U'):
     if len(data)>MAX_PLAYLIST:return self.reply(502,{})
     return self.reply(200,rewrite(data,final,cid,correlation),'application/vnd.apple.mpegurl')
    extra={k:headers[k] for k in ('Content-Range','Accept-Ranges') if headers.get(k)}
    return self.reply(status,data,headers.get('Content-Type','application/octet-stream'),extra)
   except urllib.error.HTTPError as e:
    record_source_error(cid,url,'media','HTTP '+str(e.code),correlation);log('upstream HTTP '+str(e.code));return self.reply(502,{})
   except Exception as e:
    record_source_error(cid,url,'media',type(e).__name__,correlation);log('upstream failure '+type(e).__name__);return self.reply(502,{})
  if not self.authorized(q):return self.reply(401,{})
  if path in ('/LiveTv/Channels','/Jealousfin/Policy'):
   try:snapshot=policy_rows()
   except (sqlite3.Error,OSError,RuntimeError,ValueError):return self.reply(503,{'JealousfinPolicyError':'unavailable'})
   if path=='/Jealousfin/Policy':return self.reply(200,policy_payload(snapshot))
   include=q.get('JealousfinIncludePolicy',['false'])[0]=='true'
   with lock:out=[dict(c,UserData={'IsFavorite':cid in favorites}) for cid,c in ordered_catalogue(snapshot,include)]
   result={'Items':out,'TotalRecordCount':len(out),'JealousfinPolicyRevision':'sqlite-policy-v1'}
   if include:result['JealousfinPolicy']=policy_payload(snapshot)
   return self.reply(200,result)
  if path=='/Jealousfin/Policy/Event' and self.command=='POST':
   n=int(self.headers.get('Content-Length','0'))
   if not 0<n<=8192:return self.reply(400,{})
   try:
    d=json.loads(self.rfile.read(n));cid=d['channelID'];trace=d['trace'];action=d['action'];reason=d.get('reason','')
    if cid not in channels or not isinstance(trace,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',trace) or not isinstance(reason,str) or len(reason)>512:raise ValueError("Invalid event")
    if action=='approve' and d.get('approval')!='explicit':return self.reply(403,{})
    policy_event(cid,trace,action,reason)
    return self.reply(200,policy_payload(policy_rows()))
   except (KeyError,ValueError,TypeError):return self.reply(400,{})
   except (sqlite3.Error,OSError,RuntimeError,ValueError):return self.reply(503,{'JealousfinPolicyError':'unavailable'})
  if path in ('/LiveTv/Programs','/LiveTv/Recordings'):return self.reply(200,{'Items':[],'TotalRecordCount':0})
  m=re.fullmatch(r'/Items/([a-f0-9]{32})/PlaybackInfo',path)
  if m and m[1] in sources:
   n=int(self.headers.get('Content-Length','0'))
   if n>262144:return self.reply(413,{})
   if n:self.rfile.read(n)
   cid=m[1]
   try:snapshot=policy_rows()
   except (sqlite3.Error,OSError,RuntimeError,ValueError):return self.reply(503,{'JealousfinPolicyError':'unavailable'})
   if not policy_eligible(snapshot[cid],time.time()):return self.reply(409,{'JealousfinPolicyError':'excluded'})
   record_attempt(cid)
   with lock:
    now=time.monotonic();active_channels[cid]=now+gateway_config['activeProtectionSeconds'];evict_resources(now)
   try:url=register(resolve(cid),cid,correlation);url=url.rsplit('.',1)[0]+'.m3u8'
   except Exception as e:
    record_source_error(cid,None,'resolve',type(e).__name__,correlation);return self.reply(502,{})
   try:snapshot=policy_rows()
   except (sqlite3.Error,OSError,RuntimeError,ValueError):return self.reply(503,{'JealousfinPolicyError':'unavailable'})
   if not policy_eligible(snapshot[cid],time.time()):return self.reply(409,{'JealousfinPolicyError':'excluded'})
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
def main():
 global PROVIDER
 PROVIDER=provider()
 reload_gateway_config(force=True)
 initialize_catalogue()
 server=Server((HOST,PORT),Handler)
 (ROOT/'trial-info.json').write_text(json.dumps({'url':f'http://{HOST}:{PORT}','pid':os.getpid(),'expiresAt':None,'channels':[{'id':x,'name':c['Name']} for x,c in channels.items()]}))
 log('READY channels='+str(len(channels)))
 try:server.serve_forever(poll_interval=.5)
 finally:
  server.server_close()
  try:(ROOT/'trial-info.json').unlink()
  except FileNotFoundError:pass
if __name__=='__main__':main()

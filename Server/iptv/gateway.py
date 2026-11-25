import datetime, gzip, io, xml.etree.ElementTree as ET, http.client, sqlite3, base64, concurrent.futures, hashlib, hmac, http.server, ipaddress, json, os, re, secrets, socket, subprocess, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from collections import OrderedDict, deque
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
resource_keys={}
segment_resources=set()
segment_cache=OrderedDict()
segment_cache_bytes=0
segment_flights={}
quality=OrderedDict()
manifest_quality=OrderedDict()
CACHE_BYTES=64*1024*1024
CACHE_TTL=45
MAX_SEGMENT=8*1024*1024
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
FAVORITES_FILE=ROOT/'favorites.json'

def atomic_json(path,value):
 temp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
 try:
  with os.fdopen(os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:
   json.dump(value,f,separators=(',',':'));f.flush();os.fsync(f.fileno())
  os.replace(temp,path)
 finally:
  if temp.exists():temp.unlink()

def load_favorites():
 if not FAVORITES_FILE.exists():return
 data=json.loads(FAVORITES_FILE.read_text())
 if not isinstance(data,list) or any(not isinstance(x,str) or not re.fullmatch(r'[a-f0-9]{32}',x) for x in data):raise ValueError('Invalid favorites state')
 favorites.clear();favorites.update(data)

def set_favorite(cid,value):
 with lock:
  updated=set(favorites)
  if value:updated.add(cid)
  else:updated.discard(cid)
  atomic_json(FAVORITES_FILE,sorted(updated))
  favorites.clear();favorites.update(updated)

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
  except (urllib.error.URLError,TimeoutError,http.client.IncompleteRead,ConnectionError):
   if attempt>=config['upstreamRetries']:raise
def remove_resource(rid):
 global segment_cache_bytes
 entry=resources.pop(rid,None)
 if entry:reverse.pop(resource_keys.pop(rid,entry),None)
 resource_touched.pop(rid,None);resource_correlation.pop(rid,None);segment_resources.discard(rid)
 cached=segment_cache.pop(rid,None)
 if cached:segment_cache_bytes-=len(cached[1][0])

def expire_cache(now):
 global segment_cache_bytes
 for rid,(expires,result) in list(segment_cache.items()):
  if expires<=now or segment_cache_bytes>CACHE_BYTES:
   segment_cache.pop(rid);segment_cache_bytes-=len(result[0])

def fetch_media(rid,url,rg):
 # Cache only explicitly sequenced complete media segments, not keys or playlists.
 global segment_cache_bytes
 if rg:return fetch(url,MAX_BODY,rg),False
 with lock:
  eligible=rid in segment_resources
  if eligible:
   expire_cache(time.monotonic())
   cached=segment_cache.get(rid)
   if cached:segment_cache.move_to_end(rid);return cached[1],True
   flight=segment_flights.get(rid)
   leader=flight is None
   if leader:flight=threading.Event();segment_flights[rid]=flight
 if not eligible:return fetch(url,MAX_BODY,rg),False
 if not leader:
  flight.wait(timeout=min(30,gateway_config['upstreamTimeoutSeconds']*(1+gateway_config['upstreamRetries'])+1))
  with lock:
   cached=segment_cache.get(rid)
   if cached and cached[0]>time.monotonic():return cached[1],True
  return fetch(url,MAX_BODY,rg),False
 try:
  result=fetch(url,MAX_BODY,rg)
  data,headers,status,final=result
  if status==200 and 0<len(data)<=MAX_SEGMENT and not data.lstrip(b'\xef\xbb\xbf \r\n').startswith(b'#EXTM3U'):
   with lock:
    if rid in segment_resources and rid not in segment_cache:
     segment_cache[rid]=(time.monotonic()+CACHE_TTL,result);segment_cache_bytes+=len(data);expire_cache(time.monotonic())
  return result,False
 finally:
  with lock:
   segment_flights.pop(rid,None);flight.set()

def record_delivery(cid,kind,elapsed,size,cache_hit=False,error=False):
 with lock:
  if cid not in quality:quality[cid]=deque(maxlen=128)
  quality.move_to_end(cid)
  quality[cid].append((kind,round(elapsed*1000,1),size,bool(cache_hit),bool(error)))
  while len(quality)>300:quality.popitem(last=False)

def quality_summary():
 with lock:
  result={}
  for cid,rows in quality.items():
   segments=[r for r in rows if r[0]=='segment']
   successful=[r for r in segments if not r[4]]
   times=sorted(r[1] for r in successful if not r[3])
   result[cid]={'segmentSamples':len(segments),'segmentErrors':sum(r[4] for r in segments),
                'cacheHits':sum(r[3] for r in segments),'upstreamSamples':len(times),
                'upstreamP50Ms':times[len(times)//2] if times else None,
                'upstreamP95Ms':times[min(len(times)-1,int(.95*len(times)))] if times else None,
                'upstreamMaxMs':max(times) if times else None,'segmentBytes':sum(r[2] for r in successful)}
   result[cid].update(manifest_quality.get(cid,{}))
  return result

def evict_resources(now,required=0):
 # Idle expiry plus LRU under pressure. Protect active-channel media touched
 # recently; if all slots are protected, reject growth rather than corrupt playback.
 for rid,(cid,url) in list(resources.items()):
  age=now-resource_touched.get(rid,0)
  active=active_channels.get(cid,0)>now and age<gateway_config['activeProtectionSeconds']
  if not active and (age>RESOURCE_TTL or len(resources)+required>RESOURCE_LIMIT):
   remove_resource(rid)
 for cid,until in list(active_channels.items()):
  if until<=now:active_channels.pop(cid,None)
def register(url,cid,correlation=None,identity=None):
 validate(url)
 with lock:
  key=(cid,url) if identity is None else (cid,url,identity);now=time.monotonic()
  if key in reverse and reverse[key] in resources:
   rid=reverse[key];resources.move_to_end(rid)
  else:
   if len(resources)>=RESOURCE_LIMIT:evict_resources(now,1)
   if len(resources)>=RESOURCE_LIMIT:raise RuntimeError("Resource registry saturated")
   rid=secrets.token_hex(16);resources[rid]=(cid,url);reverse[key]=rid;resource_keys[rid]=key
   if identity is not None:segment_resources.add(rid)
  resource_touched[rid]=now
  if correlation:resource_correlation[rid]=correlation
 return '/media/'+TOKEN+'/'+rid+('.m3u8' if '.m3u8' in urllib.parse.urlsplit(url).path.lower() else '.bin')
def rewrite(data,base,cid,correlation=None):
 text=data.decode('utf-8-sig')
 if not text.lstrip().startswith('#EXTM3U'):raise ValueError('Invalid playlist')
 out=[];sequence=None;discontinuity=0;segment=False;byterange=False
 durations=[];discontinuity_tags=0;first_sequence=None
 for line in text.splitlines():
  if line.startswith('#'):
   if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
    try:sequence=int(line.split(':',1)[1]);first_sequence=sequence
    except ValueError:sequence=None
   elif line.startswith('#EXT-X-DISCONTINUITY-SEQUENCE:'):
    try:discontinuity=int(line.split(':',1)[1])
    except ValueError:discontinuity=0
   elif line=='#EXT-X-DISCONTINUITY':discontinuity+=1;discontinuity_tags+=1
   elif line.startswith('#EXTINF:'):
    segment=True
    try:
     duration=float(line.split(':',1)[1].split(',',1)[0])
     if 0<duration<3600:durations.append(duration)
    except ValueError:pass
   elif line.startswith('#EXT-X-BYTERANGE:'):byterange=True
   line=re.sub(r'URI="([^"]+)"',lambda m:'URI="'+register(urllib.parse.urljoin(base,m[1]),cid,correlation)+'"',line)
  elif line.strip():
   absolute=urllib.parse.urljoin(base,line.strip())
   identity=(base,discontinuity,sequence) if segment and sequence is not None and not byterange else None
   line=register(absolute,cid,correlation,identity)
   if segment and sequence is not None:sequence+=1
   segment=False;byterange=False
  out.append(line)
 if durations:
  with lock:
   manifest_quality[cid]={'playlistMediaSequence':first_sequence,'playlistSegments':len(durations),
                         'playlistDurationSeconds':round(sum(durations),3),
                         'playlistMinSegmentSeconds':min(durations),'playlistMaxSegmentSeconds':max(durations),
                         'playlistDiscontinuities':discontinuity_tags,'playlistObservedAt':time.time()}
   manifest_quality.move_to_end(cid)
   while len(manifest_quality)>300:manifest_quality.popitem(last=False)
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
catalogue_source='uninitialized'
catalogue_fault=None
programme_rows={}
programme_observed_at=0.0
programme_fault=None
PROGRAMME_REFRESH=60
PROGRAMME_MAX_AGE=150
# Explicit distributor IDs confirmed in the November 25 guide audit.
# Channel name must match exactly, and two schedules must corroborate.
# These are schedule listings, not verified content fingerprints.
GUIDE_CHANNELS={
 'OuterSphere':('6a1610bebdf296985fd95603-641496bf238e21f260e7844a','USBD1200007FE'),
 'Overtime':('6a1610bebdf296985fd95603-62a388865440744efae8e243','US33000154M'),
 'Racer Select':('6a1610bebdf296985fd95603-5f0641d0e8ffda004033aff4','USBD1700019RN'),
 'Stadium':('6a1610bebdf296985fd95603-5f6142d2fc6ce20041ec3c92','USAJ3504705A'),
 'The Bob Ross Channel':('6a1610bebdf296985fd95603-5efe1de6e0b3f5003f5d9f88','USBD700012OA'),
 'This Old House':('6a1610bebdf296985fd95603-62e3232ca105dea9c400e9fe','USBC13000065U'),
 'Tiny House Nation':('6a1610bebdf296985fd95603-63c1e176f0f132afc3afbab0','USAJ3400020KC'),
 'FIFA+':('6a1610bebdf296985fd95603-66628d4a8dfc36b8c8a399c4','USBD12000255B'),
 'NFL Channel':('6a1610bebdf296985fd95603-61e805952502a7a6fa84d70f','US2600007WJ'),
 '48 Hours':('6a1610bebdf296985fd95603-668561babc1490a2f408f30d','US1000005GY'),
 'Dog the Bounty Hunter':('6a1610bebdf296985fd95603-66428faa5116d565e63ae32a','US900002IH'),
 'Ebony TV by Lionsgate':('6a1610bebdf296985fd95603-6533de63bd43e1c8b1865152','USBD2500012DH'),
 'FilmRise Western':('6a1610bebdf296985fd95603-6449369eb3ff17382cae57fd','USBB32000196O'),
 'Gravitas Movies':('6a1610bebdf296985fd95603-5efe1de7e0b3f5003f5d9f8e','USBD700011RE'),
 'Home.Made.Nation':('6a1610bebdf296985fd95603-5f74cf82ffbe690040689f47','USAJ3000006X5'),
}
GUIDE_URLS=('https://i.mjh.nz/Plex/us.xml.gz','https://i.mjh.nz/SamsungTVPlus/us.xml.gz')
GUIDE_REFRESH=900
GUIDE_MAX_AGE=1800
guide_rows=({}, {})
guide_observed_at=0.0
guide_fault=None
year_cache=OrderedDict()
YEAR_CACHE_FILE=ROOT/'programme-years.json'
YEAR_CACHE_LIMIT=2048
YEAR_POSITIVE_TTL=7*86400
YEAR_NEGATIVE_TTL=6*3600
YEAR_API='https://v3-cinemeta.strem.io'
year_fault=None
year_last_lookup=0.0

def release_year(value):
 if type(value) is int:year=value
 elif isinstance(value,str) and re.fullmatch(r'\d{4}(?:[–—-]\d{0,4})?',value.strip()):year=int(value.strip()[:4])
 else:return None
 return year if 1888<=year<=datetime.datetime.now(datetime.timezone.utc).year+1 else None
def title_year(title):
 match=re.search(r'\s+\((\d{4})\)(value):
 return re.sub(r'[^a-z0-9]','',value.casefold())
def guide_time(value):
 try:return datetime.datetime.strptime(value,'%Y%m%d%H%M%S %z').timestamp()
 except (TypeError,ValueError,OverflowError):return None
def parse_guide(data,ids,now):
 if len(data)>40*1024*1024:raise ValueError('Guide too large')
 # The two reviewed XMLTV feeds contain exactly this harmless declaration.
 # Strip it before parsing; never fetch the external DTD.
 data=data.replace(b'<!DOCTYPE tv SYSTEM "xmltv.dtd">',b'',1)
 upper=data.upper()
 if b'<!DOCTYPE' in upper or b'<!ENTITY' in upper:raise ValueError('Unsupported guide document')
 root=ET.fromstring(data)
 if root.tag!='tv':raise ValueError('Invalid guide root')
 out={}
 for p in root.findall('programme'):
  key=p.get('channel')
  if key not in ids:continue
  start=guide_time(p.get('start'));end=guide_time(p.get('stop'))
  title=programme_text(p.findtext('title'))
  if not title or start is None or end is None or not 0<end-start<=86400 or end<=now-60 or start>now+86400:continue
  rows=out.setdefault(key,[])
  if len(rows)>=256:raise ValueError('Guide channel limit exceeded')
  rows.append((start,end,title,programme_text((p.findtext('desc') or '')[:300]),programme_text(p.findtext('sub-title'))))
 return out
def fetch_guide(url,ids,now):
 req=urllib.request.Request(validate(url),headers={'User-Agent':'Jealousfin-EPG/1.0'})
 with opener.open(req,timeout=10) as f:raw=f.read(8*1024*1024+1)
 if len(raw)>8*1024*1024:raise ValueError('Compressed guide too large')
 with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:data=f.read(40*1024*1024+1)
 return parse_guide(data,ids,now)
def guide_worker(stop):
 global guide_rows,guide_observed_at,guide_fault
 while not stop.is_set():
  try:
   now=time.time()
   parsed=tuple(fetch_guide(url,{ids[i] for ids in GUIDE_CHANNELS.values()},now) for i,url in enumerate(GUIDE_URLS))
   with lock:guide_rows=parsed;guide_observed_at=now;guide_fault=None
  except Exception as error:
   with lock:guide_fault=type(error).__name__
   log('external programme refresh failed '+type(error).__name__)
  if stop.wait(GUIDE_REFRESH):return
def guide_snapshot(now):
 with lock:
  observed=guide_observed_at
  if not 0<=now-observed<=GUIDE_MAX_AGE:return {}
  first,second=guide_rows
  mapped=[(cid,GUIDE_CHANNELS[c['Name']]) for cid,c in channels.items() if c['Name'] in GUIDE_CHANNELS]
 out={}
 for cid,(aid,bid) in mapped:
  a=[r for r in first.get(aid,[]) if r[0]<=now<r[1]]
  b=[r for r in second.get(bid,[]) if r[0]<=now<r[1]]
  if len(a)!=1 or len(b)!=1:continue
  sa,ea,ta=a[0][:3];sb,eb,tb=b[0][:3]
  if guide_norm(ta)!=guide_norm(tb) or abs(sa-sb)>60 or abs(ea-eb)>60:continue
  if not guide_norm(ta):continue
  synopses=[r[3] for r in (a[0],b[0]) if len(r)>3 and r[3]]
  episodes=[r[4] for r in (a[0],b[0]) if len(r)>4 and r[4]]
  out[cid]={'title':ta,'source':'corroborated-external-schedule','observedAt':observed,'start':max(sa,sb),'end':min(ea,eb),'guideSynopsis':' '.join(dict.fromkeys(synopses))[:600],'guideEpisode':episodes[0] if episodes else ''}
 return out

def programme_text(value):
 if not isinstance(value,str):return ''
 value=' '.join(value.split())
 if not value or len(value)>300 or re.search(r'(?i)https?://|api_key=|token=',value):return ''
 return value

def parse_programme(row):
 # Provider clock strings are unzoned. Do not manufacture start/end dates.
 description=row.get('description')
 if not isinstance(description,str) or len(description)>8192:description=''
 release=row.get('releaseInfo')
 if not isinstance(release,str):release=''
 current=''
 next_title=''
 end_clock=''
 next_clock=''
 for line in description.splitlines():
  if line.startswith('Now: '):
   value=line[5:].strip()
   match=re.fullmatch(r'(.+?) \(until ((?:[01]\d|2[0-3]):[0-5]\d)\)',value)
   current=programme_text(match[1] if match else value)
   if match:end_clock=match[2]
  elif line.startswith('Next: '):
   value=line[6:].strip()
   match=re.fullmatch(r'(.+?) at ((?:[01]\d|2[0-3]):[0-5]\d)',value)
   next_title=programme_text(match[1] if match else value)
   if match:next_clock=match[2]
 release_title=programme_text(release[6:]) if release.startswith('Now · ') else ''
 if not current:current=release_title
 if not current or (release_title and current!=release_title):return None
 if current.casefold() in ('unknown','no information','no programme information','no program information','n/a','live tv','tba','to be announced'):return None
 if current.casefold()==str(row.get('name','')).strip().casefold():return None
 return {'title':current,'nextTitle':next_title,'providerEndClock':end_clock,'providerNextClock':next_clock}

def update_programmes(raw,observed=None):
 global programme_rows,programme_observed_at,programme_fault
 now=time.time() if observed is None else observed
 # Metadata refresh is independent from catalogue identity, policy and playback.
 valid={}
 for source_id,row in raw.items():
  if not isinstance(row,dict):continue
  cid=hashlib.md5(source_id.encode()).hexdigest()
  if cid not in sources:continue
  parsed=parse_programme(row)
  if parsed:valid[cid]=parsed
 with lock:
  programme_rows=valid;programme_observed_at=now;programme_fault=None

def programme_snapshot(now=None):
 now=time.time() if now is None else now
 result=guide_snapshot(now)
 with lock:
  if 0<=now-programme_observed_at<=PROGRAMME_MAX_AGE:
   for cid,row in programme_rows.items():
    evidence=result.get(cid,{})
    primary=dict(row,source='provider-catalogue',observedAt=programme_observed_at)
    if evidence and guide_norm(evidence['title'])==guide_norm(row['title']):
     for key in ('guideSynopsis','guideEpisode'):
      if evidence.get(key):primary[key]=evidence[key]
    result[cid]=primary
 return result

def programme_item(cid,row):
 # The provider supplies a title and an unzoned end clock, not a reliable
 # UTC schedule. Keep the standard programme dates absent and expose evidence.
 title=row['title']
 year=cached_year(row)
 display=title if not year or title_year(title)[1] else title+' ('+str(year)+')'
 evidence={k:v for k,v in row.items() if k not in ('guideSynopsis','guideEpisode')}
 if year:evidence['year']=year
 item={'Id':hashlib.sha256((cid+'\0'+title).encode()).hexdigest()[:32],
       'Name':display,'ChannelId':cid,'Type':'Program',
       'JealousfinProgramme':evidence}
 if year:item['ProductionYear']=year
 if row.get('source')=='corroborated-external-schedule':
  item['Name']='Scheduled: '+display
  item['StartDate']=datetime.datetime.fromtimestamp(row['start'],datetime.timezone.utc).isoformat()
  item['EndDate']=datetime.datetime.fromtimestamp(row['end'],datetime.timezone.utc).isoformat()
 return item

def programme_worker(stop):
 global programme_fault
 while not stop.wait(PROGRAMME_REFRESH):
  try:
   raw=catalogue()
   if len(raw)<len(sources)*.9:raise ValueError('Incomplete programme refresh')
   update_programmes(raw)
  except Exception as error:
   with lock:programme_fault=type(error).__name__
   log('programme refresh failed '+type(error).__name__)

def initialize_catalogue():
 global catalogue_source,catalogue_fault
 cache=ROOT/'catalogue-metadata.json'
 prior=None
 fresh=None
 if cache.exists():
  data=json.loads(cache.read_text())
  if isinstance(data,dict) and len(data)>=100 and all(isinstance(k,str) and k.startswith('nltv:') and isinstance(v,dict) and isinstance(v.get('name'),str) for k,v in data.items()):
   prior=data
 try:
  fresh=catalogue()
  if prior and len(fresh)<len(prior)*.9:raise ValueError('Catalogue unexpectedly contracted')
  # Only metadata is retained; no stream URLs or request credentials.
  raw={k:{'name':v['name'],'genres':[x for x in v.get('genres',[]) if isinstance(x,str)] if isinstance(v.get('genres'),list) else []} for k,v in fresh.items()}
  atomic_json(cache,raw);catalogue_source='provider';catalogue_fault=None
 except Exception as error:
  if prior is None:raise
  fresh=None;raw=prior;catalogue_source='cached';catalogue_fault=type(error).__name__
  log('catalogue fallback '+catalogue_fault)
 for i,(source_id,row) in enumerate(raw.items(),1):
  cid=hashlib.md5(source_id.encode()).hexdigest()
  genres=row.get('genres',[])
  if not isinstance(genres,list):genres=[]
  channels[cid]={'Id':cid,'Name':row['name'],'ChannelNumber':str(i),'Type':'TvChannel','Genres':[v for v in genres if isinstance(v,str)],'Tags':[],'UserData':{'IsFavorite':False}}
  sources[cid]=source_id
 if fresh is not None:update_programmes(fresh)
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
  atomic_json(ROTATION_FILE,updated)
  rotation.update(updated)
 with policy_connect(ROOT/'policy.sqlite3') as db:
  db.execute('UPDATE channel SET last_attempt=max(last_attempt,?) WHERE id=?',(time.time(),cid))
def policy_rows():
 # No false readiness marker or silent fail-open when policy storage fails.
 with sqlite3.connect((ROOT/'policy.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=1) as db:
  if not db.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():raise RuntimeError("Policy migration pending")
  columns={r[1] for r in db.execute("PRAGMA table_info(channel)")}
  policy_penalty(db)
  approval="approved_at" if "approved_at" in columns else "0"
  success="last_success" if "last_success" in columns else "0"
  rows=db.execute("SELECT id,last_attempt,hiatus_until,banned,failures,last_failure,reason,sustained,"+approval+","+success+" FROM channel").fetchall()
 result={r[0]:dict(lastAttempt=max(r[1],r[5]),hiatusUntil=r[2],banned=bool(r[3]),failures=r[4],lastFailure=r[5],reason=r[6],successes=r[7],approvedAt=r[8],lastSuccess=r[9]) for r in rows if r[0] in channels}
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
  row=db.execute("SELECT value FROM meta WHERE key='surf_resume'").fetchone()
  resume=json.loads(row[0]) if row else None
  if not isinstance(resume,dict) or resume.get('channelID') not in snapshot:resume=None
 return {"revision":"sqlite-policy-v2","channels":snapshot,"penalty":{"baseDays":days,"banAfter":ban},"resume":resume}
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
   return self.reply(200,{'channels':len(channels),'requests':counts,'resources':len(resources),'policyRevision':'sqlite-policy-v2','policyReady':ready,'policyChannels':len(snapshot),'configurationRevision':gateway_config['revision'],'configurationFault':config_fault,'resourceLimit':RESOURCE_LIMIT,'segmentCacheEntries':len(segment_cache),'segmentCacheBytes':segment_cache_bytes,'favorites':len(favorites),'catalogueSource':catalogue_source,'catalogueFault':catalogue_fault,'programmeCount':len(programme_snapshot()),'programmeObservedAt':programme_observed_at,'programmeFault':programme_fault,'externalProgrammeCount':len(guide_snapshot(time.time())),'externalProgrammeObservedAt':guide_observed_at,'externalProgrammeFault':guide_fault,'programmeYearCount':sum(cached_year(row) is not None for row in programme_snapshot().values()),'programmeYearFault':year_fault,'programmeYearLastLookup':year_last_lookup,'deliveryQuality':quality_summary()})
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
      remove_resource(rid);entry=None
     else:
      resources.move_to_end(rid);resource_touched[rid]=now;active_channels[entry[0]]=now+gateway_config['activeProtectionSeconds']
      correlation=resource_correlation.get(rid,correlation)
   if not entry:return self.reply(404,{})
   cid,url=entry
   started=time.monotonic();kind='segment' if rid in segment_resources else 'other'
   try:
    rg=self.headers.get('Range')
    if rg and not re.fullmatch(r'bytes=\d*-\d*',rg):return self.reply(416,{})
    (data,headers,status,final),cache_hit=fetch_media(rid,url,rg)
    record_delivery(cid,kind,time.monotonic()-started,len(data),cache_hit)
    if data.lstrip(b'\xef\xbb\xbf \r\n').startswith(b'#EXTM3U'):
     if len(data)>MAX_PLAYLIST:return self.reply(502,{})
     return self.reply(200,rewrite(data,final,cid,correlation),'application/vnd.apple.mpegurl')
    extra={k:headers[k] for k in ('Content-Range','Accept-Ranges') if headers.get(k)}
    return self.reply(status,data,headers.get('Content-Type','application/octet-stream'),extra)
   except urllib.error.HTTPError as e:
    record_delivery(cid,kind,time.monotonic()-started,0,error=True);record_source_error(cid,url,'media','HTTP '+str(e.code),correlation);log('upstream HTTP '+str(e.code));return self.reply(502,{})
   except Exception as e:
    record_delivery(cid,kind,time.monotonic()-started,0,error=True);record_source_error(cid,url,'media',type(e).__name__,correlation);log('upstream failure '+type(e).__name__);return self.reply(502,{})
  if not self.authorized(q):return self.reply(401,{})
  if path in ('/LiveTv/Channels','/Jealousfin/Policy'):
   try:snapshot=policy_rows()
   except (sqlite3.Error,OSError,RuntimeError,ValueError):return self.reply(503,{'JealousfinPolicyError':'unavailable'})
   if path=='/Jealousfin/Policy':return self.reply(200,policy_payload(snapshot))
   include=q.get('JealousfinIncludePolicy',['false'])[0]=='true'
   with lock:out=[dict(c,UserData={'IsFavorite':cid in favorites}) for cid,c in ordered_catalogue(snapshot,include)]
   programmes=programme_snapshot()
   for item in out:
    row=programmes.get(item['Id'])
    if row:item['CurrentProgram']=programme_item(item['Id'],row)
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
  if path=='/LiveTv/Programs':
   # The current-title feed is not a full EPG. For queries needing a real
   # time range, do not invent times; the client can use CurrentProgram.
   selected=set(','.join(q.get('ChannelIds',[])).split(','))-{''}
   out=[programme_item(cid,row) for cid,row in programme_snapshot().items() if not selected or cid in selected]
   return self.reply(200,{'Items':out,'TotalRecordCount':len(out),'JealousfinCurrentOnly':True})
  if path=='/LiveTv/Recordings':return self.reply(200,{'Items':[],'TotalRecordCount':0})
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
   if self.command not in ('POST','DELETE'):return self.reply(405,{})
   set_favorite(m[1],self.command=='POST')
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
 load_favorites()
 server=Server((HOST,PORT),Handler)
 (ROOT/'trial-info.json').write_text(json.dumps({'url':f'http://{HOST}:{PORT}','pid':os.getpid(),'expiresAt':None,'channels':[{'id':x,'name':c['Name']} for x,c in channels.items()]}))
 stop=threading.Event()
 worker=threading.Thread(target=programme_worker,args=(stop,),name='programme-refresh',daemon=True);worker.start()
 guide=threading.Thread(target=guide_worker,args=(stop,),name='external-guide-refresh',daemon=True);guide.start()
 years=threading.Thread(target=year_worker,args=(stop,),name='programme-year-refresh',daemon=True);years.start()
 log('READY channels='+str(len(channels)))
 try:server.serve_forever(poll_interval=.5)
 finally:
  stop.set();server.server_close()
  try:(ROOT/'trial-info.json').unlink()
  except FileNotFoundError:pass
if __name__=='__main__':main()
,title)
 if match and release_year(match[1]):return title[:match.start()].strip(),int(match[1])
 return title,None
def year_key(row):
 title,_=title_year(row['title'])
 return hashlib.sha256(json.dumps([guide_norm(title),row.get('guideSynopsis',''),row.get('guideEpisode','')],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def load_year_cache():
 try:
  if not YEAR_CACHE_FILE.exists():return
  if YEAR_CACHE_FILE.stat().st_size>1024*1024:raise ValueError('Year cache too large')
  raw=json.loads(YEAR_CACHE_FILE.read_text())
  if not isinstance(raw,dict):raise ValueError('Invalid year cache')
  now=time.time()
  with lock:
   for key,row in list(raw.items())[-YEAR_CACHE_LIMIT:]:
    if isinstance(key,str) and re.fullmatch(r'[a-f0-9]{64}',key) and isinstance(row,dict) and type(row.get('expires')) in (int,float) and now<row['expires']<=now+YEAR_POSITIVE_TTL and (row.get('year') is None or release_year(row.get('year'))):
     year_cache[key]=row
 except (ValueError,OSError,TypeError) as error:log('year cache ignored '+type(error).__name__)
def cached_year(row,now=None):
 _,explicit=title_year(row['title'])
 if explicit:return explicit
 now=time.time() if now is None else now
 with lock:
  entry=year_cache.get(year_key(row))
  return entry.get('year') if entry and now<entry['expires'] else None
def lookup_json(path):
 req=urllib.request.Request(validate(YEAR_API+path),headers={'User-Agent':'Jealousfin-Programme-Metadata/1.0','Accept':'application/json'})
 with opener.open(req,timeout=5) as f:data=f.read(512*1024+1)
 if len(data)>512*1024:raise ValueError('Metadata response too large')
 return json.loads(data)
def evidence_words(value):
 return re.findall(r"[a-z0-9]+",value.casefold()) if isinstance(value,str) else []
def synopsis_match(synopsis,meta):
 words=evidence_words(synopsis);desc=evidence_words(meta.get('description'))
 if len(words)>=7 and len(desc)>=7:
  phrases={tuple(words[i:i+7]) for i in range(len(words)-6)}
  if any(tuple(desc[i:i+7]) in phrases for i in range(len(desc)-6)):return True
 if not isinstance(meta.get('cast'),list):return False
 joined=' '+' '.join(words)+' '
 names={' '.join(evidence_words(name)) for name in meta['cast'] if isinstance(name,str)}
 return sum(1 for name in names if len(name.split())>=2 and ' '+name+' ' in joined)>=2
def lookup_year(row):
 title,explicit=title_year(row['title'])
 if explicit:return explicit
 if not title or len(title)>180:return None
 norm=guide_norm(title)
 if len(norm)<3:return None
 candidates={}
 for kind in ('movie','series'):
  result=lookup_json('/catalog/'+kind+'/top/search='+urllib.parse.quote(title,safe='')+'.json')
  metas=result.get('metas')
  if not isinstance(metas,list):raise ValueError('Invalid metadata search')
  for meta in metas:
   if not isinstance(meta,dict) or not isinstance(meta.get('name'),str) or guide_norm(meta['name'])!=norm:continue
   mid=meta.get('id')
   if not isinstance(mid,str) or not re.fullmatch(r'tt\d{5,12}',mid):continue
   candidates[mid]=(kind,release_year(meta.get('releaseInfo')))
 if len(candidates)==1:return next(iter(candidates.values()))[1]
 synopsis=row.get('guideSynopsis','')
 if not synopsis or not 1<len(candidates)<=6:return None
 positive=[]
 for mid,(kind,year) in candidates.items():
  if not year:continue
  try:
   meta=lookup_json('/meta/'+kind+'/'+mid+'.json').get('meta',{})
   if isinstance(meta,dict) and synopsis_match(synopsis,meta):positive.append(year)
  except urllib.error.HTTPError as error:
   if error.code!=404:raise
 return positive[0] if len(positive)==1 else None
def year_worker(stop):
 global year_fault,year_last_lookup
 load_year_cache()
 while not stop.is_set():
  try:
   now=time.time()
   rows=programme_snapshot(now)
   with lock:recent=dict(active_channels)
   ordered=sorted(rows.items(),key=lambda item:recent.get(item[0],0),reverse=True)
   selected=None
   for cid,row in ordered:
    if cached_year(row,now):continue
    key=year_key(row)
    with lock:entry=year_cache.get(key)
    if entry and now<entry['expires']:continue
    selected=(key,row);break
   if selected:
    key,row=selected
    year=lookup_year(row)
    with lock:
     year_cache[key]={'year':year,'expires':now+(YEAR_POSITIVE_TTL if year else YEAR_NEGATIVE_TTL)}
     year_cache.move_to_end(key)
     while len(year_cache)>YEAR_CACHE_LIMIT:year_cache.popitem(last=False)
     snapshot=dict(year_cache);year_last_lookup=time.time();year_fault=None
    atomic_json(YEAR_CACHE_FILE,snapshot)
   if stop.wait(3 if selected else 15):return
  except Exception as error:
   with lock:year_fault=type(error).__name__
   log('programme year lookup failed '+type(error).__name__)
   if stop.wait(60):return

def guide_norm(value):
 return re.sub(r'[^a-z0-9]','',value.casefold())
def guide_time(value):
 try:return datetime.datetime.strptime(value,'%Y%m%d%H%M%S %z').timestamp()
 except (TypeError,ValueError,OverflowError):return None
def parse_guide(data,ids,now):
 if len(data)>40*1024*1024:raise ValueError('Guide too large')
 # The two reviewed XMLTV feeds contain exactly this harmless declaration.
 # Strip it before parsing; never fetch the external DTD.
 data=data.replace(b'<!DOCTYPE tv SYSTEM "xmltv.dtd">',b'',1)
 upper=data.upper()
 if b'<!DOCTYPE' in upper or b'<!ENTITY' in upper:raise ValueError('Unsupported guide document')
 root=ET.fromstring(data)
 if root.tag!='tv':raise ValueError('Invalid guide root')
 out={}
 for p in root.findall('programme'):
  key=p.get('channel')
  if key not in ids:continue
  start=guide_time(p.get('start'));end=guide_time(p.get('stop'))
  title=programme_text(p.findtext('title'))
  if not title or start is None or end is None or not 0<end-start<=86400 or end<=now-60 or start>now+86400:continue
  rows=out.setdefault(key,[])
  if len(rows)>=256:raise ValueError('Guide channel limit exceeded')
  rows.append((start,end,title))
 return out
def fetch_guide(url,ids,now):
 req=urllib.request.Request(validate(url),headers={'User-Agent':'Jealousfin-EPG/1.0'})
 with opener.open(req,timeout=10) as f:raw=f.read(8*1024*1024+1)
 if len(raw)>8*1024*1024:raise ValueError('Compressed guide too large')
 with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:data=f.read(40*1024*1024+1)
 return parse_guide(data,ids,now)
def guide_worker(stop):
 global guide_rows,guide_observed_at,guide_fault
 while not stop.is_set():
  try:
   now=time.time()
   parsed=tuple(fetch_guide(url,{ids[i] for ids in GUIDE_CHANNELS.values()},now) for i,url in enumerate(GUIDE_URLS))
   with lock:guide_rows=parsed;guide_observed_at=now;guide_fault=None
  except Exception as error:
   with lock:guide_fault=type(error).__name__
   log('external programme refresh failed '+type(error).__name__)
  if stop.wait(GUIDE_REFRESH):return
def guide_snapshot(now):
 with lock:
  observed=guide_observed_at
  if not 0<=now-observed<=GUIDE_MAX_AGE:return {}
  first,second=guide_rows
  mapped=[(cid,GUIDE_CHANNELS[c['Name']]) for cid,c in channels.items() if c['Name'] in GUIDE_CHANNELS]
 out={}
 for cid,(aid,bid) in mapped:
  a=[r for r in first.get(aid,[]) if r[0]<=now<r[1]]
  b=[r for r in second.get(bid,[]) if r[0]<=now<r[1]]
  if len(a)!=1 or len(b)!=1:continue
  sa,ea,ta=a[0];sb,eb,tb=b[0]
  if guide_norm(ta)!=guide_norm(tb) or abs(sa-sb)>60 or abs(ea-eb)>60:continue
  if not guide_norm(ta):continue
  out[cid]={'title':ta,'source':'corroborated-external-schedule','observedAt':observed,'start':max(sa,sb),'end':min(ea,eb)}
 return out

def programme_text(value):
 if not isinstance(value,str):return ''
 value=' '.join(value.split())
 if not value or len(value)>300 or re.search(r'(?i)https?://|api_key=|token=',value):return ''
 return value

def parse_programme(row):
 # Provider clock strings are unzoned. Do not manufacture start/end dates.
 description=row.get('description')
 if not isinstance(description,str) or len(description)>8192:description=''
 release=row.get('releaseInfo')
 if not isinstance(release,str):release=''
 current=''
 next_title=''
 end_clock=''
 next_clock=''
 for line in description.splitlines():
  if line.startswith('Now: '):
   value=line[5:].strip()
   match=re.fullmatch(r'(.+?) \(until ((?:[01]\d|2[0-3]):[0-5]\d)\)',value)
   current=programme_text(match[1] if match else value)
   if match:end_clock=match[2]
  elif line.startswith('Next: '):
   value=line[6:].strip()
   match=re.fullmatch(r'(.+?) at ((?:[01]\d|2[0-3]):[0-5]\d)',value)
   next_title=programme_text(match[1] if match else value)
   if match:next_clock=match[2]
 release_title=programme_text(release[6:]) if release.startswith('Now · ') else ''
 if not current:current=release_title
 if not current or (release_title and current!=release_title):return None
 if current.casefold() in ('unknown','no information','no programme information','no program information','n/a','live tv','tba','to be announced'):return None
 if current.casefold()==str(row.get('name','')).strip().casefold():return None
 return {'title':current,'nextTitle':next_title,'providerEndClock':end_clock,'providerNextClock':next_clock}

def update_programmes(raw,observed=None):
 global programme_rows,programme_observed_at,programme_fault
 now=time.time() if observed is None else observed
 # Metadata refresh is independent from catalogue identity, policy and playback.
 valid={}
 for source_id,row in raw.items():
  if not isinstance(row,dict):continue
  cid=hashlib.md5(source_id.encode()).hexdigest()
  if cid not in sources:continue
  parsed=parse_programme(row)
  if parsed:valid[cid]=parsed
 with lock:
  programme_rows=valid;programme_observed_at=now;programme_fault=None

def programme_snapshot(now=None):
 now=time.time() if now is None else now
 result=guide_snapshot(now)
 with lock:
  if 0<=now-programme_observed_at<=PROGRAMME_MAX_AGE:
   result.update({cid:dict(row,source='provider-catalogue',observedAt=programme_observed_at) for cid,row in programme_rows.items()})
 return result

def programme_item(cid,row):
 # The provider supplies a title and an unzoned end clock, not a reliable
 # UTC schedule. Keep the standard programme dates absent and expose evidence.
 title=row['title']
 item={'Id':hashlib.sha256((cid+'\0'+title).encode()).hexdigest()[:32],
       'Name':title,'ChannelId':cid,'Type':'Program',
       'JealousfinProgramme':dict(row)}
 if row.get('source')=='corroborated-external-schedule':
  item['Name']='Scheduled: '+title
  item['StartDate']=datetime.datetime.fromtimestamp(row['start'],datetime.timezone.utc).isoformat()
  item['EndDate']=datetime.datetime.fromtimestamp(row['end'],datetime.timezone.utc).isoformat()
 return item

def programme_worker(stop):
 global programme_fault
 while not stop.wait(PROGRAMME_REFRESH):
  try:
   raw=catalogue()
   if len(raw)<len(sources)*.9:raise ValueError('Incomplete programme refresh')
   update_programmes(raw)
  except Exception as error:
   with lock:programme_fault=type(error).__name__
   log('programme refresh failed '+type(error).__name__)

def initialize_catalogue():
 global catalogue_source,catalogue_fault
 cache=ROOT/'catalogue-metadata.json'
 prior=None
 fresh=None
 if cache.exists():
  data=json.loads(cache.read_text())
  if isinstance(data,dict) and len(data)>=100 and all(isinstance(k,str) and k.startswith('nltv:') and isinstance(v,dict) and isinstance(v.get('name'),str) for k,v in data.items()):
   prior=data
 try:
  fresh=catalogue()
  if prior and len(fresh)<len(prior)*.9:raise ValueError('Catalogue unexpectedly contracted')
  # Only metadata is retained; no stream URLs or request credentials.
  raw={k:{'name':v['name'],'genres':[x for x in v.get('genres',[]) if isinstance(x,str)] if isinstance(v.get('genres'),list) else []} for k,v in fresh.items()}
  atomic_json(cache,raw);catalogue_source='provider';catalogue_fault=None
 except Exception as error:
  if prior is None:raise
  fresh=None;raw=prior;catalogue_source='cached';catalogue_fault=type(error).__name__
  log('catalogue fallback '+catalogue_fault)
 for i,(source_id,row) in enumerate(raw.items(),1):
  cid=hashlib.md5(source_id.encode()).hexdigest()
  genres=row.get('genres',[])
  if not isinstance(genres,list):genres=[]
  channels[cid]={'Id':cid,'Name':row['name'],'ChannelNumber':str(i),'Type':'TvChannel','Genres':[v for v in genres if isinstance(v,str)],'Tags':[],'UserData':{'IsFavorite':False}}
  sources[cid]=source_id
 if fresh is not None:update_programmes(fresh)
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
  atomic_json(ROTATION_FILE,updated)
  rotation.update(updated)
 with policy_connect(ROOT/'policy.sqlite3') as db:
  db.execute('UPDATE channel SET last_attempt=max(last_attempt,?) WHERE id=?',(time.time(),cid))
def policy_rows():
 # No false readiness marker or silent fail-open when policy storage fails.
 with sqlite3.connect((ROOT/'policy.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=1) as db:
  if not db.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():raise RuntimeError("Policy migration pending")
  columns={r[1] for r in db.execute("PRAGMA table_info(channel)")}
  policy_penalty(db)
  approval="approved_at" if "approved_at" in columns else "0"
  success="last_success" if "last_success" in columns else "0"
  rows=db.execute("SELECT id,last_attempt,hiatus_until,banned,failures,last_failure,reason,sustained,"+approval+","+success+" FROM channel").fetchall()
 result={r[0]:dict(lastAttempt=max(r[1],r[5]),hiatusUntil=r[2],banned=bool(r[3]),failures=r[4],lastFailure=r[5],reason=r[6],successes=r[7],approvedAt=r[8],lastSuccess=r[9]) for r in rows if r[0] in channels}
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
  row=db.execute("SELECT value FROM meta WHERE key='surf_resume'").fetchone()
  resume=json.loads(row[0]) if row else None
  if not isinstance(resume,dict) or resume.get('channelID') not in snapshot:resume=None
 return {"revision":"sqlite-policy-v2","channels":snapshot,"penalty":{"baseDays":days,"banAfter":ban},"resume":resume}
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
   return self.reply(200,{'channels':len(channels),'requests':counts,'resources':len(resources),'policyRevision':'sqlite-policy-v2','policyReady':ready,'policyChannels':len(snapshot),'configurationRevision':gateway_config['revision'],'configurationFault':config_fault,'resourceLimit':RESOURCE_LIMIT,'segmentCacheEntries':len(segment_cache),'segmentCacheBytes':segment_cache_bytes,'favorites':len(favorites),'catalogueSource':catalogue_source,'catalogueFault':catalogue_fault,'programmeCount':len(programme_snapshot()),'programmeObservedAt':programme_observed_at,'programmeFault':programme_fault,'externalProgrammeCount':len(guide_snapshot(time.time())),'externalProgrammeObservedAt':guide_observed_at,'externalProgrammeFault':guide_fault,'deliveryQuality':quality_summary()})
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
      remove_resource(rid);entry=None
     else:
      resources.move_to_end(rid);resource_touched[rid]=now;active_channels[entry[0]]=now+gateway_config['activeProtectionSeconds']
      correlation=resource_correlation.get(rid,correlation)
   if not entry:return self.reply(404,{})
   cid,url=entry
   started=time.monotonic();kind='segment' if rid in segment_resources else 'other'
   try:
    rg=self.headers.get('Range')
    if rg and not re.fullmatch(r'bytes=\d*-\d*',rg):return self.reply(416,{})
    (data,headers,status,final),cache_hit=fetch_media(rid,url,rg)
    record_delivery(cid,kind,time.monotonic()-started,len(data),cache_hit)
    if data.lstrip(b'\xef\xbb\xbf \r\n').startswith(b'#EXTM3U'):
     if len(data)>MAX_PLAYLIST:return self.reply(502,{})
     return self.reply(200,rewrite(data,final,cid,correlation),'application/vnd.apple.mpegurl')
    extra={k:headers[k] for k in ('Content-Range','Accept-Ranges') if headers.get(k)}
    return self.reply(status,data,headers.get('Content-Type','application/octet-stream'),extra)
   except urllib.error.HTTPError as e:
    record_delivery(cid,kind,time.monotonic()-started,0,error=True);record_source_error(cid,url,'media','HTTP '+str(e.code),correlation);log('upstream HTTP '+str(e.code));return self.reply(502,{})
   except Exception as e:
    record_delivery(cid,kind,time.monotonic()-started,0,error=True);record_source_error(cid,url,'media',type(e).__name__,correlation);log('upstream failure '+type(e).__name__);return self.reply(502,{})
  if not self.authorized(q):return self.reply(401,{})
  if path in ('/LiveTv/Channels','/Jealousfin/Policy'):
   try:snapshot=policy_rows()
   except (sqlite3.Error,OSError,RuntimeError,ValueError):return self.reply(503,{'JealousfinPolicyError':'unavailable'})
   if path=='/Jealousfin/Policy':return self.reply(200,policy_payload(snapshot))
   include=q.get('JealousfinIncludePolicy',['false'])[0]=='true'
   with lock:out=[dict(c,UserData={'IsFavorite':cid in favorites}) for cid,c in ordered_catalogue(snapshot,include)]
   programmes=programme_snapshot()
   for item in out:
    row=programmes.get(item['Id'])
    if row:item['CurrentProgram']=programme_item(item['Id'],row)
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
  if path=='/LiveTv/Programs':
   # The current-title feed is not a full EPG. For queries needing a real
   # time range, do not invent times; the client can use CurrentProgram.
   selected=set(','.join(q.get('ChannelIds',[])).split(','))-{''}
   out=[programme_item(cid,row) for cid,row in programme_snapshot().items() if not selected or cid in selected]
   return self.reply(200,{'Items':out,'TotalRecordCount':len(out),'JealousfinCurrentOnly':True})
  if path=='/LiveTv/Recordings':return self.reply(200,{'Items':[],'TotalRecordCount':0})
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
   if self.command not in ('POST','DELETE'):return self.reply(405,{})
   set_favorite(m[1],self.command=='POST')
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
 load_favorites()
 server=Server((HOST,PORT),Handler)
 (ROOT/'trial-info.json').write_text(json.dumps({'url':f'http://{HOST}:{PORT}','pid':os.getpid(),'expiresAt':None,'channels':[{'id':x,'name':c['Name']} for x,c in channels.items()]}))
 stop=threading.Event()
 worker=threading.Thread(target=programme_worker,args=(stop,),name='programme-refresh',daemon=True);worker.start()
 guide=threading.Thread(target=guide_worker,args=(stop,),name='external-guide-refresh',daemon=True);guide.start()
 log('READY channels='+str(len(channels)))
 try:server.serve_forever(poll_interval=.5)
 finally:
  stop.set();server.server_close()
  try:(ROOT/'trial-info.json').unlink()
  except FileNotFoundError:pass
if __name__=='__main__':main()

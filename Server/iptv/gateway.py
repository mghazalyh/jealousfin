import datetime, email.utils, gzip, io, xml.etree.ElementTree as ET, http.client, sqlite3, base64, concurrent.futures, hashlib, hmac, http.server, ipaddress, json, os, queue, re, secrets, socket, subprocess, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from collections import OrderedDict, deque
from pathlib import Path
from contextlib import closing
import regional_guide
import episode_audio
import opencode_discovery
import programme_research
import source_profiles
import prototype_selector
import language_exclusions
import language_audio
import language_pending
import language_visits
import overlay_bridge
import programme_overlay
import schedule_episode
import schedule_evidence
import deployment_continuity
spanish_classification=language_exclusions
spanish_audio=language_audio
from policy_observer import connect as policy_connect, failure as policy_failure, penalty as policy_penalty

HOST=os.environ.get('JEALOUSFIN_BIND','127.0.0.1')
SOURCE_PROFILE=source_profiles.profile_name(os.environ)
PORT=source_profiles.gateway_port(os.environ,SOURCE_PROFILE)
MIN_CHANNELS=source_profiles.minimum_channels(os.environ,SOURCE_PROFILE)
ROOT=source_profiles.state_root(os.environ['JEALOUSFIN_STATE_DIR'],SOURCE_PROFILE)
ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
MEDIA_CONTINUITY=deployment_continuity.Continuity(ROOT)
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
                 'resourceLimit':10000,'activeProtectionSeconds':120,'sourceSelection':'round-robin','preferredHosts':{},
                 'metadataResearchEnabled':False,'metadataResearchModel':opencode_discovery.DEFAULT_MODEL,
                 'metadataResearchDailyBudget':None,'metadataResearchTimeoutSeconds':180,'metadataResearchVariant':opencode_discovery.DEFAULT_VARIANT,
                 'episodeAudioEnabled':False,
                 'ukManifest':None,'ukTrialProviderIds':[],'ukTrialEnabled':False,
                 'expandedManifest':None,'expandedTrialProviderIds':[],'expandedCataloguePageCount':6,'canadaManifest':None,'canadaTrialProviderIds':[],'canadaCataloguePageCount':1,'mediaEndpointAllowlist':{}}
gateway_config=dict(CONFIG_DEFAULTS)
config_fingerprint=None
config_checked_at=0.0
config_fault=None
# Manual Spanish classification is a separate membership filter, never a
# failure/ban/hiatus and never a mutation of rotation or health history.
SPANISH_REVISION='language-exclusions-v4'
SPANISH_UI_FIELD='spanishRequest'
SPANISH_UI_NODE='spanish-read'
SPANISH_FILE=ROOT/'language-exclusions.json'
LEGACY_SPANISH_FILE=ROOT/'spanish-classification.json'
spanish_state=spanish_classification.empty()
spanish_provider_ids=set()
spanish_manual_provider_ids=set()
spanish_auto_provider_ids=set()
spanish_fingerprint=None
spanish_checked_at=0.0
SPANISH_AUTO_CONTROL_FILE=ROOT/'language-auto-control.json'
LEGACY_SPANISH_AUTO_CONTROL_FILE=ROOT/'spanish-auto-control.json'
SPANISH_AUTO_REVISION='language-auto-control-v2'
SPANISH_EVIDENCE_THRESHOLD=language_exclusions.DEFAULT_THRESHOLD
SPANISH_AUTO_ENFORCEMENT=False
spanish_auto_control=False
spanish_auto_control_fingerprint=None
spanish_auto_control_checked=0.0
spanish_auto_manager=None
# The overlay publisher is additive: it only writes the existing hidden
# languageOverlay form field through the authorized device command transport.
overlay_bridge_state=None
overlay_bridge_fingerprint=None
overlay_bridge_fault=''
programme_overlay_bridge_state=None
programme_overlay_bridge_fingerprint=None
programme_overlay_fault=''
overlay_combined_stats={'attempts':0,'published':0,'accepted':0,'fallback':0,
                        'suppressed':0,'unavailable':0,'unverified':0,
                        'invalid':0,'noProgramme':0,'lastResult':None,'lastReason':None}
overlay_combined_evidence=None
overlay_init_lock=threading.Lock()
overlay_jobs=queue.Queue(maxsize=8)
OVERLAY_SETTLE_SECONDS=2.0
OVERLAY_PUMP_MIN_INTERVAL=1.0
OVERLAY_PUMP_QUEUE_TTL=10.0
OVERLAY_ADMISSION_CLOCK=time.monotonic
overlay_pump_lock=threading.Lock()
overlay_pump_pending=False
overlay_pump_queued_at=0.0
overlay_pump_last_request=float('-inf')
# Every admitted pump owns a token.  A pump only clears the coalescing flag
# when it is the owner being queued, evicted or completed.  Older in-flight
# pumps therefore cannot release a newer queued replacement.
overlay_pump_token=0
overlay_pump_tokens=set()
overlay_admission_stats={'pumpRequests':0,'pumpEnqueued':0,'pumpCoalesced':0,
 'pumpStale':0,'refreshRequests':0,'refreshEnqueued':0}
def _overlay_admission_now():
 try:return float(OVERLAY_ADMISSION_CLOCK())
 except (TypeError,ValueError):return time.monotonic()
def _overlay_pump_retire(token):
 # Deterministic ownership release.  None is the legacy untracked pump used by
 # direct enqueue callers; it never clears a tracked replacement.
 global overlay_pump_pending
 with overlay_pump_lock:
  if token is not None:overlay_pump_tokens.discard(token)
  overlay_pump_pending=bool(overlay_pump_tokens)
episode_probe_diagnostics={'checks':0,'accepted':0,'rejections':{},'last':None}
device_rotation_cache=None
device_rotation_checked=0.0
language_visit_ledger=None
MAINTENANCE_FILE=ROOT/'maintenance-window.json'
MAINTENANCE_IGNORED_FILE=ROOT/'maintenance-ignored.jsonl'
maintenance_state=None
maintenance_fingerprint=None
maintenance_checked_at=0.0
resource_correlation={}
resource_keys={}
segment_resources=set()
resource_bases={}
audio_playlists=set()
audio_segments=set()
map_resources={}
map_bases={}
key_resources={}
segment_keys={}
key_cache={}
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
 if type(result['metadataResearchEnabled']) is not bool:raise ValueError('Invalid metadata research toggle')
 if type(result['episodeAudioEnabled']) is not bool:raise ValueError('Invalid episode audio toggle')
 if not isinstance(result['metadataResearchModel'],str) or not opencode_discovery.MODEL_RE.fullmatch(result['metadataResearchModel']):raise ValueError('Invalid research model')
 if result['metadataResearchVariant'] not in (None,'low','high','max'):raise ValueError('Invalid research variant')
 manifest=result['ukManifest'];ids=result['ukTrialProviderIds'];enabled=result['ukTrialEnabled']
 if type(enabled) is not bool or not isinstance(ids,list) or len(ids)>100 or any(not isinstance(i,str) or not re.fullmatch(r'nltv:uk:[A-Za-z0-9_.-]{1,160}',i) for i in ids) or len(ids)!=len(set(ids)):raise ValueError('Invalid UK trial cohort')
 if manifest is not None and (not isinstance(manifest,str) or not re.fullmatch(r'https://ptv3-livetv-addon\.pages\.dev/[A-Za-z0-9_-]{1,4096}/manifest\.json',manifest)):raise ValueError('Invalid UK manifest')
 if (manifest is None)!= (not ids) or (enabled and not ids) or (SOURCE_PROFILE!='american' and (manifest or ids or enabled)):raise ValueError('Invalid UK trial configuration')
 extra=result['expandedManifest'];extra_ids=result['expandedTrialProviderIds']
 if not isinstance(extra_ids,list) or len(extra_ids)>1000 or any(not isinstance(i,str) or not re.fullmatch(r'nltv:(us|uk):[A-Za-z0-9_.-]{1,160}',i) for i in extra_ids) or len(extra_ids)!=len(set(extra_ids)) or set(extra_ids).intersection(ids):raise ValueError('Invalid expanded trial identities')
 if extra is not None and (not isinstance(extra,str) or not re.fullmatch(r'https://ptv3-livetv-addon\.pages\.dev/[A-Za-z0-9_-]{1,4096}/manifest\.json',extra)):raise ValueError('Invalid expanded manifest')
 if type(result['expandedCataloguePageCount']) is not int or not 1<=result['expandedCataloguePageCount']<=100:raise ValueError('Invalid expanded page count')
 if (extra is None)!=(not extra_ids) or (SOURCE_PROFILE!='american' and (extra or extra_ids)):raise ValueError('Invalid expanded trial configuration')
 ca=result['canadaManifest'];ca_ids=result['canadaTrialProviderIds']
 if not isinstance(ca_ids,list) or len(ca_ids)>1000 or any(not isinstance(i,str) or not re.fullmatch(r'nltv:ca:[A-Za-z0-9_.-]{1,160}',i) for i in ca_ids) or len(ca_ids)!=len(set(ca_ids)):raise ValueError('Invalid Canada trial identities')
 if ca is not None and (not isinstance(ca,str) or not re.fullmatch(r'https://ptv3-livetv-addon\.pages\.dev/[A-Za-z0-9_-]{1,4096}/manifest\.json',ca)):raise ValueError('Invalid Canada manifest')
 if (ca is None)!=(not ca_ids) or (SOURCE_PROFILE!='american' and (ca or ca_ids)):raise ValueError('Invalid Canada trial configuration')
 if type(result['canadaCataloguePageCount']) is not int or not 1<=result['canadaCataloguePageCount']<=100:raise ValueError('Invalid Canada page count')
 endpoints=result['mediaEndpointAllowlist']
 if not isinstance(endpoints,dict) or len(endpoints)>256:raise ValueError('Invalid media endpoint allowlist')
 for host,ports in endpoints.items():
  if not isinstance(host,str) or not re.fullmatch(r'[a-z0-9.-]{1,253}',host) or not isinstance(ports,list) or not ports or len(ports)>16 or any(type(p) is not int or not 1024<=p<=65535 for p in ports) or len(set(ports))!=len(ports):raise ValueError('Invalid media endpoint')
  try:address=ipaddress.ip_address(host)
  except ValueError:address=None
  if address is not None and not address.is_global:raise ValueError('Non-public media endpoint')
 # Legacy quota settings are accepted but ignored; research has no daily cap.
 result['metadataResearchDailyBudget']=None
 for key,lo,hi in [('metadataResearchTimeoutSeconds',10,300)]:
  if type(result[key]) is not int or not lo<=result[key]<=hi:raise ValueError('Invalid '+key)
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
class MediaCompatibilityError(ValueError):
 pass
def validate(url,media=False):
 p=urllib.parse.urlsplit(url)
 if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:
  raise ValueError('Unsupported upstream URL')
 port=p.port if p.port is not None else (443 if p.scheme=='https' else 80)
 if not media and port not in (80,443):raise ValueError('Unsupported upstream URL')
 if port not in (80,443) and not 1024<=port<=65535:raise ValueError('Unsupported upstream port')
 ips=socket.getaddrinfo(p.hostname,port,type=socket.SOCK_STREAM)
 if not ips or any(not ipaddress.ip_address(x[4][0]).is_global for x in ips):
  raise ValueError('Non-public upstream address')
 if port not in (80,443) and port not in gateway_config['mediaEndpointAllowlist'].get(p.hostname,[]):
  raise MediaCompatibilityError('Unconfigured public media endpoint')
 return url
class Redirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  return super().redirect_request(req,fp,code,msg,headers,validate(urllib.parse.urljoin(req.full_url,newurl)))
class MediaRedirect(Redirect):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  return urllib.request.HTTPRedirectHandler.redirect_request(self,req,fp,code,msg,headers,validate(urllib.parse.urljoin(req.full_url,newurl),media=True))
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),Redirect())
media_opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),MediaRedirect())
def fetch(url,limit,range_header=None):
 validate(url,media=True)
 headers={'User-Agent':'Jealousfin-IPTV-Trial/1.0','Accept':'*/*','Accept-Encoding':'identity'}
 if range_header:headers['Range']=range_header
 config=gateway_config
 for attempt in range(config['upstreamRetries']+1):
  try:
   with media_opener.open(urllib.request.Request(url,headers=headers),timeout=config['upstreamTimeoutSeconds']) as f:
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
 resource_bases.pop(rid,None);audio_playlists.discard(rid);audio_segments.discard(rid)
 key_resources.pop(rid,None);segment_keys.pop(rid,None);key_cache.pop(rid,None)
 base=map_bases.pop(rid,None)
 if base is not None and map_resources.get(base)==rid:map_resources.pop(base,None)
 for base,map_rid in list(map_resources.items()):
  if map_rid==rid:map_resources.pop(base,None)
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
 validate(url,media=True)
 with lock:
  key=(cid,url) if identity is None else (cid,url,identity);now=time.monotonic()
  if key in reverse and reverse[key] in resources:
   rid=reverse[key];resources.move_to_end(rid)
  else:
   if len(resources)>=RESOURCE_LIMIT:evict_resources(now,1)
   if len(resources)>=RESOURCE_LIMIT:raise RuntimeError("Resource registry saturated")
   rid=secrets.token_hex(16);resources[rid]=(cid,url);reverse[key]=rid;resource_keys[rid]=key
   if identity is not None:segment_resources.add(rid);resource_bases[rid]=identity[0]
   kind='segment' if identity is not None else ('playlist' if '.m3u8' in urllib.parse.urlsplit(url).path.lower() else 'media')
   MEDIA_CONTINUITY.record_handle(rid,cid,kind,time.time())
  resource_touched[rid]=now
  if correlation:resource_correlation[rid]=correlation
 return ('/malay/media/' if SOURCE_PROFILE=='malay' else '/media/')+TOKEN+'/'+rid+('.m3u8' if '.m3u8' in urllib.parse.urlsplit(url).path.lower() else '.bin')
MEDIA_RID=re.compile(r'/(?:malay/)?media/[^/]+/([a-f0-9]{32})(?:\.(?:m3u8|bin))?$')
def media_rid(url):
 match=MEDIA_RID.search(url)
 return match[1] if match else None
def rewrite(data,base,cid,correlation=None,audio_only=False):
 text=data.decode('utf-8-sig')
 if not text.lstrip().startswith('#EXTM3U'):raise ValueError('Invalid playlist')
 out=[];sequence=None;discontinuity=0;segment=False;byterange=False
 durations=[];discontinuity_tags=0;first_sequence=None
 current_key=None;key_method=None;encrypted=False
 for line in text.splitlines():
  if line.startswith('#'):
   tag=line
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
   def replace_uri(match):
    url=register(urllib.parse.urljoin(base,match[1]),cid,correlation)
    rid=media_rid(url)
    if rid and tag.startswith('#EXT-X-MEDIA:') and 'TYPE=AUDIO' in tag:audio_playlists.add(rid)
    elif rid and tag.startswith('#EXT-X-MAP:'):
     map_resources[base]=rid;map_bases[rid]=base
    elif rid and tag.startswith('#EXT-X-KEY:'):
     key_resources[rid]=base
    return 'URI="'+url+'"'
   line=re.sub(r'URI="([^"]+)"',replace_uri,line)
   if tag.startswith('#EXT-X-KEY:'):
    method=re.search(r'METHOD=([A-Za-z0-9-]+)',tag)
    iv=re.search(r'IV=0x([0-9A-Fa-f]+)',tag)
    label=method.group(1).upper() if method else ''
    key_method=label or key_method
    encrypted=encrypted or label not in ('','NONE')
    uri_match=re.search(r'URI="([^"]+)"',line)
    key_rid=media_rid(uri_match.group(1)) if uri_match else None
    current_key=(key_rid,iv.group(1) if iv else None) if key_rid and label and label!='NONE' else None
  elif line.strip():
   absolute=urllib.parse.urljoin(base,line.strip())
   identity=(base,discontinuity,sequence) if segment and sequence is not None and not byterange else None
   line=register(absolute,cid,correlation,identity)
   rid=media_rid(line)
   if rid and audio_only:audio_segments.add(rid)
   if rid and current_key:segment_keys[rid]={'keyRid':current_key[0],'iv':current_key[1],'sequence':sequence if segment else None}
   if segment and sequence is not None:sequence+=1
   segment=False;byterange=False
  out.append(line)
 if durations:
  with lock:
   manifest_quality[cid]={'playlistMediaSequence':first_sequence,'playlistSegments':len(durations),
                         'playlistDurationSeconds':round(sum(durations),3),
                         'playlistMinSegmentSeconds':min(durations),'playlistMaxSegmentSeconds':max(durations),
                         'playlistDiscontinuities':discontinuity_tags,'playlistEncrypted':bool(encrypted),
                         'playlistKeyMethod':key_method or '','playlistObservedAt':time.time()}
   manifest_quality.move_to_end(cid)
   while len(manifest_quality)>300:manifest_quality.popitem(last=False)
 return ('\n'.join(out)+'\n').encode()
def provider():
 base=os.environ['JEALOUSFIN_PROVIDER_URL'].rstrip('/')
 for suffix in ('/manifest.json','/playlist.m3u'):
  if base.endswith(suffix):base=base[:-len(suffix)].rstrip('/');break
 return validate(base)
PROVIDER=None
def provider_json(path,base=None):
 base=PROVIDER if base is None else base
 if base is None:raise RuntimeError('Provider is not initialized')
 url=validate(base+path)
 req=urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'Jealousfin-Catalogue-Diagnostics/1.0'})
 with opener.open(req,timeout=gateway_config['providerTimeoutSeconds']) as f:
  data=f.read(MAX_PLAYLIST+1)
  if len(data)>MAX_PLAYLIST:raise ValueError('Provider response too large')
  return json.loads(data)
def catalogue(base=None,minimum=None):
 found={}
 for skip in range(0,10000,100):
  path='/catalog/tv/nltv_channels'+('/skip='+str(skip) if skip else '')+'.json'
  response=provider_json(path) if base is None else provider_json(path,base)
  rows=response.get('metas') if isinstance(response,dict) else None
  if not isinstance(rows,list):raise ValueError('Invalid provider catalogue page')
  if not rows:break
  prior=len(found)
  for row in rows:
   if isinstance(row,dict) and isinstance(row.get('id'),str) and row['id'].startswith('nltv:') and isinstance(row.get('name'),str):found[row['id']]=row
  # The provider catalogue is paginated in groups of 100. A partial final
  # page is complete; do not make an unnecessary request beyond its end.
  if len(found)==prior or len(rows)<100:break
 if len(found)<(MIN_CHANNELS if minimum is None else minimum):raise ValueError('Incomplete channel catalogue')
 return found
channels={}
sources={}
uk_manifest_status={'observedAt':None,'channels':0,'matched':0,'fault':None}
def uk_base(config):
 url=config['ukManifest']
 return url[:-len('/manifest.json')] if url else None
def uk_cohort(config):
 return set(config['ukTrialProviderIds'])
expanded_manifest_status={'observedAt':None,'channels':0,'matched':0,'fault':None}
primary_source_ids=set()
def expanded_cohort(config):
 return set(config['expandedTrialProviderIds'])
def trial_cohort(config):
 return uk_cohort(config)|expanded_cohort(config)|canada_cohort(config)
def expanded_base(config):
 url=config['expandedManifest']
 return url[:-len('/manifest.json')] if url else None
def expanded_catalogue(config,cache_fallback=False):
 global expanded_manifest_status
 base=expanded_base(config)
 if base is None:return {}
 ids=expanded_cohort(config)
 cache=ROOT/'expanded-catalogue-metadata.json'
 try:
  raw=catalogue(base,1)
  selected={sid:row for sid,row in raw.items() if sid in ids}
  if set(selected)!=ids:raise ValueError('Incomplete expanded trial catalogue')
  if cache_fallback:atomic_json(cache,{sid:{'name':row['name'],'genres':row.get('genres',[])} for sid,row in selected.items()})
  expanded_manifest_status={'observedAt':time.time(),'channels':len(raw),'matched':len(selected),'fault':None}
  return selected
 except Exception as error:
  expanded_manifest_status=dict(expanded_manifest_status,fault=type(error).__name__)
  log('Expanded manifest refresh failed '+type(error).__name__)
  if cache_fallback and cache.exists():
   raw=json.loads(cache.read_text())
   if isinstance(raw,dict) and set(raw)==ids and all(isinstance(v,dict) and isinstance(v.get('name'),str) for v in raw.values()):
    expanded_manifest_status=dict(expanded_manifest_status,matched=len(raw),catalogueSource='cached')
    return raw
  return {}
canada_manifest_status={'observedAt':None,'channels':0,'matched':0,'fault':None}
def canada_cohort(config):
 return set(config.get('canadaTrialProviderIds',[]))
def canada_base(config):
 url=config.get('canadaManifest')
 return url[:-len('/manifest.json')] if url else None
def canada_catalogue(config,cache_fallback=False):
 global canada_manifest_status
 base=canada_base(config)
 if base is None:return {}
 ids=canada_cohort(config)
 cache=ROOT/'canada-catalogue-metadata.json'
 try:
  raw=catalogue(base,1)
  selected={sid:row for sid,row in raw.items() if sid in ids}
  if set(selected)!=ids:raise ValueError('Incomplete Canada trial catalogue')
  if cache_fallback:atomic_json(cache,{sid:{'name':row['name'],'genres':row.get('genres',[])} for sid,row in selected.items()})
  canada_manifest_status={'observedAt':time.time(),'channels':len(raw),'matched':len(selected),'fault':None}
  return selected
 except Exception as error:
  canada_manifest_status=dict(canada_manifest_status,fault=type(error).__name__)
  log('Canada manifest refresh failed '+type(error).__name__)
  if cache_fallback and cache.exists():
   try:
    raw=json.loads(cache.read_text())
    if isinstance(raw,dict) and set(raw)==ids and all(isinstance(v,dict) and isinstance(v.get('name'),str) for v in raw.values()):
     canada_manifest_status=dict(canada_manifest_status,matched=len(raw),catalogueSource='cached')
     return raw
   except (OSError,ValueError,TypeError):pass
  return {}
def uk_trial_projection(items,config):
 # Sports is a temporary native rotation alias; never change stored genres or IDs.
 if not config['ukTrialEnabled']:return items
 cohort=trial_cohort(config)
 out=[]
 for item in items:
  row=dict(item)
  for field in ('Genres','Tags'):
   row[field]=[v for v in item.get(field,[]) if isinstance(v,str) and 'sport' not in v.casefold()]
  if sources.get(item['Id']) in cohort:row['Tags'].append('Sports')
  out.append(row)
 return out
def uk_programmes(config):
 global uk_manifest_status
 base=uk_base(config)
 if base is None:return {}
 try:
  raw=catalogue(base,1)
  matched={sid:row for sid,row in raw.items() if sid in uk_cohort(config)}
  uk_manifest_status={'observedAt':time.time(),'channels':len(raw),'matched':len(matched),'fault':None}
  return matched
 except Exception as error:
  uk_manifest_status=dict(uk_manifest_status,fault=type(error).__name__)
  log('UK manifest refresh failed '+type(error).__name__)
  return {}
preferred_hosts={s[0]:s[2] for s in SAMPLES}
attempts={}
catalogue_source='uninitialized'
catalogue_fault=None
programme_rows={}
programme_observed_at=0.0
programme_fault=None
programme_refresh_failures=0
programme_last_attempt=0.0
programme_next_attempt=0.0
PROGRAMME_REFRESH=60
PROGRAMME_MAX_AGE=150
paged_programmes={}
paged_seen=set()
paged_faults={}
paged_due={}
paged_failures={}
paged_last_success=0.0
paged_origin_block_until=0.0
# Explicit distributor IDs confirmed in the November 25–26 guide audits.
# Exact provider identity, exact name, and two corroborating schedules are required.
# Do not match similarly named channels across regional feeds.
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
 "50 Cent Action":('6a1610bebdf296985fd95603-6827b9579beea89c38b6df55','US150000550'),
 "All Weddings WE tv":('6a1610bebdf296985fd95603-60de0520281b17002cd1d4c4','USBD1200005XR'),
 "At Home with Family Handyman":('6a1610bebdf296985fd95603-632270e3e67cf9cd9011fce1','USBD25000138Y'),
 "Billiard TV":('6a1610bebdf296985fd95603-6334a8e48a7b2a349d903b8c','USBD12000183X'),
 "Celebrity Name Game":('6a1610bebdf296985fd95603-640205c40e1daf16b299567e','US1900010IY'),
 "Designated Survivor":('6a1610bebdf296985fd95603-66aa9b7fd8fc59729226073e','US1800019OC'),
 "Dog Whisperer":('6a1610bebdf296985fd95603-652a8b001b5c74bc83cf727c','US3400001FX'),
 "Fubo Sports Network":('6a1610bebdf296985fd95603-5fc705f68aba71002d8b7b37','USAJ2200023Z0'),
 "MovieSphere":('6a1610bebdf296985fd95603-5fb84bc1b7ad2d002dcb735f','USBD17000117B'),
 "NBC Sports NOW":('6a1610bebdf296985fd95603-5fc70600598c41002da3dd43','USBD420002446'),
 "Nosey":('6a1610bebdf296985fd95603-5f0ff262d71dcb00449ec015','USAJ34000022E'),
 "PBS Nature":('6a1610bebdf296985fd95603-63506e7747b2355a27e76e7f','US1300007KZ'),
 "Scripps Sports Network":('6a1610bebdf296985fd95603-69b047eed42e70e462105dbb','US400221W2'),
 "SportsGrid":('6a1610bebdf296985fd95603-5ef11485d33ab9004048a1ca','USBA300031OT'),
 "Willow Sports":('6a1610bebdf296985fd95603-66fbf9cf128ab3f65b259bcb','US20000131M'),
}
GUIDE_PROVIDER_IDS={
 "OuterSphere":'nltv:us:OuterSphere.us',
 "Overtime":'nltv:us:Overtime.us',
 "Racer Select":'nltv:us:RacerSelect.us',
 "Stadium":'nltv:us:Stadium.us',
 "The Bob Ross Channel":'nltv:us:TheBobRossChannel.us',
 "This Old House":'nltv:us:ThisOldHouse.us',
 "Tiny House Nation":'nltv:us:TinyHouseNation.us',
 "NFL Channel":'nltv:us:NFLChannel.us',
 "48 Hours":'nltv:us:48Hours.us',
 "Dog the Bounty Hunter":'nltv:us:DogtheBountyHunter.us',
 "Ebony TV by Lionsgate":'nltv:us:EbonyTVbyLionsgate.us',
 "FilmRise Western":'nltv:us:FilmRiseWestern.us',
 "Gravitas Movies":'nltv:us:GravitasMovies.us',
 "Home.Made.Nation":'nltv:us:HomeMadeNation.us',
 "50 Cent Action":'nltv:us:50CentAction.us',
 "All Weddings WE tv":'nltv:us:AllWeddingsWEtv.us',
 "At Home with Family Handyman":'nltv:us:AtHomewithFamilyHandyman.us',
 "Billiard TV":'nltv:us:BilliardTV.us',
 "Celebrity Name Game":'nltv:us:CelebrityNameGame.us',
 "Designated Survivor":'nltv:us:DesignatedSurvivor.us',
 "Dog Whisperer":'nltv:us:DogWhisperer.us',
 "Fubo Sports Network":'nltv:us:FuboSportsNetwork.us',
 "MovieSphere":'nltv:us:MovieSphere.us',
 "NBC Sports NOW":'nltv:us:NBCSportsNOW.us',
 "Nosey":'nltv:us:Nosey.us',
 "PBS Nature":'nltv:us:PBSNature.us',
 "Scripps Sports Network":'nltv:us:ScrippsSportsNetwork.us',
 "SportsGrid":'nltv:us:SportsGrid.us',
 "Willow Sports":'nltv:us:WillowSports.us',
 'FIFA+':'nltv:uk:FIFAPlus.uk', # Existing reviewed global replay feed.
}
GUIDE_URLS=('https://i.mjh.nz/Plex/us.xml.gz','https://i.mjh.nz/SamsungTVPlus/us.xml.gz')
GUIDE_REFRESH=900
GUIDE_MAX_AGE=1800
guide_rows=({}, {})
guide_observed_at=0.0
guide_fault=None
# Explicitly reviewed distributor channel IDs and exact provider identities.
# Never infer identity from a fuzzy channel name or use a guide to replace a
# conflicting primary programme. The single-distributor schedule is evidence,
# not independent proof of decoded video identity.
EPISODE_GUIDE_URL='https://i.mjh.nz/PlutoTV/us.xml.gz'
EPISODE_CHANNELS={
 'nltv:us:BeverlyHills90210.us':('Beverly Hills 90210','5f4d83e0a382c00007bc02e7'),
 'nltv:us:Andromeda.us':('Andromeda','66c638726838ee00085ac20d'),
 'nltv:us:BarRescue.us':('Bar Rescue','60a3d889a5b3690008dc7fe8'),
 'nltv:us:BattlestarGalactica.us':('Battlestar Galactica','69d7ebff029c431826bd328e'),
 'nltv:us:BlueBloods.us':('Blue Bloods','61b3b8c645b45d0007a9bb8d'),
 'nltv:us:Bonanza.us':('Bonanza','68e9913c4a666b9054849832'),
 'nltv:us:Cheers.us':('Cheers','6a28827ef4a627f3b07df1ea'),
 'nltv:us:CSI.us':('CSI','5efbd29e4aa26700076c0d06'),
 'nltv:us:CSIMiami.us':('CSI: Miami','62f54c11b3af68000702c304'),
 'nltv:us:CSINY.us':('CSI: NY','62f54c6439183b000769fb8f'),
 'nltv:us:DiagnosisMurder.us':('Diagnosis Murder','6a2b0eab9465b98649cd0d2f'),
 'nltv:us:Dynasty.us':('Dynasty','64d1606b4c5ed80008e3b8b9'),
 'nltv:us:FamilyTies.us':('Family Ties','5f77939a630f530007dde654'),
 'nltv:us:FeelGoodDrama.us':('Feel Good Drama','65775af3b3801200084a3aa4'),
 'nltv:us:Gunsmoke.us':('Gunsmoke','60f75771dfc72a00071fd0e0'),
 'nltv:us:HappyDays.us':('Happy Days','5f7794162a4559000781fc12'),
 'nltv:us:Matlock.us':('Matlock','60f754f98185580007eb0754'),
 'nltv:us:MidsomerMurders.us':('Midsomer Murders','5cbf6a868a1bce4a3d52a5e9'),
 'nltv:us:MissionImpossible.us':('Mission Impossible','5f77977bd924d80007eee60c'),
 'nltv:us:Mythbusters.us':('Mythbusters','66e0b4866ad04d0008fff4d8'),
 'nltv:us:PerryMason.us':('Perry Mason','6197086891ddd4000739941a'),
 'nltv:us:PlutoTVAdventure.us':('Pluto TV Adventure','69d7ebb726eeec84158e28cb'),
 'nltv:us:PlutoTVCrimeDrama.us':('Pluto TV Crime Drama','5f31fd1b4c510e00071c3103'),
 'nltv:us:PlutoTVHometownDrama.us':('Pluto TV Hometown Drama','69d6c7befde57cd5ba0160c3'),
 'nltv:us:PlutoTVSciFi.us':('Pluto TV Sci-Fi','5b4fc274694c027be6ed3eea'),
 'nltv:us:Rawhide.us':('Rawhide','634f307c7a068e00072c9982'),
 'nltv:us:StarTrekDeepSpaceNine.us':('Star Trek: Deep Space Nine','65c69bbfd77d450008c7ffee'),
 'nltv:us:StarTrekTheNextGeneration.us':('Star Trek: The Next Generation','695c32e61ca20bb8ca802eb9'),
 'nltv:us:StarTrekVoyager.us':('Star Trek: Voyager','634dacf51d90320007fcd5fa'),
 'nltv:us:SupernaturalDrama.us':('Supernatural Drama','5f24662bebe0f0000767de32'),
 'nltv:us:TheAddamsFamily.us':('The Addams Family','5d81607ab737153ea3c1c80e'),
 'nltv:us:TheRifleman.us':('The Rifleman','5e825550e758c700077b0aef'),
 'nltv:us:TheWildWildWest.us':('The Wild Wild West','664e640a0120f40008be4582'),
 'nltv:us:ThreesCompany.us':("Three's Company",'5ef3977e5d773400077de284'),
 'nltv:us:TVLandDrama.us':('TV Land Drama','5d40bebc5e3d2750a2239d7e'),
}
EPISODE_SERIES_IDS={'beverlyhills90210':'tt0098749'}
EPISODE_CACHE_FILE=ROOT/'programme-episodes.json'
EPISODE_CACHE_LIMIT=2048
episode_rows={}
episode_observed_at=0.0
episode_fault=None
episode_lookup_fault=None
episode_lookup_errors=0
episode_lookup_deferred=0
episode_lookup_last_error=None
episode_candidate_errors=0
episode_candidate_last_error=None
episode_cache=OrderedDict()
episode_series_cache=OrderedDict()
episode_series_faults=OrderedDict()
EPISODE_CANDIDATE_RETRY_SECONDS=900
EPISODE_CANDIDATE_RETRY_MAX=6*3600
EPISODE_CANDIDATE_FAULT_LIMIT=64
regional_programme=None
regional_fault=None
regional_observed_at=0.0
discovery=opencode_discovery.Discovery(ROOT/'opencode-discovery.json')
playback_activity=programme_research.ActiveViewing()
research_coordinator=programme_research.ResearchCoordinator()
year_cache=OrderedDict()
YEAR_CACHE_FILE=ROOT/'programme-years.json'
YEAR_CACHE_LIMIT=2048
YEAR_POSITIVE_TTL=7*86400
YEAR_NEGATIVE_TTL=6*3600
YEAR_API='https://v3-cinemeta.strem.io'
year_fault=None
year_last_lookup=0.0
year_errors=0
year_last_error=None
year_retry=OrderedDict()
YEAR_RETRY_SECONDS=900
# Reviewed canonical catalog rows for the exact mapping only. These are
# identity data, never a current-schedule substitute.
CATALOG_REFRESH=6*3600
CATALOG_MAX_AGE=8*3600
CATALOG_RETRY_SECONDS=300
catalog_rows={}
catalog_observed_at=0.0
catalog_fault=None
catalog_last_attempt=0.0

def year_error_code(error):
 if isinstance(error,urllib.error.HTTPError):return 'HTTP '+str(error.code)
 if isinstance(error,json.JSONDecodeError):return 'invalid JSON'
 if isinstance(error,ValueError):
  reason=str(error)
  if reason in ('Metadata response too large','Invalid metadata search','Invalid metadata response','Unsupported upstream URL','Non-public upstream address'):return reason
 return type(error).__name__

class EpisodeLookupIncomplete(ValueError):
 def __init__(self,faults,retry_after=YEAR_RETRY_SECONDS):
  self.faults=tuple(faults[:4]);self.retry_after=retry_after
  super().__init__('Incomplete candidate details')

def episode_error_code(error):
 if isinstance(error,EpisodeLookupIncomplete):return 'Incomplete candidate details'
 if isinstance(error,urllib.error.HTTPError):return 'HTTP '+str(error.code)
 if isinstance(error,json.JSONDecodeError):return 'invalid JSON'
 if isinstance(error,ValueError):
  reason=str(error)
  if reason in ('Metadata response too large','Invalid metadata search','Invalid metadata response','Invalid series search','Invalid series metadata','Episode metadata response too large','Episode metadata limit','Unsupported upstream URL','Non-public upstream address'):return reason
 return type(error).__name__

def release_year(value):
 if type(value) is int:year=value
 elif isinstance(value,str) and re.fullmatch(r'\d{4}(?:[–—-]\d{0,4})?',value.strip()):year=int(value.strip()[:4])
 else:return None
 return year if 1888<=year<=datetime.datetime.now(datetime.timezone.utc).year+1 else None
def title_year(title):
 match=re.search(r'\s+\((\d{4})\)$',title)
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
 result=json.loads(data)
 if not isinstance(result,dict):raise ValueError('Invalid metadata response')
 return result
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
 incomplete=False
 for mid,(kind,year) in candidates.items():
  if not year:continue
  try:
   meta=lookup_json('/meta/'+kind+'/'+mid+'.json').get('meta',{})
   if isinstance(meta,dict) and synopsis_match(synopsis,meta):positive.append(year)
  except urllib.error.HTTPError as error:
   # Do not retry or circumvent denied or missing detail records.
   # A distinctive matching synopsis/cast is positive identity evidence;
   # inaccessible alternatives are not evidence of a conflicting match.
   if error.code not in (403,404,410):raise
   incomplete=True
  except ValueError as error:
   if str(error)!='Metadata response too large':raise
   incomplete=True
 if len(positive)==1:return positive[0]
 if not positive and incomplete:raise ValueError('Incomplete candidate details')
 return None
def year_worker(stop):
 global year_fault,year_last_lookup,year_errors,year_last_error
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
    with lock:
     entry=year_cache.get(key)
     deferred=year_retry.get(key,0)
    if (entry and now<entry['expires']) or now<deferred:continue
    selected=(key,row);break
   if selected:
    key,row=selected
    try:year=lookup_year(row)
    except Exception as error:
     reason=year_error_code(error)
     if isinstance(error,ValueError) and str(error)=='Incomplete candidate details':reason='Incomplete candidate details'
     with lock:
      year_errors+=1;year_last_error={'at':time.time(),'key':key[:12],'reason':reason}
      year_retry[key]=time.time()+YEAR_RETRY_SECONDS
      year_retry.move_to_end(key)
      while len(year_retry)>YEAR_CACHE_LIMIT:year_retry.popitem(last=False)
      year_last_lookup=time.time();year_fault=None
     log('programme year deferred '+reason)
     if stop.wait(3):return
     continue
    with lock:
     year_retry.pop(key,None)
     year_cache[key]={'year':year,'expires':now+(YEAR_POSITIVE_TTL if year else YEAR_NEGATIVE_TTL)}
     year_cache.move_to_end(key)
     while len(year_cache)>YEAR_CACHE_LIMIT:year_cache.popitem(last=False)
     snapshot=dict(year_cache);year_last_lookup=time.time();year_fault=None
    atomic_json(YEAR_CACHE_FILE,snapshot)
   if stop.wait(3 if selected else 15):return
  except Exception as error:
   with lock:year_fault=year_error_code(error)
   log('programme year worker failed '+year_fault)
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
  code=next((x.text for x in p.findall('episode-num') if x.get('system')=='onscreen'),'')
  match=re.fullmatch(r'S(\d{1,2})E(\d{1,4})',code or '',re.I)
  numbers=(int(match[1]),int(match[2])) if match and 0<int(match[1])<=99 and 0<int(match[2])<=9999 else None
  zone=(p.get('start') or '')[-5:].strip()
  rows.append((start,end,title,programme_text((p.findtext('desc') or '')[:300]),programme_text(p.findtext('sub-title')),numbers,zone))
 return out
def fetch_guide(url,ids,now):
 req=urllib.request.Request(validate(url),headers={'User-Agent':'Jealousfin-EPG/1.0'})
 with opener.open(req,timeout=10) as f:raw=f.read(8*1024*1024+1)
 if len(raw)>8*1024*1024:raise ValueError('Compressed guide too large')
 with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:data=f.read(40*1024*1024+1)
 return parse_guide(data,ids,now)
def catalog_snapshot(now=None):
 now=time.time() if now is None else now
 with lock:
  if not 0<=now-catalog_observed_at<=CATALOG_MAX_AGE:return {}
  return {key:list(value) for key,value in catalog_rows.items()}
def fetch_catalog_page(url):
 validate(url)
 original=urllib.parse.urlsplit(url).hostname.removeprefix('www.')
 class CatalogRedirect(Redirect):
  def redirect_request(self,req,fp,code,msg,headers,newurl):
   target=urllib.parse.urljoin(req.full_url,newurl)
   validate(target)
   if urllib.parse.urlsplit(target).hostname.removeprefix('www.')!=original:raise ValueError('Unapproved catalog redirect')
   return super().redirect_request(req,fp,code,msg,headers,target)
 agent=urllib.request.build_opener(urllib.request.ProxyHandler({}),CatalogRedirect())
 request=urllib.request.Request(url,headers={'User-Agent':'Jealousfin-Episode-Catalog/1.0','Accept':'text/html,application/json'})
 with agent.open(request,timeout=10) as f:raw=f.read(2*1024*1024+1)
 if len(raw)>2*1024*1024:raise ValueError('Catalog response too large')
 return raw.decode(f.headers.get_content_charset() or 'utf-8',errors='replace')
def catalog_worker(stop):
 global catalog_rows,catalog_observed_at,catalog_fault,catalog_last_attempt
 while not stop.is_set():
  try:
   now=time.time()
   with lock:
    if 0<=now-catalog_observed_at<CATALOG_REFRESH:delay=CATALOG_REFRESH-(now-catalog_observed_at)
    elif catalog_last_attempt and now-catalog_last_attempt<CATALOG_RETRY_SECONDS:delay=CATALOG_RETRY_SECONDS-(now-catalog_last_attempt)
    else:delay=0
   if delay>0:
    if stop.wait(min(delay,60)):return
    continue
   with lock:catalog_last_attempt=now
   fetched={};fault=None
   for provider_id,mapping in schedule_episode.REVIEWED_MAPPINGS.items():
    rows=[]
    tvmaze_id=mapping.catalog_ids.get('tvmaze')
    if tvmaze_id:
     try:
      page=fetch_catalog_page('https://api.tvmaze.com/shows/'+urllib.parse.quote(tvmaze_id,safe='')+'/episodes')
      rows.extend(schedule_episode.parse_tvmaze_episodes(page,mapping.series_title,
       'https://api.tvmaze.com/shows/'+urllib.parse.quote(tvmaze_id,safe='')+'/episodes'))
     except Exception as error:fault=type(error).__name__
    tvdb_id=mapping.catalog_ids.get('tvdb')
    if tvdb_id:
     try:
      page=fetch_catalog_page('https://thetvdb.com/series/'+urllib.parse.quote(tvdb_id,safe='')+'/allseasons/official')
      rows.extend(schedule_episode.parse_tvdb_allseasons_html(page,mapping.series_title,
       'https://thetvdb.com/series/'+urllib.parse.quote(tvdb_id,safe='')+'/allseasons/official'))
     except Exception as error:fault=type(error).__name__
    if rows:fetched[provider_id]=rows[:schedule_episode.MAX_EPISODES]
   with lock:
    if fetched:catalog_rows=fetched;catalog_observed_at=now;catalog_fault=None
    else:catalog_fault=fault or 'empty-catalog'
   log('episode catalog refresh '+(('ok channels='+str(len(fetched))) if fetched else ('failed '+str(catalog_fault))))
   if stop.wait(30):return
  except Exception as error:
   with lock:catalog_fault=type(error).__name__
   log('episode catalog worker '+type(error).__name__)
   if stop.wait(300):return
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
  mapped=[(cid,GUIDE_CHANNELS[c['Name']]) for cid,c in channels.items() if c['Name'] in GUIDE_CHANNELS and sources.get(cid)==GUIDE_PROVIDER_IDS.get(c['Name'])]
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
  provider_id=sources.get(cid)
  catalogs=catalog_snapshot(now).get(provider_id,())
  if catalogs and provider_id in schedule_episode.REVIEWED_MAPPINGS:
   mapped=schedule_episode.REVIEWED_MAPPINGS[provider_id]
   events=[]
   for index,row in enumerate((a[0],b[0])):
    events.append(schedule_evidence.Event(
     title=row[2],start=row[0],end=row[1],station=channels[cid]['Name'],region=mapped.region,
     zone=(row[6] if len(row)>6 else ''),source=('i.mjh.nz/Plex' if index==0 else 'i.mjh.nz/SamsungTVPlus'),
     format='xmltv',episode_title=(row[4] if len(row)>4 else ''),
     season_number=(row[5][0] if len(row)>5 and row[5] else None),
     episode_number=(row[5][1] if len(row)>5 and row[5] else None),
     first_party=False,structured=True))
   resolved=schedule_episode.resolve_current(now,provider_id,channels[cid]['Name'],schedule_events=events,
                                              catalogs=catalogs,observed_at=now)
   if resolved.accepted and isinstance(resolved.row,dict):
    for key in ('episodeTitle','seasonNumber','episodeNumber','seasonYear','episodeSource',
                'episodeObservedAt','episodeOriginalAirDate','scheduleVerification',
                'scheduleEpisodeSources','episodeKind','seasonPremiereYear','seriesPremiereYear'):
     if key in resolved.row:out[cid][key]=resolved.row[key]
 return out

def episode_key(row):
 return hashlib.sha256(json.dumps([guide_norm(row['title']),guide_norm(row['episodeTitle']),row['guideSeasonNumber'],row['guideEpisodeNumber']],separators=(',',':')).encode()).hexdigest()

def checked_episode(value):
 if not isinstance(value,dict):return {}
 season=value.get('seasonNumber');number=value.get('episodeNumber')
 if type(season) is not int or not 1<=season<=99 or type(number) is not int or not 1<=number<=9999:return {}
 result={'seasonNumber':season,'episodeNumber':number}
 year=release_year(value.get('seasonYear'))
 if year:result['seasonYear']=year
 return result

def load_episode_cache():
 try:
  if not EPISODE_CACHE_FILE.exists():return
  if EPISODE_CACHE_FILE.stat().st_size>1024*1024:raise ValueError('Episode cache too large')
  raw=json.loads(EPISODE_CACHE_FILE.read_text())
  if not isinstance(raw,dict):raise ValueError('Invalid episode cache')
  now=time.time()
  with lock:
   for key,entry in list(raw.items())[-EPISODE_CACHE_LIMIT:]:
    if isinstance(key,str) and re.fullmatch(r'[a-f0-9]{64}',key) and isinstance(entry,dict) and type(entry.get('expires')) in (int,float) and now<entry['expires']<=now+YEAR_POSITIVE_TTL:
     episode_cache[key]={'expires':entry['expires'],'value':checked_episode(entry.get('value'))}
 except (OSError,ValueError,TypeError):log('episode cache ignored')

def episode_snapshot(now,programmes):
 with lock:
  observed=episode_observed_at
  if not 0<=now-observed<=GUIDE_MAX_AGE:return {}
  mapped=[(cid,EPISODE_CHANNELS[sources[cid]][1]) for cid,c in channels.items() if sources.get(cid) in EPISODE_CHANNELS and c['Name']==EPISODE_CHANNELS[sources[cid]][0]]
  out={}
  for cid,gid in mapped:
   primary=programmes.get(cid)
   # No title-only guess from a channel name, or substitution for another show.
   if not primary or primary.get('source')!='provider-catalogue':continue
   active=[r for r in episode_rows.get(gid,[]) if r[0]<=now<r[1]]
   if len(active)!=1:continue
   row=active[0];start,end,title=row[:3]
   if guide_norm(title_year(primary['title'])[0])!=guide_norm(title_year(title)[0]):continue
   if len(row)<6 or not row[4] or not row[5]:continue
   season,number=row[5]
   item={'episodeTitle':row[4],'guideSeasonNumber':season,'guideEpisodeNumber':number,'episodeSource':'pluto-xmltv','episodeObservedAt':observed,'start':start,'end':end}
   key=episode_key(dict(primary,**item))
   entry=episode_cache.get(key)
   if entry and now<entry['expires']:item.update(checked_episode(entry['value']))
   out[cid]=item
  return out

def episode_guide_worker(stop):
 global episode_rows,episode_observed_at,episode_fault
 while not stop.is_set():
  delay=GUIDE_REFRESH
  try:
   now=time.time()
   rows=fetch_guide(EPISODE_GUIDE_URL,{x[1] for x in EPISODE_CHANNELS.values()},now)
   if not rows:raise ValueError('Empty episode guide')
   with lock:episode_rows=rows;episode_observed_at=now;episode_fault=None
  except Exception as error:
   reason,delay=programme_retry_delay(error,1)
   delay=max(delay,GUIDE_REFRESH)
   with lock:episode_fault=reason
   log('episode guide refresh failed '+reason)
  if stop.wait(delay):return

def note_episode_candidate_fault(mid,error,now):
 global episode_candidate_errors,episode_candidate_last_error
 reason=episode_error_code(error)
 with lock:
  prior=episode_series_faults.get(mid)
  delay=min((prior.get('delay') or EPISODE_CANDIDATE_RETRY_SECONDS)*2 if prior else EPISODE_CANDIDATE_RETRY_SECONDS,EPISODE_CANDIDATE_RETRY_MAX)
  episode_series_faults[mid]={'until':now+delay,'delay':delay,'reason':reason}
  episode_series_faults.move_to_end(mid)
  while len(episode_series_faults)>EPISODE_CANDIDATE_FAULT_LIMIT:episode_series_faults.popitem(last=False)
  episode_candidate_errors+=1
  episode_candidate_last_error={'at':now,'candidate':mid,'reason':reason}
 return reason,now+delay

def clear_episode_candidate_fault(mid):
 with lock:episode_series_faults.pop(mid,None)

def fetch_episode_series(mid):
 # The entire episode catalogue is bounded, with a larger limit than ordinary
 # movie detail metadata. Keep only titles, numbers and dates in memory.
 now=time.time()
 cached=episode_series_cache.get(mid)
 if cached and now<cached[0]:
  episode_series_cache.move_to_end(mid);return cached[1]
 url=validate(YEAR_API+'/meta/series/'+mid+'.json')
 req=urllib.request.Request(url,headers={'User-Agent':'Jealousfin-Programme-Metadata/1.0','Accept':'application/json'})
 with opener.open(req,timeout=8) as f:data=f.read(4*1024*1024+1)
 if len(data)>4*1024*1024:raise ValueError('Episode metadata response too large')
 obj=json.loads(data)
 meta=obj.get('meta') if isinstance(obj,dict) else None
 if not isinstance(meta,dict) or not isinstance(meta.get('videos'),list):raise ValueError('Invalid series metadata')
 if len(meta['videos'])>5000:raise ValueError('Episode metadata limit')
 name=programme_text(meta.get('name'))
 videos=[]
 for v in meta['videos']:
  if not isinstance(v,dict):continue
  season=v.get('season');number=v.get('episode');title=programme_text(v.get('title') or v.get('name'))
  if type(season) is not int or not 1<=season<=99 or type(number) is not int or not 1<=number<=9999:continue
  date=v.get('released')
  year=None
  if isinstance(date,str):
   try:year=release_year(datetime.datetime.fromisoformat(date.replace('Z','+00:00')).year)
   except ValueError:pass
  videos.append((season,number,title,year))
 value=(name,videos)
 episode_series_cache[mid]=(now+86400,value)
 episode_series_cache.move_to_end(mid)
 while len(episode_series_cache)>16:episode_series_cache.popitem(last=False)
 return value

def resolve_episode(row):
 series=title_year(row['title'])[0];norm=guide_norm(series)
 episode=guide_norm(row['episodeTitle'])
 if len(episode)<4 or re.fullmatch(r'(?:episode|ep|e)\d+',episode):return {}
 known=EPISODE_SERIES_IDS.get(norm)
 if known:ids=[known]
 else:
  result=lookup_json('/catalog/series/top/search='+urllib.parse.quote(series,safe='')+'.json')
  metas=result.get('metas')
  if not isinstance(metas,list):raise ValueError('Invalid series search')
  ids=list(dict.fromkeys(m['id'] for m in metas if isinstance(m,dict) and isinstance(m.get('id'),str) and re.fullmatch(r'tt\d{5,12}',m['id']) and isinstance(m.get('name'),str) and guide_norm(m['name'])==norm))
 if not 1<=len(ids)<=4:return {}
 exact=[];named=[];faults=[]
 for mid in ids:
  now=time.time()
  with lock:prior=episode_series_faults.get(mid)
  if prior and now<prior['until']:
   faults.append((mid,prior['reason'],prior['until']));continue
  try:name,videos=fetch_episode_series(mid)
  except Exception as error:
   reason,until=note_episode_candidate_fault(mid,error,now)
   log('episode candidate deferred '+reason+' candidate='+mid)
   faults.append((mid,reason,until));continue
  clear_episode_candidate_fault(mid)
  if guide_norm(name)!=norm:continue
  for s,e,t,y in videos:
   if guide_norm(t)!=episode:continue
   first={v[3] for v in videos if v[0]==s and v[1]==1 and v[3]}
   result={'seasonNumber':s,'episodeNumber':e}
   if len(first)==1:result['seasonYear']=next(iter(first))
   named.append(result)
   if (s,e)==(row['guideSeasonNumber'],row['guideEpisodeNumber']):exact.append(result)
 # If distributor numbering differs, correct only a unique canonical title
 # match across all exact series candidates. Never pick a remake by ranking.
 if len(exact)==1:return exact[0]
 if not exact and len(named)==1:return named[0]
 if faults:
  until=max(item[2] for item in faults)
  raise EpisodeLookupIncomplete(faults,max(YEAR_RETRY_SECONDS,until-time.time()))
 return {}

def episode_worker(stop):
 global episode_lookup_fault,episode_lookup_errors,episode_lookup_deferred,episode_lookup_last_error
 load_episode_cache()
 while not stop.is_set():
  selected=None
  try:
   now=time.time()
   rows=programme_snapshot(now)
   with lock:
    recent=dict(active_channels)
    for cid,row in sorted(rows.items(),key=lambda item:recent.get(item[0],0),reverse=True):
     if not row.get('episodeTitle'):continue
     key=episode_key(row);entry=episode_cache.get(key)
     if entry and now<entry['expires']:continue
     selected=(key,row);break
   if selected:
    key,row=selected
    try:
     value=checked_episode(resolve_episode(row));ttl=YEAR_POSITIVE_TTL if value else YEAR_NEGATIVE_TTL
     with lock:episode_lookup_fault=None
    except EpisodeLookupIncomplete as error:
     value={};ttl=max(YEAR_RETRY_SECONDS,error.retry_after)
     detail=';'.join(mid+':'+reason for mid,reason,_ in error.faults[:3])
     with lock:
      episode_lookup_deferred+=1;episode_lookup_fault=None
      episode_lookup_last_error={'at':time.time(),'key':key[:12],'reason':'Incomplete candidate details','candidates':detail}
     log('episode lookup deferred Incomplete candidate details '+detail)
    except Exception as error:
     reason=episode_error_code(error);value={};ttl=YEAR_RETRY_SECONDS
     with lock:
      episode_lookup_errors+=1;episode_lookup_fault=reason
      episode_lookup_last_error={'at':time.time(),'key':key[:12],'reason':reason}
     log('episode lookup deferred '+reason)
    with lock:
     episode_cache[key]={'expires':time.time()+ttl,'value':value};episode_cache.move_to_end(key)
     while len(episode_cache)>EPISODE_CACHE_LIMIT:episode_cache.popitem(last=False)
     snapshot=dict(episode_cache)
    atomic_json(EPISODE_CACHE_FILE,snapshot)
   if stop.wait(3 if selected else 15):return
  except Exception as error:
   reason=episode_error_code(error)
   with lock:episode_lookup_fault=reason
   log('episode worker failed '+reason)
   if stop.wait(60):return

def regional_snapshot(now):
 with lock:
  row=regional_programme
  if not row or not 0<=now-row['observedAt']<=regional_guide.MAX_AGE or not row['start']<=now<row['end']:return {}
  return {cid:dict(row) for cid,c in channels.items() if c['Name']==regional_guide.CHANNEL and sources.get(cid)==regional_guide.PROVIDER_ID}

def fetch_regional_page(url):
 req=urllib.request.Request(validate(url),headers={'User-Agent':'Jealousfin-EPG/1.0','Accept':'text/html'})
 with opener.open(req,timeout=8) as f:
  raw=f.read(2*1024*1024+1)
  encoding=f.headers.get_content_charset() or 'utf-8'
 if len(raw)>2*1024*1024:raise ValueError('Regional guide too large')
 return raw.decode(encoding,errors='strict')

def regional_worker(stop):
 global regional_programme,regional_fault,regional_observed_at
 while not stop.is_set():
  delay=regional_guide.REFRESH
  try:
   now=time.time()
   row=regional_guide.fetch_current(fetch_regional_page,now)
   with lock:regional_programme=row;regional_observed_at=now;regional_fault=None
  except Exception as error:
   reason,delay=programme_retry_delay(error,1)
   # Use a slow cadence; a rejected public guide is never retried rapidly.
   delay=max(delay,regional_guide.REFRESH)
   with lock:regional_fault=reason
   log('regional programme refresh failed '+reason)
  if stop.wait(delay):return

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
 # Dedicated series channels can legitimately have the same channel and
 # programme name. Require both explicit Now fields and a coherent boundary
 # before accepting that case; never infer a show from the channel name alone.
 if current.casefold()==str(row.get('name','')).strip().casefold():
  if not release_title or not end_clock or not next_title or next_clock!=end_clock:return None
 return {'title':current,'nextTitle':next_title,'providerEndClock':end_clock,'providerNextClock':next_clock}

def update_programmes(raw,observed=None,expanded=None,canada=None):
 global programme_rows,programme_observed_at,programme_fault
 now=time.time() if observed is None else observed
 # Metadata refresh is independent from catalogue identity, policy and playback.
 valid={}
 for source_id,row in raw.items():
  if not isinstance(row,dict):continue
  cid=source_profiles.channel_id(SOURCE_PROFILE,source_id)
  if cid not in sources:continue
  parsed=parse_programme(row)
  if parsed:valid[cid]=parsed
 # The admitted expanded provider has independent stream and metadata routing.
 for source_id,row in (expanded_catalogue(gateway_config) if expanded is None else expanded).items():
  cid=source_profiles.channel_id(SOURCE_PROFILE,source_id)
  if sources.get(cid)==source_id and source_id not in primary_source_ids:
   parsed=parse_programme(row)
   if parsed:valid[cid]=parsed
 for source_id,row in (canada_catalogue(gateway_config) if canada is None else canada).items():
  cid=source_profiles.channel_id(SOURCE_PROFILE,source_id)
  if sources.get(cid)==source_id:
   parsed=parse_programme(row)
   if parsed:valid[cid]=parsed
 # A UK provider fault cannot erase the original provider's metadata.
 for source_id,row in uk_programmes(gateway_config).items():
  cid=source_profiles.channel_id(SOURCE_PROFILE,source_id)
  if sources.get(cid)==source_id:
   parsed=parse_programme(row)
   if parsed:valid[cid]=parsed
 with lock:
  programme_rows=valid;programme_observed_at=now;programme_fault=None

def fetch_discovery_page(url):
 allowed=opencode_discovery.GUIDE_HOSTS
 original_host=urllib.parse.urlsplit(url).hostname
 class GuideRedirect(Redirect):
  def redirect_request(self,req,fp,code,msg,headers,newurl):
   target=opencode_discovery.public_url(urllib.parse.urljoin(req.full_url,newurl))
   if not target or urllib.parse.urlsplit(target).hostname.removeprefix('www.')!=original_host.removeprefix('www.'):raise ValueError('Unapproved guide redirect')
   return super().redirect_request(req,fp,code,msg,headers,target)
 target=opencode_discovery.public_url(url)
 if not target or original_host not in allowed:raise ValueError('Unapproved guide source')
 request=urllib.request.Request(validate(target),headers={'User-Agent':'Jealousfin-EPG/1.0','Accept':'text/html'})
 agent=urllib.request.build_opener(urllib.request.ProxyHandler({}),GuideRedirect())
 with agent.open(request,timeout=8) as f:
  raw=f.read(2*1024*1024+1)
  encoding=f.headers.get_content_charset() or 'utf-8'
 if len(raw)>2*1024*1024:raise ValueError('Research guide too large')
 return raw.decode(encoding,errors='strict')

def discovery_worker(stop):
 # Normal bounded gateway worker; not an external scheduled task.
 while not stop.wait(10):
  plan=None
  try:
   reload_gateway_config()
   if not gateway_config['metadataResearchEnabled'] or not opencode_discovery.available():continue
   if discovery.in_flight:continue
   now=time.time()
   with lock:
    if not 0<=now-max(programme_observed_at,paged_last_success)<=PROGRAMME_MAX_AGE:continue
    if programme_fault and not expanded_base(gateway_config):continue
    known=programme_snapshot(now)
    active={cid for cid,until in active_channels.items() if until>time.monotonic()}
    missing={cid for cid in channels if cid not in known and sources.get(cid) and opencode_discovery.supported_region(sources[cid])}
    views=playback_activity.views()
    plan=research_coordinator.choose_active(missing,views,now,gateway_config['metadataResearchVariant'],discovery.records)
    recheck=None if plan else discovery.choose_recheck(missing,active,now)
    cid=plan.channel_id if plan else (recheck or discovery.choose(missing,active,now,gateway_config['metadataResearchDailyBudget']))
    if not cid:continue
    channel=channels[cid]['Name'];provider_id=sources[cid]
    model=gateway_config['metadataResearchModel'];timeout=gateway_config['metadataResearchTimeoutSeconds']
    variant=plan.variant if plan else gateway_config['metadataResearchVariant']
    if not opencode_discovery.clean(channel) or not re.fullmatch(r'nltv:[A-Za-z0-9_-]+:[A-Za-z0-9_.-]{1,160}',provider_id):continue
    if recheck:
     discovery.rechecked[cid]=now
     previous=dict(discovery.records[cid]['candidate'])
    else:
     if plan:discovery.active_calls=(discovery.active_calls+[now])[-128:]
     else:discovery.calls.append(now)
     discovery.records[cid]={'at':now,'outcome':'started','candidate':opencode_discovery.normalize_result({})}
     discovery.save(atomic_json)
    discovery.in_flight=True
   try:
    if recheck:
     candidate=previous;diagnostics=None
    else:
     started=time.monotonic()
     try:
      prompt=opencode_discovery.prompt_for(channel,provider_id,now,variant,
              plan.attempt if plan else 1,
              discovery.records.get(cid,{}).get('candidate',{}).get('title') if plan else None)
      raw,diagnostics=opencode_discovery.run_model_detailed(prompt,model,timeout,variant)
     finally:
      with lock:discovery.last_run={'variant':variant,'seconds':round(time.monotonic()-started,3),'channelID':cid,'at':time.time()}
     candidate=opencode_discovery.normalize_result(raw)
    checked_at=time.time()
    row,verification=opencode_discovery.verify_candidate_detailed(candidate,channel,provider_id,checked_at,fetch_discovery_page,match_title=not recheck)
    with lock:
     discovery.last_tool_diagnostics=diagnostics
     discovery.last_verification=verification
    if recheck and not row:
     # Source pages may be date-specific; return to budgeted research.
     with lock:
      discovery.records[cid]['outcome']='unverified'
      discovery.save(atomic_json)
     continue
    outcome='verified' if row else 'unverified' if candidate['title'] else 'missing'
    if row:candidate=dict(candidate,title=row['title'],year=None)
    with lock:
     discovery.records[cid]={'at':now,'outcome':outcome,'candidate':candidate}
     if row and gateway_config['metadataResearchEnabled']:discovery.verified[cid]=row
     discovery.last_fault=None
     discovery.save(atomic_json)
     if plan:research_coordinator.record(plan,outcome,now)
    log('programme research '+('verified' if row else 'lead only' if candidate['title'] else 'no evidence')+' channel='+cid)
   except Exception as error:
    with lock:
     discovery.last_fault=type(error).__name__
     discovery.last_tool_diagnostics=getattr(error,'diagnostics',None) or {'error':type(error).__name__}
     discovery.records[cid]['outcome']='error'
     discovery.save(atomic_json)
     if plan:research_coordinator.record(plan,'error',time.time())
    log('programme research failed '+type(error).__name__)
   finally:
    with lock:discovery.in_flight=False
  except Exception as error:
   with lock:discovery.in_flight=False;discovery.last_fault=type(error).__name__
   log('programme research worker '+type(error).__name__)

def discovery_status():
 with lock:
  now=time.time();known=programme_snapshot(now)
  missing=[cid for cid in channels if cid not in known and sources.get(cid)]
  return dict(discovery.status(now,gateway_config['metadataResearchDailyBudget']),enabled=gateway_config['metadataResearchEnabled'],timeoutSeconds=gateway_config['metadataResearchTimeoutSeconds'],variant=gateway_config['metadataResearchVariant'],lastRun=discovery.last_run,missingProgrammeChannels=len(missing),verifiableMissingChannels=sum(opencode_discovery.supported_region(sources[cid]) for cid in missing),activeViewing=playback_activity.diagnostics(),activeResearch=research_coordinator.diagnostics(now))

def merge_episode_publication(result,now=None):
 # Bounded audio episode identity is additive only. It never replaces a
 # provider/episode-guide title, verified numbering or an existing schedule
 # episode, and it expires on its own independent clock.
 now=time.time() if now is None else now
 for cid,audio in episode_publisher.snapshot(now).items():
  row=result.get(cid)
  series=audio.get("audioSeriesTitle")
  session=audio.get("audioPlaybackSessionHash")
  if session:
   view=playback_activity.views().get(cid)
   if view is None or view.session_hash!=session:continue
  if not isinstance(row,dict):
   if not session or not series:continue
   row={"title":series,"source":"local-audio-identity","observedAt":audio["episodeObservedAt"]}
  if episode_audio.programme_has_stronger_episode_metadata(row):continue
  if series and episode_audio.source_key(row.get("title"))!=episode_audio.source_key(series):continue
  merged=dict(row);merged.update({k:v for k,v in audio.items() if k not in ("audioSeriesTitle","audioPlaybackSessionHash")});result[cid]=merged
 return result

def programme_snapshot(now=None):
 now=time.time() if now is None else now
 result=guide_snapshot(now)
 result.update(regional_snapshot(now))
 with lock:
  if gateway_config['metadataResearchEnabled']:
   for cid,row in discovery.snapshot(now).items():result.setdefault(cid,row)
  provider_rows={cid:(programme_observed_at,row) for cid,row in programme_rows.items() if cid not in paged_seen and 0<=now-programme_observed_at<=PROGRAMME_MAX_AGE}
  for origin in ('primary','expanded','uk','canada'):
   newest={}
   for (owner,page),(at,rows) in paged_programmes.items():
    if owner!=origin or not 0<=now-at<=PROGRAMME_MAX_AGE:continue
    for cid,row in rows.items():
     if cid not in newest or at>=newest[cid][0]:newest[cid]=(at,row)
   for cid,(at,row) in newest.items():
    if row is not None:provider_rows[cid]=(at,row)
  for cid,(at,row) in provider_rows.items():
   evidence=result.get(cid,{})
   primary=dict(row,source='provider-catalogue',observedAt=at)
   if evidence and guide_norm(evidence['title'])==guide_norm(row['title']):
    for key in ('guideSynopsis','guideEpisode'):
     if evidence.get(key):primary[key]=evidence[key]
    # The corroborated schedule/catalog identity is additive. The guide fetch
    # is the only source of it, so keep it on the provider row instead of
    # discarding it below. Existing provider XMLTV episode data still wins
    # because episode_snapshot() is applied afterwards.
    if evidence.get('episodeSource')=='catalog-schedule':
     for key in ('episodeTitle','seasonNumber','episodeNumber','seasonYear','episodeYear',
                 'episodeSource','episodeObservedAt','episodeOriginalAirDate',
                 'scheduleVerification','scheduleEpisodeSources','episodeKind',
                 'seasonPremiereYear','seriesPremiereYear'):
      if key in evidence and key not in primary:primary[key]=evidence[key]
   result[cid]=primary
 for cid,episode in episode_snapshot(now,result).items():result[cid].update(episode)
 return merge_episode_publication(result,now)

def programme_item(cid,row):
 # The provider supplies a title and an unzoned end clock, not a reliable
 # UTC schedule. Keep the standard programme dates absent and expose evidence.
 title=row['title']
 year=cached_year(row)
 display=title if not year or title_year(title)[1] else title+' ('+str(year)+')'
 evidence={k:v for k,v in row.items() if k not in ('guideSynopsis','guideEpisode','guideSeasonNumber','guideEpisodeNumber')}
 episode=programme_text(row.get('episodeTitle'))
 if episode:
  prefix='' if guide_norm(channels.get(cid,{}).get('Name',''))==guide_norm(title_year(title)[0]) else title_year(title)[0]+' — '
  season=row.get('seasonNumber');number=row.get('episodeNumber')
  code=f'S{season:02d}E{number:02d}: ' if type(season) is int and type(number) is int else ''
  season_year=release_year(row.get('seasonYear')) or (release_year(row.get('episodeYear')) if row.get('episodeSource')=='local-audio-episode-identity' else None)
  display=prefix+code+episode+(f' ({season_year})' if season_year else '')
 if year:evidence['year']=year
 identity=title if not episode else title+'\0'+episode+'\0'+str(row.get('start',''))
 item={'Id':hashlib.sha256((cid+'\0'+identity).encode()).hexdigest()[:32],
       'Name':display,'ChannelId':cid,'Type':'Program',
       'JealousfinProgramme':evidence}
 if year:item['ProductionYear']=year
 if (row.get('source')=='corroborated-external-schedule' or episode) and isinstance(row.get('start'),(int,float)) and isinstance(row.get('end'),(int,float)):
  try:
   if type(row['start']) not in (int,float) or type(row['end']) not in (int,float) or not row['start']<row['end']:raise ValueError('invalid programme interval')
   start=datetime.datetime.fromtimestamp(row['start'],datetime.timezone.utc).isoformat()
   end=datetime.datetime.fromtimestamp(row['end'],datetime.timezone.utc).isoformat()
  except (ValueError,OverflowError,OSError):
   pass
  else:
   if row.get('source')=='corroborated-external-schedule':item['Name']='Scheduled: '+display
   item['StartDate']=start
   item['EndDate']=end
 return item

def programme_retry_delay(error,failures,now=None):
 # Recover from transient refresh failures while the existing evidence is
 # still fresh. Rate limits and authorization failures never get fast retries.
 now=time.time() if now is None else now
 transient=False
 retry_after=0.0
 if isinstance(error,urllib.error.HTTPError):
  reason='HTTP '+str(error.code)
  transient=error.code in (408,500,502,503,504)
  delay=300 if error.code in (401,403) else 120 if error.code==429 else PROGRAMME_REFRESH
  value=error.headers.get('Retry-After') if error.headers else None
  if isinstance(value,str):
   try:
    retry_after=float(int(value)) if value.isdecimal() and len(value)<=12 else email.utils.parsedate_to_datetime(value).timestamp()-now
   except (ValueError,TypeError,OverflowError,AttributeError):retry_after=0.0
  # An absurdly large Retry-After must not overflow Event.wait and kill
  # the worker. The platform maximum still defers effectively indefinitely.
  if retry_after>0:return reason,min(threading.TIMEOUT_MAX-1,max(delay,retry_after))
 elif isinstance(error,(TimeoutError,ConnectionError)) or (isinstance(error,urllib.error.URLError) and isinstance(error.reason,(TimeoutError,ConnectionError))):
  reason=type(error).__name__;transient=True;delay=PROGRAMME_REFRESH
 else:
  reason=type(error).__name__;delay=PROGRAMME_REFRESH
 if transient:delay=min(PROGRAMME_REFRESH,15*2**min(2,max(0,failures-1)))
 return reason,delay

def page_plan(config):
 return [('primary',None,p) for p in range(max(1,(len(primary_source_ids)+99)//100))]+[
  ('expanded',expanded_base(config),p) for p in range(config['expandedCataloguePageCount'])]+(
  [('uk',uk_base(config),0)] if uk_base(config) else [])+(
  [('canada',canada_base(config),p) for p in range(config.get('canadaCataloguePageCount',1))] if canada_base(config) else [])

def refresh_programme_page(origin,base,page,now=None):
 global paged_last_success,paged_origin_block_until
 supplied_now=now
 now=time.time() if now is None else now
 key=(origin,page)
 path='/catalog/tv/nltv_channels'+('/skip='+str(page*100) if page else '')+'.json'
 try:
  response=provider_json(path) if base is None else provider_json(path,base)
  rows=response.get('metas') if isinstance(response,dict) else None
  if not isinstance(rows,list) or not rows or len(rows)>100:raise ValueError('Invalid programme page')
  config=gateway_config
  allowed=primary_source_ids if origin=='primary' else expanded_cohort(config)-primary_source_ids if origin=='expanded' else canada_cohort(config) if origin=='canada' else uk_cohort(config)
  parsed={}
  for row in rows:
   if not isinstance(row,dict) or row.get('id') not in allowed:continue
   sid=row['id'];cid=source_profiles.channel_id(SOURCE_PROFILE,sid)
   if sources.get(cid)==sid:parsed[cid]=parse_programme(row)
  observed=time.time() if supplied_now is None else supplied_now
  with lock:
   old=paged_programmes.get(key,(0,{}))[1]
   paged_seen.update(old);paged_seen.update(parsed)
   paged_programmes[key]=(observed,parsed)
   paged_last_success=max(paged_last_success,observed)
   paged_faults.pop(key,None);paged_failures.pop(key,None)
   paged_due[key]=observed+PROGRAMME_REFRESH
  return True
 except Exception as error:
  failures=paged_failures.get(key,0)+1
  reason,delay=programme_retry_delay(error,failures,now)
  if isinstance(error,urllib.error.HTTPError):
   if error.code in (408,500,502,503,504):delay=max(delay,min(300,15*2**min(5,failures-1)))
   if error.code in (401,403,429) or error.headers and error.headers.get('Retry-After'):
    with lock:paged_origin_block_until=max(paged_origin_block_until,now+delay)
   if getattr(error,"fp",None) is not None:error.close()
  with lock:
   paged_faults[key]=reason;paged_failures[key]=failures;paged_due[key]=now+delay
  log('Programme page '+origin+'/'+str(page*100)+' failed '+reason+'; retry in '+str(round(delay))+'s')
  return False

def paged_refresh_status(now=None):
 global expanded_manifest_status,uk_manifest_status,canada_manifest_status,programme_fault,programme_refresh_failures,programme_next_attempt
 now=time.time() if now is None else now
 with lock:
  for origin,status in (('expanded',expanded_manifest_status),('uk',uk_manifest_status),('canada',canada_manifest_status)):
   pages=[(key,at,rows) for key,(at,rows) in paged_programmes.items() if key[0]==origin]
   faults={str(k[1]*100):v for k,v in paged_faults.items() if k[0]==origin}
   fresh=[(key,at,rows) for key,at,rows in pages if 0<=now-at<=PROGRAMME_MAX_AGE]
   updated=dict(status,pageFaults=faults,freshPages=len(fresh),freshChannels=len(set().union(*(set(r) for _,_,r in fresh))),fault=next(iter(faults.values()),None) if pages or faults else status.get('fault'))
   if pages:updated['observedAt']=max(at for _,at,_ in pages)
   if origin=='expanded':expanded_manifest_status=updated
   elif origin=='canada':canada_manifest_status=updated
   else:uk_manifest_status=updated
  primary_faults=[v for k,v in paged_faults.items() if k[0]=='primary']
  programme_fault=primary_faults[0] if primary_faults else None
  programme_refresh_failures=sum(paged_failures.values())
  programme_next_attempt=max(paged_origin_block_until,min(paged_due.values(),default=now+5))

def paged_programme_worker(stop):
 global programme_last_attempt
 while not stop.wait(5):
  config=gateway_config;now=time.time()
  paged_refresh_status(now)
  if now<paged_origin_block_until:continue
  for origin,base,page in page_plan(config):
   if stop.is_set():return
   now=time.time()
   if now<paged_origin_block_until:break
   if now<paged_due.get((origin,page),0):continue
   with lock:programme_last_attempt=now
   refresh_programme_page(origin,base,page)
   paged_refresh_status()
   if stop.wait(1):return

def programme_worker(stop):
 global programme_fault,programme_refresh_failures,programme_last_attempt,programme_next_attempt
 if expanded_base(gateway_config) or canada_base(gateway_config):return paged_programme_worker(stop)
 delay=PROGRAMME_REFRESH
 failures=0
 while True:
  with lock:programme_next_attempt=time.time()+delay
  if stop.wait(delay):return
  with lock:programme_last_attempt=time.time()
  try:
   raw=catalogue()
   if len(raw)<(len(primary_source_ids) or len(sources))*.9:raise ValueError('Incomplete programme refresh')
   update_programmes(raw)
   failures=0;delay=PROGRAMME_REFRESH
   with lock:programme_refresh_failures=0
  except Exception as error:
   failures+=1
   reason,delay=programme_retry_delay(error,failures)
   with lock:
    programme_fault=reason
    programme_refresh_failures=failures
   log('programme refresh failed '+reason+'; retry in '+str(round(delay))+'s')

def initialize_catalogue():
 global catalogue_source,catalogue_fault
 cache=ROOT/'catalogue-metadata.json'
 prior=None
 fresh=None
 if cache.exists():
  data=json.loads(cache.read_text())
  if isinstance(data,dict) and len(data)>=MIN_CHANNELS and all(isinstance(k,str) and k.startswith('nltv:') and isinstance(v,dict) and isinstance(v.get('name'),str) for k,v in data.items()):
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
 primary_source_ids.clear();primary_source_ids.update(raw)
 expanded=expanded_catalogue(gateway_config,cache_fallback=True)
 canada=canada_catalogue(gateway_config,cache_fallback=True)
 raw=dict(raw)
 for sid,row in expanded.items():raw.setdefault(sid,row)
 for sid,row in canada.items():raw.setdefault(sid,row)
 for i,(source_id,row) in enumerate(raw.items(),1):
  cid=source_profiles.channel_id(SOURCE_PROFILE,source_id)
  genres=row.get('genres',[])
  if not isinstance(genres,list):genres=[]
  channels[cid]={'Id':cid,'Name':row['name'],'ChannelNumber':str(i),'Type':'TvChannel','Genres':[v for v in genres if isinstance(v,str)],'Tags':[],'UserData':{'IsFavorite':False}}
  sources[cid]=source_id
 if fresh is not None:update_programmes(fresh,expanded=expanded,canada=canada)
 # A pre-existing initialized database may acquire new channels. Preserve every
 # prior penalty and insert only newly discovered IDs; the observer handles initial migration.
 with closing(policy_connect(ROOT/'policy.sqlite3')) as db, db:
  seeded=db.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone()
  if seeded or SOURCE_PROFILE=='malay':
   db.executemany("INSERT OR IGNORE INTO channel(id,name) VALUES(?,?)",[(cid,c['Name']) for cid,c in channels.items()])
   if not seeded:
    # New isolated prototype has no legacy native history to migrate.
    # The very same policy observer and strict penalty implementation apply.
    db.execute("INSERT INTO meta VALUES('seeded','1')")
    db.execute("INSERT INTO meta VALUES('seed_time',?)",(str(time.time()),))
def resolve_bases(source_id,config):
 # Exact upstream bases resolve() may try; also used to reserve request budget
 # before a foreground PlaybackInfo admission wait.
 bases=[None]
 if source_id in canada_cohort(config) and canada_base(config):
  bases=[canada_base(config)]
 elif source_id in expanded_cohort(config) and expanded_base(config):
  bases=[expanded_base(config)] if source_id not in primary_source_ids or config['ukTrialEnabled'] else [expanded_base(config),None]
 elif source_id in uk_cohort(config) and uk_base(config):
  bases=[uk_base(config)] if config['ukTrialEnabled'] else [uk_base(config),None]
 return bases
def resolve(cid):
 source_id=sources[cid]
 candidates=[];local_rejections=0;other_rejections=0
 config=gateway_config
 path='/stream/tv/'+urllib.parse.quote(source_id,safe=':')+'.json'
 bases=resolve_bases(source_id,config)
 for base in bases:
  try:response=provider_json(path) if base is None else provider_json(path,base)
  except Exception:
   if len(bases)==1:raise
   other_rejections+=1
   continue
  streams=response.get('streams',[])
  if not isinstance(streams,list) or not streams:other_rejections+=1;continue
  for v in streams:
   if not isinstance(v,dict):other_rejections+=1;continue
   url=v.get('url')
   if not isinstance(url,str):other_rejections+=1;continue
   if url in candidates:continue
   try:validate(url,media=True)
   except MediaCompatibilityError:local_rejections+=1;continue
   except Exception:other_rejections+=1;continue
   candidates.append(url)
 if not candidates:
  if local_rejections and not other_rejections:raise MediaCompatibilityError('Only unconfigured public media endpoints')
  raise ValueError('No usable stream source')
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
 with closing(policy_connect(ROOT/'policy.sqlite3')) as db, db:
  db.execute('UPDATE channel SET last_attempt=max(last_attempt,?) WHERE id=?',(time.time(),cid))
def policy_rows():
 # No false readiness marker or silent fail-open when policy storage fails.
 with closing(sqlite3.connect((ROOT/'policy.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
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
def _language_membership(state):
 return (spanish_classification.excluded(state),
         spanish_classification.manual_excluded(state),
         spanish_classification.auto_excluded(state))
def maybe_reload_spanish(force=False):
 global spanish_state,spanish_provider_ids,spanish_manual_provider_ids,spanish_auto_provider_ids,spanish_fingerprint,spanish_checked_at
 if SOURCE_PROFILE!='american':return
 now=time.monotonic()
 if not force and now-spanish_checked_at<2:return
 spanish_checked_at=now
 try:
  try:
   stat=SPANISH_FILE.stat();fingerprint=(stat.st_mtime_ns,stat.st_size,stat.st_ino)
   if not force and fingerprint==spanish_fingerprint:return
  except OSError:
   fingerprint=None
  state=spanish_classification.load_or_migrate(ROOT)
  try:
   stat=SPANISH_FILE.stat();fingerprint=(stat.st_mtime_ns,stat.st_size,stat.st_ino)
  except OSError:
   fingerprint=None
  if state!=spanish_state:
   spanish_state=state
   spanish_provider_ids,spanish_manual_provider_ids,spanish_auto_provider_ids=_language_membership(state)
   log('Language exclusions loaded entries='+str(len(spanish_provider_ids))+' manual='+str(len(spanish_manual_provider_ids))+' auto='+str(len(spanish_auto_provider_ids)))
  spanish_fingerprint=fingerprint
 except (ValueError,OSError,TypeError) as error:
  log('Language exclusions ignored '+type(error).__name__)
def spanish_excluded(cid):
 if SOURCE_PROFILE!='american':return False
 provider=sources.get(cid)
 if not provider:return False
 if provider in spanish_manual_provider_ids:return True
 if not reload_spanish_auto_control():return False
 return bool(SPANISH_AUTO_ENFORCEMENT and provider in spanish_auto_provider_ids)
def device_rotation_state(force=False):
 # Read-only device state used to avoid changing playback while the user has
 # paused or stopped rotation. No state-changing command is issued here. It goes
 # through the same admission/backoff path as other GET status consumers.
 global device_rotation_cache,device_rotation_checked
 now=time.monotonic()
 if not force and now-device_rotation_checked<2:return device_rotation_cache
 device_rotation_checked=now
 try:probe=device_language_probe(origin='rotation')
 except Exception:
  # A selector/config read error must not escape into catalogue rendering.
  device_rotation_cache=None;return None
 if not isinstance(probe,dict):
  device_rotation_cache=None;return None
 if type(probe.get('surfPaused')) is not bool or type(probe.get('surfing')) is not bool:
  device_rotation_cache=None;return None
 device_rotation_cache={'launchID':str(probe.get('launchID',''))[:160],
  'streamGeneration':str(probe.get('streamGeneration',''))[:160],
  'currentChannelID':str(probe.get('channelID') or ''),
  'surfPaused':probe.get('surfPaused') is True,'surfing':probe.get('surfing') is True,
  'rotationOwner':str(probe.get('rotationOwner',''))[:80],'observedAt':time.time()}
 return device_rotation_cache
def language_visible(cid):
 if not spanish_excluded(cid):return True
 entry=spanish_state['entries'].get(sources.get(cid))
 if not entry or entry.get('source')=='manual':return False
 state=device_rotation_state()
 if state is None:return True
 if state.get('surfPaused') and state.get('currentChannelID')==cid:return True
 return False
def language_visit_identity():
 global language_visit_ledger
 if language_visit_ledger is None:
  language_visit_ledger=language_visits.Ledger(ROOT)
 return language_visit_ledger
playback_marker_lock=threading.Lock()
def enqueue_playback_marker(config,device_token):
 # Bound optional UI callbacks to one in flight.
 if not playback_marker_lock.acquire(blocking=False):return False
 def run():
  try:apply_device_spanish_request(config,device_token)
  except Exception as error:log('Playback marker worker '+type(error).__name__)
  finally:playback_marker_lock.release()
 try:threading.Thread(target=run,name='playback-marker',daemon=True).start()
 except Exception:
  playback_marker_lock.release()
  return False
 return True

def apply_device_spanish_request(config, device_token):
 global spanish_state,spanish_provider_ids,spanish_manual_provider_ids,spanish_auto_provider_ids,spanish_fingerprint
 if SOURCE_PROFILE!='american':return
 reload_spanish_auto_control()
 try:
  raw=prototype_selector.device_ui_trigger(config,device_token,SPANISH_UI_NODE)
 except Exception as error:
  log('Spanish marker read failed '+type(error).__name__)
  return
 marker=spanish_classification.parse_marker(raw)
 if marker is None:return
 if marker['action']=='mistaken_english' and not overlay_correction_allowed(marker['channelID']):
  log('LANGUAGE_CORRECTION_STALE channel='+marker['channelID'])
  return
 provider_id=sources.get(marker['channelID'])
 retracted_event_id=''
 try:
  with lock:
   # Reload under the writer lock so a pending automatic evidence commit can
   # never clobber a manual correction made since the last fingerprint check.
   fresh=spanish_classification.load_or_migrate(ROOT)
   if marker['action']=='mistaken_english':
    state,info=spanish_classification.retract_evidence(
     fresh,provider_id,marker['channelID'],marker['channelName'],marker['nonce'],
     threshold=SPANISH_EVIDENCE_THRESHOLD,enforcement=SPANISH_AUTO_ENFORCEMENT)
    changed=bool(info.get('changed'));reason=str(info.get('reason'))
    retracted_event_id=str(info.get('retractedEventID') or '')
   else:
    state,changed,reason=spanish_classification.apply_manual(fresh,marker['action'],marker['channelID'],marker['channelName'],provider_id,marker['nonce'])
   spanish_state=fresh
   spanish_provider_ids,spanish_manual_provider_ids,spanish_auto_provider_ids=_language_membership(fresh)
   if not changed:return
   spanish_classification.atomic_write(ROOT,state)
   spanish_state=state
   spanish_provider_ids,spanish_manual_provider_ids,spanish_auto_provider_ids=_language_membership(state)
   try:
    stat=SPANISH_FILE.stat();spanish_fingerprint=(stat.st_mtime_ns,stat.st_size,stat.st_ino)
   except OSError:spanish_fingerprint=None
 except (ValueError,OSError,TypeError) as error:
  log('Spanish marker rejected '+type(error).__name__)
  return
 log('LANGUAGE_EXCLUSION '+reason+' provider='+(provider_id or 'unknown')+' channel='+marker['channelID']+' entries='+str(len(spanish_provider_ids))+' manual='+str(len(spanish_manual_provider_ids))+' auto='+str(len(spanish_auto_provider_ids)))
 if retracted_event_id:
  try:english_count=language_exclusions.counts_for(state,marker['channelID'],'en')
  except (ValueError,TypeError):english_count=None
  overlay_publish_correction(marker['channelID'],marker['channelName'],retracted_event_id,english_count,SPANISH_EVIDENCE_THRESHOLD)
SPANISH_AUTO_CONTROL_KEYS={'schemaVersion','enabled','enforcementEnabled','threshold','updatedAt'}
LEGACY_SPANISH_AUTO_CONTROL_KEYS={'schemaVersion','enabled','updatedAt'}
def _control_defaults():
 return {'schemaVersion':2,'enabled':False,'enforcementEnabled':False,'threshold':language_exclusions.DEFAULT_THRESHOLD,'updatedAt':0}
def reload_spanish_auto_control(force=False):
 global spanish_auto_control,SPANISH_AUTO_ENFORCEMENT,SPANISH_EVIDENCE_THRESHOLD,spanish_auto_control_fingerprint,spanish_auto_control_checked
 if SOURCE_PROFILE!='american':
  spanish_auto_control=False;SPANISH_AUTO_ENFORCEMENT=False;return False
 now=time.monotonic()
 if not force and now-spanish_auto_control_checked<2:return spanish_auto_control
 spanish_auto_control_checked=now
 try:
  path=SPANISH_AUTO_CONTROL_FILE
  if not path.exists() and LEGACY_SPANISH_AUTO_CONTROL_FILE.exists():
   path=LEGACY_SPANISH_AUTO_CONTROL_FILE
  if not path.exists():
   # The live detector stays off until observation is explicitly enabled and
   # the evidence gate has been reviewed on this host.
   defaults=_control_defaults();spanish_auto_control=False;SPANISH_AUTO_ENFORCEMENT=False;SPANISH_EVIDENCE_THRESHOLD=defaults['threshold']
   spanish_auto_control_fingerprint=None;return False
  stat=path.stat();fingerprint=(path.name,stat.st_mtime_ns,stat.st_size,stat.st_ino)
  if not force and fingerprint==spanish_auto_control_fingerprint:return spanish_auto_control
  if stat.st_size>4096:raise ValueError('Language auto control too large')
  data=json.loads(path.read_text())
  if not isinstance(data,dict):raise ValueError('Invalid language auto control')
  if set(data)==SPANISH_AUTO_CONTROL_KEYS and data.get('schemaVersion')==2:
   enabled=data.get('enabled');enforcement=data.get('enforcementEnabled');threshold=data.get('threshold')
   if type(enabled) is not bool or type(enforcement) is not bool or type(threshold) is not int or not language_exclusions.MIN_THRESHOLD<=threshold<=language_exclusions.MAX_THRESHOLD or type(data.get('updatedAt')) not in (int,float):
    raise ValueError('Invalid language auto control')
  elif set(data)==LEGACY_SPANISH_AUTO_CONTROL_KEYS and data.get('schemaVersion')==1:
   if type(data.get('enabled')) is not bool or type(data.get('updatedAt')) not in (int,float):raise ValueError('Invalid language auto control')
   enabled=data['enabled'];enforcement=False;threshold=language_exclusions.DEFAULT_THRESHOLD
  else:
   raise ValueError('Invalid language auto control')
  spanish_auto_control_fingerprint=fingerprint
  spanish_auto_control=enabled;SPANISH_AUTO_ENFORCEMENT=enforcement;SPANISH_EVIDENCE_THRESHOLD=threshold
 except (ValueError,OSError,TypeError) as error:
  # Fail closed for observation and enforcement; existing evidence and manual
  # decisions are untouched.
  spanish_auto_control=False;SPANISH_AUTO_ENFORCEMENT=False;SPANISH_EVIDENCE_THRESHOLD=language_exclusions.DEFAULT_THRESHOLD;spanish_auto_control_fingerprint=None
  log('Language auto control ignored '+type(error).__name__)
 return spanish_auto_control
def set_language_auto_control(enabled=None,enforcement=None,threshold=None):
 global spanish_auto_control,SPANISH_AUTO_ENFORCEMENT,SPANISH_EVIDENCE_THRESHOLD,spanish_auto_control_fingerprint,spanish_auto_control_checked
 if SOURCE_PROFILE!='american':raise ValueError('Language auto is English-trial only')
 reload_spanish_auto_control(force=True)
 current_enabled=bool(spanish_auto_control);current_enforcement=bool(SPANISH_AUTO_ENFORCEMENT);current_threshold=int(SPANISH_EVIDENCE_THRESHOLD)
 if enabled is not None:
  if type(enabled) is not bool:raise ValueError('Invalid language auto control')
  current_enabled=enabled
 if enforcement is not None:
  if type(enforcement) is not bool:raise ValueError('Invalid language enforcement control')
  current_enforcement=enforcement
 if threshold is not None:
  if type(threshold) is not int or not language_exclusions.MIN_THRESHOLD<=threshold<=language_exclusions.MAX_THRESHOLD:raise ValueError('Invalid language evidence threshold')
  current_threshold=threshold
 control={'schemaVersion':2,'enabled':current_enabled,'enforcementEnabled':current_enforcement,'threshold':current_threshold,'updatedAt':time.time()}
 atomic_json(SPANISH_AUTO_CONTROL_FILE,control)
 spanish_auto_control=current_enabled;SPANISH_AUTO_ENFORCEMENT=current_enforcement;SPANISH_EVIDENCE_THRESHOLD=current_threshold;spanish_auto_control_checked=time.monotonic()
 try:
  stat=SPANISH_AUTO_CONTROL_FILE.stat();spanish_auto_control_fingerprint=(stat.st_mtime_ns,stat.st_size,stat.st_ino)
 except OSError:spanish_auto_control_fingerprint=None
 if spanish_auto_manager is not None:spanish_auto_manager.refresh_control()
 log('LANGUAGE_AUTO_CONTROL enabled='+str(current_enabled)+' enforcement='+str(current_enforcement)+' threshold='+str(current_threshold))
 return control
def set_spanish_auto_enabled(enabled):
 return set_language_auto_control(enabled=enabled)
MAINTENANCE_KEYS={'schemaVersion','start','until','reason','launchID','channelID','allowedReasons'}
def maintenance_window(force=False):
 global maintenance_state,maintenance_fingerprint,maintenance_checked_at
 now=time.monotonic()
 # 0 is the "never checked" sentinel; some platforms start monotonic near 0.
 if not force and maintenance_checked_at and now-maintenance_checked_at<2:return maintenance_state
 maintenance_checked_at=now
 try:
  if not MAINTENANCE_FILE.exists():
   maintenance_fingerprint=None;maintenance_state=None;return None
  stat=MAINTENANCE_FILE.stat();fingerprint=(stat.st_mtime_ns,stat.st_size,stat.st_ino)
  if not force and fingerprint==maintenance_fingerprint:return maintenance_state
  if stat.st_size>8192:raise ValueError('Maintenance window too large')
  data=json.loads(MAINTENANCE_FILE.read_text())
  if not isinstance(data,dict) or set(data)!=MAINTENANCE_KEYS or data.get('schemaVersion')!=1:raise ValueError('Invalid maintenance window')
  start=data.get('start');until=data.get('until')
  if type(start) not in (int,float) or type(until) not in (int,float) or not 0<=start<=until or until-start>900:raise ValueError('Invalid maintenance interval')
  if not isinstance(data.get('reason'),str) or not data['reason'] or len(data['reason'])>160:raise ValueError('Invalid maintenance reason')
  if not isinstance(data.get('launchID'),str) or len(data['launchID'])>100:raise ValueError('Invalid maintenance launch')
  channel=data.get('channelID')
  if channel and (not isinstance(channel,str) or not re.fullmatch(r'[a-f0-9]{32}',channel)):raise ValueError('Invalid maintenance channel')
  reasons=data.get('allowedReasons')
  if not isinstance(reasons,list) or len(reasons)>24 or any(not isinstance(item,str) or not item or len(item)>80 for item in reasons):raise ValueError('Invalid maintenance reasons')
  maintenance_fingerprint=fingerprint;maintenance_state=data
 except (ValueError,OSError,TypeError) as error:
  maintenance_fingerprint=None;maintenance_state=None
  log('Maintenance window ignored '+type(error).__name__)
 return maintenance_state
def maintenance_ignores(reason, now=None):
 window=maintenance_window()
 if window is None:return None
 current=time.time() if now is None else now
 if not window['start']<=current<=window['until']:return None
 text=(reason or '').casefold()
 for allowed in window['allowedReasons']:
  if allowed.casefold() in text:return window
 return None
def record_maintenance_ignored(cid,trace,reason,now):
 entry={'at':now,'channelID':cid,'trace':trace,'reason':str(reason)[:300]}
 try:
  fd=os.open(MAINTENANCE_IGNORED_FILE,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
  try:os.write(fd,(json.dumps(entry,separators=(',',':'))+'\n').encode())
  finally:os.close(fd)
 except OSError:pass
def resolve_whisper_model():
 candidates=[]
 explicit=os.environ.get('JEALOUSFIN_WHISPER_MODEL','')
 if explicit:candidates.append(Path(explicit))
 official=Path.home()/'.local'/'share'/'whisper.cpp'/'models'
 home=Path.home()/'.config'/'jealousfin'/'models'
 # Prefer the official base model now available at the standard whisper.cpp
 # path; keep the verified small model as a fallback for multilingual edge
 # cases and for installations that only have the larger file.
 candidates.extend([official/'ggml-base.bin',official/'ggml-small.bin',
                    home/'ggml-base.bin',home/'ggml-small.bin',
                    ROOT/'models'/'ggml-base.bin',ROOT/'models'/'ggml-small.bin'])
 for path in candidates:
  try:
   if path.is_file() and path.stat().st_size>1024*1024:return path
  except OSError:pass
 return None
DEVICE_PROBE_WAIT_SECONDS=25.0
DEVICE_PROBE_LOCK=threading.Lock()
DEVICE_PROBE_CONDITION=threading.Condition(DEVICE_PROBE_LOCK)
DEVICE_PROBE_INFLIGHT=False
DEVICE_PROBE_VERSION=0
DEVICE_PROBE_CURRENT=None
DEVICE_PROBE_SHARED=None
DEVICE_PROBE_SHARED_FAULT=''
DEVICE_PROBE_SHARED_VERSION=-1
DEVICE_PROBE_STATS={'probes':0,'shared':0,'failures':0,'timeouts':0,
 'admitted':0,'rejectedBackoff':0,'rejectedRate':0,'freshAdmitted':0,
 'freshRejected':0,'backoffStarted':0,'recovered':0,'byReason':{},'byOrigin':{}}
DEVICE_PROBE_BACKOFF_BASE=2.0
DEVICE_PROBE_BACKOFF_MAX=20.0
DEVICE_PROBE_BACKOFF_RESET=120.0
DEVICE_PROBE_MIN_SPACING=0.5
DEVICE_PROBE_STATUS_REASONS=frozenset((
 'device_status_unavailable','device_status_invalid'))
DEVICE_PROBE_IDENTITY_REASONS=frozenset((
 'device_snapshot_unavailable','device_snapshot_invalid','device_incoherent'))
DEVICE_PROBE_BACKOFF_REASONS=DEVICE_PROBE_STATUS_REASONS|DEVICE_PROBE_IDENTITY_REASONS
# The installed native adapter sends PlaybackInfo with a verified 20 s request
# deadline.  A foreground admission may wait out an existing status backoff,
# but only when a fresh category read and the following upstream resolution
# still fit inside that deadline.  Background catalogue/policy consumers never
# take this path.
PLAYBACK_ADMISSION_BUDGET_SECONDS=20.0
PLAYBACK_ADMISSION_MARGIN_SECONDS=1.0
PLAYBACK_ADMISSION_WAIT=time.sleep
PLAYBACK_ADMISSION_LOCK=threading.Lock()
PLAYBACK_ADMISSION_FLIGHTS={}
PLAYBACK_ADMISSION_STATS={'started':0,'shared':0,'refused':0,'recovered':0,
 'failures':0,'timeouts':0}
DEVICE_PROBE_CLOCK=time.monotonic
DEVICE_PROBE_BACKOFF={}
DEVICE_PROBE_IDENTITY_BACKOFF={}
device_probe_fault=''
DEVICE_PROBE_LOCAL=threading.local()
def _device_probe_copy(value):
 return dict(value) if isinstance(value,dict) else value
def _device_probe_set_local_fault(reason):
 # Per-call attribution.  A fresh probe must never overwrite the fault of a
 # coalesced flight that is still running on another worker thread.
 DEVICE_PROBE_LOCAL.fault=reason or ''
def _device_probe_local_fault():
 return getattr(DEVICE_PROBE_LOCAL,'fault','')
def _device_probe_now():
 try:return float(DEVICE_PROBE_CLOCK())
 except (TypeError,ValueError):return time.monotonic()
def _device_probe_config():
 try:return prototype_selector.load_selector(ROOT)
 except (OSError,ValueError,TypeError):return None
def _device_probe_key(config):
 if not isinstance(config,dict):return ''
 address=config.get('deviceIP');port=config.get('backendPort')
 if not isinstance(address,str) or not address:return ''
 return address+':'+str(port if port is not None else '')
def _device_probe_status_remaining_locked(config,now):
 key=_device_probe_key(config);state=DEVICE_PROBE_BACKOFF.get(key) if key else None
 if not isinstance(state,dict):return 0.0
 if now-state.get('at',0)>DEVICE_PROBE_BACKOFF_RESET:
  DEVICE_PROBE_BACKOFF.pop(key,None);return 0.0
 return max(0.0,state.get('until',0)-now,
  state.get('attemptAt',0)+DEVICE_PROBE_MIN_SPACING-now)
def _device_probe_origin(origin):
 name=origin if isinstance(origin,str) and origin else 'unspecified'
 bucket=DEVICE_PROBE_STATS['byOrigin'].get(name)
 if not isinstance(bucket,dict):
  bucket={'admitted':0,'rejected':0,'shared':0,'failures':0}
  DEVICE_PROBE_STATS['byOrigin'][name]=bucket
 return bucket
def _device_probe_backoff_tables(lanes):
 tables=[]
 for lane in lanes:
  table=DEVICE_PROBE_IDENTITY_BACKOFF if lane=='identity' else DEVICE_PROBE_BACKOFF
  if table not in tables:tables.append(table)
 return tables
def _device_probe_failure_tables(reason):
 if reason in DEVICE_PROBE_STATUS_REASONS:return (DEVICE_PROBE_BACKOFF,)
 if reason in DEVICE_PROBE_IDENTITY_REASONS:return (DEVICE_PROBE_IDENTITY_BACKOFF,)
 return ()
def _device_probe_admit_locked(config,origin,fresh,lanes=('status',)):
 now=_device_probe_now();key=_device_probe_key(config)
 # A bounded foreground recovery owns the next status admission. Do not
 # start new competing identity/profile reads during its existing wait.
 if origin!='playback-recovery' and 'status' in lanes:
  with PLAYBACK_ADMISSION_LOCK:
   reserved=any(flight_key[0]==key for flight_key in PLAYBACK_ADMISSION_FLIGHTS)
  if reserved:
   _device_probe_origin(origin)['rejected']+=1
   if fresh:DEVICE_PROBE_STATS['freshRejected']+=1
   _device_probe_set_local_fault('device_probe_playback_reserved');return False
 for table in _device_probe_backoff_tables(lanes):
  state=table.get(key) if key else None
  if isinstance(state,dict):
   if now-state.get('at',0)>DEVICE_PROBE_BACKOFF_RESET:
    table.pop(key,None);state=None
   elif now<state.get('until',0):
    DEVICE_PROBE_STATS['rejectedBackoff']+=1
    _device_probe_origin(origin)['rejected']+=1
    if fresh:DEVICE_PROBE_STATS['freshRejected']+=1
    _device_probe_set_local_fault('device_probe_backoff');return False
   elif now-state.get('attemptAt',0)<DEVICE_PROBE_MIN_SPACING:
    DEVICE_PROBE_STATS['rejectedRate']+=1
    _device_probe_origin(origin)['rejected']+=1
    if fresh:DEVICE_PROBE_STATS['freshRejected']+=1
    _device_probe_set_local_fault('device_probe_rate_limited');return False
 DEVICE_PROBE_STATS['admitted']+=1
 _device_probe_origin(origin)['admitted']+=1
 if fresh:DEVICE_PROBE_STATS['freshAdmitted']+=1
 if key:
  for table in _device_probe_backoff_tables(lanes):
   state=table.get(key)
   if isinstance(state,dict):state['attemptAt']=now
 _device_probe_set_local_fault('');return True
def _device_probe_record_success_locked(key,lanes=('status','identity')):
 if not key:return
 for table in _device_probe_backoff_tables(lanes):
  if isinstance(table.pop(key,None),dict):
   DEVICE_PROBE_STATS['recovered']+=1
def _device_probe_record_failure_locked(key,reason,origin=None):
 if not key:return
 tables=_device_probe_failure_tables(reason)
 if not tables:return
 now=_device_probe_now()
 for table in tables:
  state=table.get(key)
  previous=state.get('failures') if isinstance(state,dict) else 0
  if type(previous) is not int or previous<0:previous=0
  if isinstance(state,dict) and now-state.get('at',0)>DEVICE_PROBE_BACKOFF_RESET:previous=0
  failures=min(previous+1,16)
  delay=min(DEVICE_PROBE_BACKOFF_BASE*(2**(failures-1)),DEVICE_PROBE_BACKOFF_MAX)
  table[key]={'failures':failures,'until':now+delay,
   'at':now,'attemptAt':now,'reason':reason}
  if previous==0:DEVICE_PROBE_STATS['backoffStarted']+=1
  while len(table)>16:
   table.pop(next(iter(table)))
 if origin:_device_probe_origin(origin)['failures']+=1
class _DeviceProbeFlight:
 # One owner plus its exact waiters.  The completion event is the single
 # handoff point: a waiter can never mistake a newer flight for the one it
 # joined, and a timeout never starts a duplicate owner.
 __slots__=('version','event','result','fault','exception','key','origin')
 def __init__(self,version,key='',origin='unspecified'):
  self.version=version;self.event=threading.Event()
  self.result=None;self.fault='';self.exception=None
  self.key=key;self.origin=origin
def device_identity_pair(config,token):
 # The GET route is read-only and does not create durable command receipts.
 # New native builds provide channel and generation in a single atomic status.
 # Legacy installed IPAs need a separate player snapshot for stream identity.
 try:status=prototype_selector.device_status(config,token)
 except Exception:
  _device_probe_set_local_fault('device_status_unavailable');raise
 if not isinstance(status,dict):
  _device_probe_set_local_fault('device_status_invalid');raise ValueError('Invalid device state')
 channel=status.get('currentChannelID');generation=status.get('streamGeneration')
 if isinstance(channel,str) and channel and isinstance(generation,str) and generation:
  return status,{'channelID':channel,'streamGeneration':generation}
 try:snapshot=json.loads(prototype_selector.device_command(config,token,'get_player_snapshot',{}))
 except Exception:
  _device_probe_set_local_fault('device_snapshot_unavailable');raise
 if not isinstance(snapshot,dict):
  _device_probe_set_local_fault('device_snapshot_invalid');raise ValueError('Invalid player snapshot')
 return status,snapshot

def _device_language_probe_once():
 # One coherent read: channel, launch and generation must describe the same
 # instant.  An incoherent status/snapshot pair is retried once, then fails
 # closed with a stable reason instead of an opaque 'device_unavailable'.
 _device_probe_set_local_fault('')
 config=prototype_selector.load_selector(ROOT)
 if config is None:
  _device_probe_set_local_fault('device_not_configured');return None
 token=os.environ.get('JEALOUSFIN_DEVICE_TOKEN','')
 incoherent=False
 for _ in range(2):
  try:
   status,snapshot=device_identity_pair(config,token)
  except Exception:
   return None
  channel_id=str(snapshot.get('channelID',''))
  status_channel=str(status.get('currentChannelID','') or '')
  stream_generation=str(snapshot.get('streamGeneration',''))
  status_stream=str(status.get('streamGeneration','') or '')
  if status_channel and channel_id and status_channel!=channel_id:
   incoherent=True;continue
  if status_stream and stream_generation and status_stream!=stream_generation:
   incoherent=True;continue
  paused=status.get('surfPaused') if type(status.get('surfPaused')) is bool else None
  surfing=status.get('surfing') if type(status.get('surfing')) is bool else None
  playing=status.get('playing') if type(status.get('playing')) is bool else None
  _device_probe_set_local_fault('')
  return {'channelID':channel_id or status_channel,'launchID':str(status.get('launchID','') or ''),
          'streamGeneration':stream_generation,'surfPaused':paused,'surfing':surfing,
          'playing':playing,'rotationOwner':str(status.get('rotationOwner','') or '')[:80]}
 if incoherent:_device_probe_set_local_fault('device_incoherent')
 return None

def _device_probe_record_completion(flight,value,fault,error,count_probe):
 global device_probe_fault,DEVICE_PROBE_INFLIGHT,DEVICE_PROBE_CURRENT
 global DEVICE_PROBE_SHARED,DEVICE_PROBE_SHARED_FAULT,DEVICE_PROBE_SHARED_VERSION
 with DEVICE_PROBE_LOCK:
  flight.result=_device_probe_copy(value)
  flight.fault=fault or ''
  flight.exception=error
  flight.event.set()
  DEVICE_PROBE_SHARED=_device_probe_copy(value)
  DEVICE_PROBE_SHARED_FAULT=flight.fault
  DEVICE_PROBE_SHARED_VERSION=flight.version
  DEVICE_PROBE_INFLIGHT=False
  if DEVICE_PROBE_CURRENT is flight:DEVICE_PROBE_CURRENT=None
  device_probe_fault=flight.fault
  if count_probe:
   DEVICE_PROBE_STATS['probes']+=1
   if isinstance(value,dict):
    _device_probe_record_success_locked(flight.key,('status','identity'))
   else:
    DEVICE_PROBE_STATS['failures']+=1
    reasons=DEVICE_PROBE_STATS['byReason']
    reason=flight.fault or 'device_unavailable'
    reasons[reason]=(reasons.get(reason,0)+1)
    _device_probe_record_failure_locked(flight.key,reason,flight.origin)
  DEVICE_PROBE_CONDITION.notify_all()
def device_language_probe(share=True,origin='unspecified'):
 # Single-flight, not a launch cache.  A probe that is genuinely in flight is
 # shared by callers that arrive while it runs; a probe that already finished
 # is never reused.  Sequential probes therefore always read a fresh launch,
 # while concurrent language/episode/overlay probes after a new launch collapse
 # to one coherent read instead of several 3s/8s device calls.  A failed read
 # opens a bounded per-device backoff; fresh checks never reuse the last
 # positive identity and fail closed while that backoff is active.
 global DEVICE_PROBE_INFLIGHT,DEVICE_PROBE_VERSION,DEVICE_PROBE_CURRENT
 config=_device_probe_config();key=_device_probe_key(config)
 if not share:
  # Independently fresh observation.  It owns its own thread-local fault and
  # never joins or mutates the coalesced flight.
  with DEVICE_PROBE_LOCK:
   if not _device_probe_admit_locked(config,origin,True,('status','identity')):return None
  value=None;fault=''
  try:
   value=_device_language_probe_once()
   fault=_device_probe_local_fault()
  except BaseException:
   reason=_device_probe_local_fault() or 'device_probe_exception'
   with DEVICE_PROBE_LOCK:
    DEVICE_PROBE_STATS['probes']+=1
    DEVICE_PROBE_STATS['failures']+=1
    reasons=DEVICE_PROBE_STATS['byReason']
    reasons[reason]=(reasons.get(reason,0)+1)
    _device_probe_record_failure_locked(key,reason,origin)
   raise
  with DEVICE_PROBE_LOCK:
   DEVICE_PROBE_STATS['probes']+=1
   if isinstance(value,dict):_device_probe_record_success_locked(key,('status','identity'))
   else:
    reason=fault or 'device_unavailable'
    DEVICE_PROBE_STATS['failures']+=1
    reasons=DEVICE_PROBE_STATS['byReason']
    reasons[reason]=(reasons.get(reason,0)+1)
    _device_probe_record_failure_locked(key,reason,origin)
  return value
 with DEVICE_PROBE_LOCK:
  flight=DEVICE_PROBE_CURRENT
  if flight is None or flight.event.is_set():
   if not _device_probe_admit_locked(config,origin,False,('status','identity')):return None
   DEVICE_PROBE_VERSION+=1
   flight=_DeviceProbeFlight(DEVICE_PROBE_VERSION,key,origin)
   DEVICE_PROBE_CURRENT=flight
   DEVICE_PROBE_INFLIGHT=True
   owner=True
  else:
   owner=False
 if not owner:
  if not flight.event.wait(DEVICE_PROBE_WAIT_SECONDS):
   # Fail closed: do not start another owner while the original is still
   # running.  A later caller will start a new flight once it completes.
   with DEVICE_PROBE_LOCK:
    DEVICE_PROBE_STATS['timeouts']+=1
    reasons=DEVICE_PROBE_STATS['byReason']
    reasons['device_probe_timeout']=(reasons.get('device_probe_timeout',0)+1)
   _device_probe_set_local_fault('device_probe_timeout')
   return None
  with DEVICE_PROBE_LOCK:
   DEVICE_PROBE_STATS['shared']+=1
   _device_probe_origin(origin)['shared']+=1
   device_probe_fault=flight.fault
  _device_probe_set_local_fault(flight.fault)
  if flight.exception is not None:raise flight.exception
  return _device_probe_copy(flight.result)
 value=None;fault='';error=None
 try:
  value=_device_language_probe_once()
  fault=_device_probe_local_fault()
 except BaseException as failure:
  error=failure
  fault=_device_probe_local_fault() or 'device_probe_exception'
 _device_probe_record_completion(flight,value,fault,error,True)
 if error is not None:raise error
 return value
def device_probe_diagnostics():
 with DEVICE_PROBE_LOCK:
  now=_device_probe_now();active=[]
  for table in (DEVICE_PROBE_BACKOFF,DEVICE_PROBE_IDENTITY_BACKOFF):
   for key,state in table.items():
    if isinstance(state,dict) and now<state.get('until',0):
     active.append((key,state))
  remaining=max((state['until']-now for _,state in active),default=0.0)
  failures=max((state.get('failures',0) for _,state in active),default=0)
  return {'probes':DEVICE_PROBE_STATS['probes'],'shared':DEVICE_PROBE_STATS['shared'],
          'failures':DEVICE_PROBE_STATS['failures'],
          'timeouts':DEVICE_PROBE_STATS['timeouts'],
          'admitted':DEVICE_PROBE_STATS['admitted'],
          'rejectedBackoff':DEVICE_PROBE_STATS['rejectedBackoff'],
          'rejectedRate':DEVICE_PROBE_STATS['rejectedRate'],
          'freshAdmitted':DEVICE_PROBE_STATS['freshAdmitted'],
          'freshRejected':DEVICE_PROBE_STATS['freshRejected'],
          'backoffStarted':DEVICE_PROBE_STATS['backoffStarted'],
          'recovered':DEVICE_PROBE_STATS['recovered'],
          'backoff':{'active':bool(active),'devices':len({key for key,_ in active}),
                     'remainingSeconds':round(max(0.0,remaining),3),
                     'consecutiveFailures':failures,
                     'capSeconds':DEVICE_PROBE_BACKOFF_MAX},
          'byOrigin':{name:dict(bucket) for name,bucket in DEVICE_PROBE_STATS['byOrigin'].items()},
          'byReason':dict(DEVICE_PROBE_STATS['byReason']),
          'fault':device_probe_fault,'inFlight':DEVICE_PROBE_INFLIGHT}
def playback_admission_diagnostics():
 with PLAYBACK_ADMISSION_LOCK:
  return dict(PLAYBACK_ADMISSION_STATS,inFlight=len(PLAYBACK_ADMISSION_FLIGHTS))
# Optional audio-assisted episode identity. It reuses the single language
# decode/transcribe pass through on_speech; it never requests media, starts a
# second Whisper run or calls a research service while disabled.
episode_observer=None
episode_source_discovery=None
episode_init_lock=threading.Lock()
episode_publisher=episode_audio.PublicationStore()
EPISODE_SOURCE_FILE=ROOT/'episode-sources.json'
EPISODE_SOURCE_LIMIT=65536
EPISODE_SOURCE_PAGES=8
EPISODE_SOURCE_HOSTS=episode_audio.OFFICIAL_ARCHIVE_HOSTS|episode_audio.INDEPENDENT_REPORT_HOSTS
def load_episode_sources():
 # Private, reviewed registry only. Unknown fields, hosts or oversized files
 # are ignored so an unsafe registry can never widen the source allowlist.
 try:
  if not EPISODE_SOURCE_FILE.exists():return {}
  if EPISODE_SOURCE_FILE.stat().st_size>EPISODE_SOURCE_LIMIT:raise ValueError('Episode source registry too large')
  data=json.loads(EPISODE_SOURCE_FILE.read_text())
  if not isinstance(data,dict) or data.get('schemaVersion')!=1:raise ValueError('Invalid episode source registry')
  series=data.get('series')
  if not isinstance(series,dict) or len(series)>256:raise ValueError('Invalid episode source series')
  out={}
  for key,row in series.items():
   if not isinstance(key,str) or not 1<=len(key)<=200 or not isinstance(row,dict) or set(row)-{'official','reports'}:continue
   entry={}
   for kind,hosts in (('official',episode_audio.OFFICIAL_ARCHIVE_HOSTS),('reports',episode_audio.INDEPENDENT_REPORT_HOSTS)):
    urls=row.get(kind,[])
    if not isinstance(urls,list) or len(urls)>EPISODE_SOURCE_PAGES:raise ValueError('Invalid episode source list')
    valid=[]
    for url in urls:
     if not isinstance(url,str) or not re.fullmatch(r'https://[^\s]{1,500}',url):raise ValueError('Invalid episode source URL')
     host=urllib.parse.urlsplit(url).hostname
     if not isinstance(host,str) or host.lower() not in hosts:raise ValueError('Unapproved episode source host')
     valid.append(url)
    entry[kind]=valid
   out[episode_audio.source_key(key)]=entry
  return out
 except (OSError,ValueError,TypeError):return {}
def episode_research_page(url,timeout=8.0):
 # Bounded, HTTPS-only read of one approved public page. Never stores the page.
 target=episode_audio.safe_source_url(url,EPISODE_SOURCE_HOSTS,allow_query=True)
 if not target:raise ValueError('Unapproved episode source')
 original_host=urllib.parse.urlsplit(target).hostname
 class EpisodeRedirect(Redirect):
  def redirect_request(self,req,fp,code,msg,headers,newurl):
   redirect=episode_audio.safe_source_url(urllib.parse.urljoin(req.full_url,newurl),EPISODE_SOURCE_HOSTS,allow_query=True)
   if not redirect or not episode_audio._same_site(urllib.parse.urlsplit(redirect).hostname,original_host):
    raise ValueError('Unapproved episode source redirect')
   return super().redirect_request(req,fp,code,msg,headers,redirect)
 req=urllib.request.Request(validate(target),headers={'User-Agent':'Jealousfin-Episode-Evidence/1.0','Accept':'text/html,application/xhtml+xml'})
 agent=urllib.request.build_opener(urllib.request.ProxyHandler({}),EpisodeRedirect())
 with agent.open(req,timeout=max(0.01,min(8.0,float(timeout)))) as f:
  data=f.read(episode_audio.MAX_PAGE_BYTES+1)
  final=f.geturl()
 if not episode_audio._same_site(urllib.parse.urlsplit(final).hostname,original_host):raise ValueError('Episode source left approved host')
 if len(data)>episode_audio.MAX_PAGE_BYTES:raise ValueError('Episode evidence page too large')
 return data
def episode_source_provider(payload):
 # Search results are URL leads only; all pages are independently fetched,
 # parsed and corroborated. The direct lane does not depend on a paid model.
 if not gateway_config.get('episodeAudioEnabled'):return [], []
 diagnostics={}
 direct=opencode_discovery.public_episode_search(payload,validate,diagnostics=diagnostics)
 if diagnostics.get('queries') or diagnostics.get('backoff'):
  log('EPISODE_PUBLIC_SEARCH '+json.dumps(diagnostics,separators=(',',':')))
 official,reports=direct['official'],direct['reports']
 if official or reports:
  log('episode public search official=%d reports=%d'%(len(official),len(reports)))
 required_reports=2 if not payload.get('seriesTitle') else 1
 if official and len({episode_audio._host(u).removeprefix('www.') for u in reports})>=required_reports:return official,reports
 if not opencode_discovery.available():return official,reports
 try:
  # Clue-backed work is latency-sensitive. Keep the alternate provider bounded.
  unknown=not payload.get('seriesTitle') and bool(payload.get('clues'))
  timeout=min(float(gateway_config.get('metadataResearchTimeoutSeconds',180)),episode_audio.UNKNOWN_PROVIDER_SECONDS if unknown else 8 if payload.get('clues') else episode_audio.MAX_RESEARCH_SECONDS)
  prompt=opencode_discovery.episode_prompt_for(payload,payload.get('clues',()) if isinstance(payload,dict) else ())
  raw,_diagnostics=opencode_discovery.run_model_detailed(prompt,gateway_config['metadataResearchModel'],timeout,gateway_config['metadataResearchVariant'],episode_leads=unknown)
  result=opencode_discovery.normalize_episode_sources(raw)
  log('episode source discovery official=%d reports=%d'%(len(result['official']),len(result['reports'])))
  combined=opencode_discovery.normalize_episode_sources({'official':official+result['official'],'reports':reports+result['reports']})
  return combined['official'],combined['reports']
 except Exception as error:
  diagnostics=getattr(error,'diagnostics',{})
  reason=diagnostics.get('error') if isinstance(diagnostics,dict) else None
  safe_reason=reason if reason in ('opencode-error','invalid-answer','unexpected-tool','missing-search','no-search','no-completed-public-search','timeout-no-approved-leads','timeout') else type(error).__name__
  log('episode source discovery deferred '+safe_reason)
  return official,reports
def episode_source_prefetch_worker(stop):
 # Public programme candidates are cached before a transcript exists. This
 # worker never touches audio, language decisions, rotation or playback.
 while not stop.wait(15):
  try:
   reload_gateway_config()
   if not gateway_config.get('episodeAudioEnabled'):continue
   observer=episode_observer_instance()
   if observer is None:continue
   observer.enabled=True
   now=time.monotonic()
   with lock:active=[cid for cid,until in active_channels.items() if until>now]
   for cid in active[:4]:
    context=current_programme_context(cid)
    if not context or not context.get('seriesTitle') or episode_audio.programme_has_episode_identity(context):continue
    observer.prefetch(context)
  except Exception as error:
   log('episode source prefetch '+type(error).__name__)
def current_programme_context(channel_id):
 try:row=programme_snapshot().get(channel_id)
 except (sqlite3.Error,OSError,RuntimeError,ValueError):return None
 context=episode_audio.programme_context(row,channel_id)
 if context is not None:return context
 if channel_id not in channels:return None
 return {'channelID':channel_id,'seriesTitle':'','channelName':channels[channel_id].get('Name','')}
def episode_active_view(channel_id):
 view=playback_activity.views().get(channel_id)
 if view is None:return None
 try:row=programme_snapshot().get(channel_id) or {}
 except (sqlite3.Error,OSError,RuntimeError,ValueError):row={}
 return {'channel_id':channel_id,'session_hash':view.session_hash,
         'active_seconds':view.active_seconds,'confirmed_seconds':view.confirmed_seconds,
         'hasEpisodeIdentity':episode_audio.programme_has_episode_identity(row)}
def note_episode_probe(reason=None,detail=None):
 # Short, bounded diagnostics for every early return. No audio, transcript,
 # media URL or credential is retained.
 with lock:
  episode_probe_diagnostics['checks']=episode_probe_diagnostics.get('checks',0)+1
  if reason is None:
   episode_probe_diagnostics['accepted']=episode_probe_diagnostics.get('accepted',0)+1
  else:
   rejections=episode_probe_diagnostics.setdefault('rejections',{})
   rejections[reason]=rejections.get(reason,0)+1
  episode_probe_diagnostics['last']={'at':round(time.time(),3),'reason':reason or 'accepted','detail':detail}
def episode_probe():
 # Fresh coherent device identity plus the durable physical visit. The visit
 # is observed through the existing read-only ledger; no playback command is
 # issued. Read-only episode research/publication is allowed while surf
 # rotation is paused only when the player is validated and real playback is
 # still progressing. A stopped rotation, unknown pause state, actual player
 # pause or stale/absent playback evidence fails closed. The channel, launch,
 # physical visit and stream generation are still required, and a same-launch
 # reload keeps the same visit ID so the audio binding treats it as one
 # physical session.
 probe=device_language_probe(origin='episode')
 if not isinstance(probe,dict):
  # Keep the legacy bucket for older dashboards but expose the exact read
  # failure so a launch-time timeout is not confused with an incoherent pair.
  note_episode_probe('device_unavailable',_device_probe_local_fault() or 'device_unavailable');return None
 if probe.get('surfing') is not True:
  note_episode_probe('rotation_stopped');return None
 if probe.get('playing') is False:
  note_episode_probe('playback_paused');return None
 paused=probe.get('surfPaused')
 if paused is True:
  # Rotation is paused but the player may still be running. The existing
  # ActiveViewing view only exists for a non-stopped, non-paused, fresh
  # progressing playback session, so it is the actual-playback guard.
  if episode_active_view(probe.get('channelID')) is None:
   note_episode_probe('playback_inactive');return None
 elif paused is not False:
  note_episode_probe('rotation_unknown');return None
 try:visit=language_visit_identity().observe(probe.get('channelID'),probe.get('launchID'),probe.get('streamGeneration'),now=time.time())
 except Exception:
  note_episode_probe('visit_unavailable');return None
 if not isinstance(visit,dict) or visit.get('verified') is not True:
  note_episode_probe('visit_unverified');return None
 visit_id=str(visit.get('visitID','') or '')
 if not visit_id:
  note_episode_probe('visit_missing');return None
 result=dict(probe,visitID=visit_id,visitVerified=language_visits.is_verified(visit_id))
 note_episode_probe()
 return result
def episode_retry_callback(binding,reason):
 # Ask the existing language sampler for one bounded, episode-only retry in
 # the same physical visit. It reuses already-fetched media bytes and never
 # records language evidence again; actual playback still has to be running.
 manager=spanish_auto_manager
 if manager is None or not callable(getattr(manager,'request_episode_retry',None)):
  note_episode_probe('retry_unavailable',str(reason or ''));return False
 try:
  return bool(manager.request_episode_retry(
   {'cid':binding.channel_id,'visitID':binding.visit_id,
    'launchID':binding.launch_id,'streamGeneration':binding.stream_generation},
   str(reason or '')))
 except Exception as error:
  note_episode_probe('retry_error',type(error).__name__);return False
def episode_publication_callback(promotion,context,now):
 # Presentation only: enqueue the existing combined label after a bounded
 # episode identity is published. This never changes programme metadata.
 try:
  cid=promotion.episode.channel_id if promotion.episode is not None else ''
 except Exception:
  cid=''
 if not isinstance(cid,str) or not cid:return False
 return overlay_enqueue('combined',channelID=cid)
def episode_observer_instance():
 global episode_observer,episode_source_discovery
 if SOURCE_PROFILE!='american':return None
 with episode_init_lock:
  if episode_observer is None:
   episode_source_discovery=episode_audio.EpisodeSourceDiscovery(
    episode_research_page,registry_loader=load_episode_sources,
    provider=episode_source_provider,max_candidates=EPISODE_SOURCE_PAGES)
   research=episode_audio.PublicEvidenceResearch(episode_research_page,registry_loader=load_episode_sources,max_pages=EPISODE_SOURCE_PAGES,discovery=episode_source_discovery,unknown_deadline_seconds=episode_audio.UNKNOWN_RESEARCH_SECONDS,fetch_with_timeout=episode_research_page)
   episode_observer=episode_audio.EpisodeObserver(
    probe=episode_probe,research=research,enabled=False,
    budget=episode_audio.EpisodeBudget(),programme=current_programme_context,
    activity=episode_active_view,publisher=episode_publisher,log=log,
    retry=episode_retry_callback,publish=episode_publication_callback)
  return episode_observer
def episode_speech_callback(job,decision,text):
 # Fast callback from the language worker. Runtime opt-in is re-read here so a
 # reviewed configuration revision can enable or disable the lane without a
 # restart. submit() extracts bounded clues and returns; network research,
 # re-probe and publication happen on the observer's own worker.
 if SOURCE_PROFILE!='american':return None
 reload_gateway_config()
 observer=episode_observer_instance()
 if observer is None:return None
 observer.enabled=bool(gateway_config.get('episodeAudioEnabled'))
 return observer.submit(job,decision,text)
def episode_audio_status():
 status=episode_audio.status()
 status['configuredEnabled']=bool(gateway_config.get('episodeAudioEnabled'))
 status['probe']=dict(episode_probe_diagnostics)
 status['deviceProbe']=device_probe_diagnostics()
 status['publication']=episode_publisher.diagnostics()
 status['sourceDiscovery']=episode_source_discovery.diagnostics() if episode_source_discovery is not None else None
 status['sourceProvider']={'opencodeAvailable':opencode_discovery.available(),
                           'runtimeGated':not bool(gateway_config.get('episodeAudioEnabled'))}
 status['sourceProvider']['publicSearchConfigured']=True
 status['handoff']['searchEnabled']=not status['sourceProvider']['runtimeGated']
 if episode_observer is None:
  status['active']=False;status['fault']='not_started';status['observer']=None;return status
 status['enabled']=bool(episode_observer.enabled)
 status['active']=bool(episode_observer.enabled)
 status['fault']=None
 status['budget']=episode_observer.budget.diagnostics() if episode_observer.budget is not None else None
 status['observer']=episode_observer.diagnostics()
 return status
def language_auto_commit(cid,provider,name,language,language_name,confidence,reason,evidence,session=None,respect_pause=True):
 global spanish_state,spanish_provider_ids,spanish_manual_provider_ids,spanish_auto_provider_ids,spanish_fingerprint
 if SOURCE_PROFILE!='american':return {'changed':False,'reason':'wrong_profile'}
 if sources.get(cid)!=provider:return {'changed':False,'reason':'unknown_provider'}
 if not reload_spanish_auto_control():return {'changed':False,'reason':'observation_disabled'}
 payload=evidence if isinstance(evidence,dict) else {}
 outcome=payload.get('outcome','excluded')
 if outcome not in language_exclusions.OUTCOMES:return {'changed':False,'reason':'inconclusive'}
 if payload.get('deviceVerified') is not True:return {'changed':False,'reason':'device_unverified'}
 if payload.get('channelChangedBeforeResult') is True:return {'changed':False,'reason':'stale_channel_changed'}
 window=maintenance_window()
 if window is not None:
  now=time.time()
  if window['start']<=now<=window['until'] and (not window.get('channelID') or window['channelID']==cid):
   return {'changed':False,'reason':'maintenance'}
 if respect_pause and outcome=='excluded':
  rotation=device_rotation_state()
  if rotation is None:
   defer_reason='deferred_uncertain'
  elif rotation.get('surfPaused') is True or rotation.get('surfing') is not True:
   # Defer whenever the user has paused or stopped rotation, even when the
   # device is now on a different channel from the sampled one.  A membership
   # change must not land while playback is under the user's control.
   defer_reason='deferred_paused'
  else:
   defer_reason=None
  if defer_reason:
   identity=session if isinstance(session,dict) else {}
   record={'channelID':cid,'channelName':str(name or provider)[:160],'language':language,
           'languageName':language_name,'confidence':confidence,'reason':reason,
           'evidence':payload,
           'launchID':str(identity.get('launchID') or payload.get('launchID') or ''),
           'streamGeneration':str(identity.get('streamGeneration') or payload.get('streamGeneration') or ''),
           'createdAt':time.time()}
   try:
    language_pending.add(ROOT,provider,record)
    log('LANGUAGE_EXCLUSION_DEFER language='+language+' provider='+provider+' channel='+cid+' reason='+defer_reason)
    return {'changed':False,'reason':defer_reason}
   except (ValueError,OSError,TypeError) as error:
    log('Language pending write rejected '+type(error).__name__)
    return {'changed':False,'reason':'pending_'+type(error).__name__}
 try:
  with lock:
   fresh=spanish_classification.load_or_migrate(ROOT)
   state,info=spanish_classification.record_evidence(
    fresh,provider,cid,name,language,language_name,confidence,reason,payload,
    outcome=outcome,visit_id=payload.get('visitID',''),launch_id=payload.get('launchID',''),
    stream_generation=payload.get('streamGeneration',''),
    threshold=SPANISH_EVIDENCE_THRESHOLD,enforcement=SPANISH_AUTO_ENFORCEMENT)
   if info.get('changed'):spanish_classification.atomic_write(ROOT,state)
   spanish_state=state
   spanish_provider_ids,spanish_manual_provider_ids,spanish_auto_provider_ids=_language_membership(state)
   try:
    stat=SPANISH_FILE.stat();spanish_fingerprint=(stat.st_mtime_ns,stat.st_size,stat.st_ino)
   except OSError:spanish_fingerprint=None
  if info.get('enforced'):log('LANGUAGE_EVIDENCE_ENFORCED language='+language+' provider='+provider+' channel='+cid+' count='+str(info.get('count'))+' threshold='+str(info.get('threshold')))
  elif info.get('changed'):log('LANGUAGE_EVIDENCE language='+language+' provider='+provider+' channel='+cid+' outcome='+outcome+' count='+str(info.get('count'))+' threshold='+str(info.get('threshold')))
  if info.get('changed'):overlay_publish_evidence(cid,name,language,language_name,confidence,payload,info)
  return info
 except (ValueError,OSError,TypeError) as error:
  log('Language auto commit rejected '+type(error).__name__)
  return {'changed':False,'reason':'commit_'+type(error).__name__}
def process_language_pending_once():
 global spanish_state,spanish_provider_ids,spanish_manual_provider_ids,spanish_auto_provider_ids,spanish_fingerprint
 if SOURCE_PROFILE!='american':return 0
 if not reload_spanish_auto_control():return 0
 try:pending=language_pending.load(ROOT)
 except (ValueError,OSError,TypeError):return 0
 if not pending['entries']:return 0
 rotation=device_rotation_state(force=True)
 if rotation is None or rotation.get('surfing') is not True or rotation.get('surfPaused') is True:return 0
 applied=0
 for provider,record in list(pending['entries'].items()):
  cid=record['channelID']
  if sources.get(cid)!=provider:
   language_pending.remove(ROOT,provider);continue
  payload=record['evidence'] if isinstance(record['evidence'],dict) else {}
  outcome=payload.get('outcome','excluded')
  if outcome!='excluded':
   language_pending.remove(ROOT,provider);continue
  visit_id=payload.get('visitID','')
  launch_id=payload.get('launchID') or record.get('launchID') or ''
  stream_generation=payload.get('streamGeneration') or record.get('streamGeneration') or ''
  changed=False;why=None;info={}
  try:
   with lock:
    fresh=spanish_classification.load_or_migrate(ROOT)
    state,info=spanish_classification.record_evidence(
     fresh,provider,cid,record['channelName'],record['language'],record['languageName'],
     record['confidence'],record['reason'],payload,outcome=outcome,
     visit_id=visit_id,launch_id=launch_id,stream_generation=stream_generation,
     threshold=SPANISH_EVIDENCE_THRESHOLD,enforcement=SPANISH_AUTO_ENFORCEMENT)
    changed=bool(info.get('changed'))
    why=info.get('reason')
    if changed:spanish_classification.atomic_write(ROOT,state)
    spanish_state=state
    spanish_provider_ids,spanish_manual_provider_ids,spanish_auto_provider_ids=_language_membership(state)
    try:
     stat=SPANISH_FILE.stat();spanish_fingerprint=(stat.st_mtime_ns,stat.st_size,stat.st_ino)
    except OSError:spanish_fingerprint=None
  except (ValueError,OSError,TypeError) as error:
   log('Language pending apply rejected '+type(error).__name__);continue
  if changed:
   applied+=1
   if info.get('enforced'):log('LANGUAGE_EVIDENCE_ENFORCED language='+record['language']+' provider='+provider+' channel='+cid+' count='+str(info.get('count'))+' threshold='+str(info.get('threshold')))
   else:log('LANGUAGE_EXCLUSION_PENDING_APPLIED language='+record['language']+' provider='+provider+' channel='+cid+' count='+str(info.get('count')))
   overlay_publish_evidence(cid,record['channelName'],record['language'],record['languageName'],record['confidence'],payload,info)
  if changed or why in ('manual_not_spanish','manual_exclusion','already_marked','unknown_provider','duplicate_visit'):
   language_pending.remove(ROOT,provider)
 return applied
def language_pending_worker(stop):
 # Existing daemon lifecycle only; it never issues a device command.
 while not stop.wait(5):
  try:process_language_pending_once()
  except Exception as error:log('Language pending worker error '+type(error).__name__)
def spanish_auto_commit(cid,provider,name,confidence,reason,evidence):
 # Backward-compatible manual-era entry point; automatic manager uses language_auto_commit.
 return language_auto_commit(cid,provider,name,'es','Spanish',confidence,reason,evidence,respect_pause=False)
def schedule_episode_status():
 try:
  base=schedule_episode.status()
  snap=catalog_snapshot()
  return dict(base,catalogChannels=len(snap),
              catalogEpisodes=sum(len(rows) for rows in snap.values()),
              catalogObservedAt=catalog_observed_at,catalogFault=catalog_fault,
              catalogLastAttemptAt=catalog_last_attempt,
              catalogRefreshSeconds=CATALOG_REFRESH,catalogMaxAgeSeconds=CATALOG_MAX_AGE)
 except Exception:return {'revision':schedule_episode.REVISION,'fault':'status_unavailable'}
def overlay_bridge_instance():
 global overlay_bridge_state,overlay_bridge_fingerprint,overlay_bridge_fault
 if SOURCE_PROFILE!='american':return None
 try:config=prototype_selector.load_selector(ROOT)
 except (OSError,ValueError,TypeError) as error:
  overlay_bridge_fault='selector_'+type(error).__name__;return None
 if config is None:
  overlay_bridge_fault='';return None
 fingerprint=(config['deviceIP'],config['backendPort'])
 with overlay_init_lock:
  if overlay_bridge_state is None or fingerprint!=overlay_bridge_fingerprint:
   transport=overlay_bridge.AuthorizedDeviceTransport(config,os.environ.get('JEALOUSFIN_DEVICE_TOKEN',''))
   overlay_bridge_state=overlay_bridge.OverlayBridge(transport)
   overlay_bridge_fingerprint=fingerprint
  return overlay_bridge_state
def programme_overlay_instance():
 # Existing installed IPA fields only. This creates no native style, no layout
 # install and no extra device command beyond the authorized ui_set_value.
 global programme_overlay_bridge_state,programme_overlay_bridge_fingerprint
 if SOURCE_PROFILE!='american':return None
 try:config=prototype_selector.load_selector(ROOT)
 except (OSError,ValueError,TypeError):return None
 if config is None:return None
 fingerprint=(config['deviceIP'],config['backendPort'])
 with overlay_init_lock:
  if programme_overlay_bridge_state is None or fingerprint!=programme_overlay_bridge_fingerprint:
   transport=overlay_bridge.AuthorizedDeviceTransport(config,os.environ.get('JEALOUSFIN_DEVICE_TOKEN',''))
   programme_overlay_bridge_state=programme_overlay.ProgrammeOverlayPublisher(transport)
   programme_overlay_bridge_fingerprint=fingerprint
  return programme_overlay_bridge_state
def _overlay_visit_for(cid,launch,stream):
 if not isinstance(cid,str) or not cid:return ''
 if not isinstance(launch,str) or not launch:return ''
 if not isinstance(stream,str) or not stream:return ''
 try:visit=language_visit_identity().observe(cid,launch,stream,now=time.time())
 except Exception:return ''
 if not isinstance(visit,dict) or visit.get('verified') is not True:return ''
 value=visit.get('visitID')
 return value if isinstance(value,str) else ''
def overlay_combined_scope(cid,name,probe=None):
 provider=sources.get(cid)
 if not isinstance(provider,str) or not provider.startswith('nltv:'):return None
 probe=probe if isinstance(probe,dict) else {}
 launch=str(probe.get('launchID') or '')
 stream=str(probe.get('streamGeneration') or '')
 visit=str(probe.get('visitID') or '') or _overlay_visit_for(cid,launch,stream)
 try:
  return programme_overlay.OverlayScope(cid,provider,name[:160],launch[:160],visit[:160],stream[:160])
 except (TypeError,ValueError):return None
def _overlay_current_scope(scope):
 # Presentation rechecks are never served from the short probe cache: a stale
 # visiting identity must not be able to switch the combined node on.
 current=overlay_current_channel(fresh=True)
 if not isinstance(current,dict):return None
 visit=_overlay_visit_for(current.get('channelID'),current.get('launchID'),
                          current.get('streamGeneration'))
 return {'channelID':current.get('channelID'),'launchID':current.get('launchID'),
         'streamGeneration':current.get('streamGeneration'),'visitID':visit}
def overlay_combined_publish(cid,name=None,probe=None,evidence=None,now=None):
 result=_overlay_combined_publish(cid,name,probe,evidence,now)
 stats=overlay_combined_stats
 stats['attempts']+=1
 stats['lastResult']=result
 stats['lastReason']=programme_overlay_fault or ''
 if result=='published':
  stats['published']+=1;stats['accepted']+=1
 elif result in ('duplicate','unchanged'):
  stats['accepted']+=1
 elif result=='fallback':stats['fallback']+=1
 elif result=='unavailable':stats['unavailable']+=1
 elif result=='unverified':stats['unverified']+=1
 elif result=='suppressed_channel':stats['suppressed']+=1
 elif result=='no_programme':stats['noProgramme']+=1
 elif result in ('invalid','invalid_scope'):stats['invalid']+=1
 return result
def _overlay_combined_publish(cid,name=None,probe=None,evidence=None,now=None):
 # Presentation only. It never rewrites Name/ProductionYear/JealousfinProgramme
 # and is allowed while surf rotation is paused as long as the exact scope is
 # still current and the caller has validated actual playback.
 global programme_overlay_fault
 if SOURCE_PROFILE!='american':return 'disabled'
 bridge=programme_overlay_instance()
 if bridge is None:
  programme_overlay_fault='authorized_transport_unavailable';return 'unavailable'
 probe=probe if isinstance(probe,dict) else None
 if probe is None:
  probe=overlay_current_channel()
  if not isinstance(probe,dict):
   programme_overlay_fault='device_unverified';return 'unverified'
 if probe.get('channelID')!=cid:
  programme_overlay_fault='channel_changed';return 'suppressed_channel'
 if not isinstance(name,str) or not name:
  row=channels.get(cid) or {};name=row.get('Name') if isinstance(row,dict) else ''
 if not isinstance(name,str) or not name:return 'invalid'
 scope=overlay_combined_scope(cid,name,probe)
 if scope is None:
  programme_overlay_fault='invalid_scope';return 'invalid_scope'
 try:row=programme_snapshot().get(cid)
 except (sqlite3.Error,OSError,RuntimeError,ValueError):row=None
 if not isinstance(row,dict):
  result=bridge.clear(scope=scope)
  programme_overlay_fault='' if result=='fallback' else result
  return 'no_programme'
 identity=programme_overlay.programme_identity(row)
 if identity is None:
  result=bridge.clear(scope=scope)
  programme_overlay_fault='' if result=='fallback' else result
  return 'no_programme'
 if not isinstance(evidence,dict):
  cached=overlay_combined_evidence
  if (isinstance(cached,dict) and cached.get('channelID')==cid
      and (not cached.get('launchID') or cached.get('launchID')==scope.launch_id)
      and (not cached.get('visitID') or cached.get('visitID')==scope.visit_id)
      and (not cached.get('streamGeneration') or cached.get('streamGeneration')==scope.stream_generation)):
   evidence=cached.get('evidence')
 language=programme_overlay.language_evidence(evidence) if isinstance(evidence,dict) else None
 stamp=time.time() if now is None else float(now)
 if language is not None and not language.checked(stamp):
  # Expired evidence is not stronger than the installed live-metadata
  # fallback, and must never switch the native node on.
  language=None
 has_episode=bool(identity.episode_title or identity.season_number is not None)
 if not has_episode and language is None:
  # The installed 22 px fallback already shows live programme metadata. Do
  # not switch to the native combined node until there is something stronger
  # than a bare series title, and never leave an old combined label visible.
  result=bridge.clear(scope=scope)
  programme_overlay_fault='' if result=='fallback' else result
  return 'fallback'
 text=programme_overlay.format_combined(identity,language,now=stamp)
 event_id='|'.join((scope.fingerprint(),identity.original_air_date or identity.text(),
                    language.event_id if language is not None else 'pending'))
 result=bridge.publish(scope,text,event_id,reprobe=lambda:_overlay_current_scope(scope))
 programme_overlay_fault='' if result in ('published','duplicate','unchanged') else result
 return result
def overlay_combined_status():
 bridge=programme_overlay_instance()
 base={'configured':bridge is not None,'fault':programme_overlay_fault,
       'stats':dict(overlay_combined_stats),
       'revision':programme_overlay.REVISION,
       'field':programme_overlay.KEY,'readyField':programme_overlay.READY_KEY,
       'fallbackField':programme_overlay.FALLBACK_KEY,
       'layoutRevision':'programme-language-existing-ipa-20261127',
       'nativeAbi':'existing headline text node programme-combined-native-v1'}
 if bridge is None:return base
 return dict(base,**bridge.status())
def overlay_enqueue(kind,**fields):
 global overlay_bridge_fault
 if SOURCE_PROFILE!='american':return False
 job=dict(fields,kind=kind,at=time.time())
 if kind=='refresh':
  with overlay_pump_lock:overlay_admission_stats['refreshRequests']+=1
 try:overlay_jobs.put_nowait(job)
 except queue.Full:
  # Keep only the newest lifecycle event; the bridge is a display, never a
  # durable queue, and a stale label must not block a current one.
  evicted=None
  try:evicted=overlay_jobs.get_nowait()
  except queue.Empty:pass
  # A queued pump that never runs must not strand the coalescing boolean.
  # Retire only the evicted pump's own token so a replacement pump that is
  # already queued keeps its pending ownership.
  if isinstance(evicted,dict) and evicted.get('kind')=='pump':
   _overlay_pump_retire(evicted.get('pumpToken'))
  try:overlay_jobs.put_nowait(job)
  except queue.Full:
   overlay_bridge_fault='queue_full';return False
 if kind=='refresh':
  with overlay_pump_lock:overlay_admission_stats['refreshEnqueued']+=1
 return True
def overlay_publish_evidence(cid,name,language,language_name,confidence,payload,info):
 event_id=info.get('eventID')
 if not isinstance(event_id,str) or not event_id:return False
 evidence={'eventID':event_id,'at':time.time(),'language':language,'languageName':language_name,
           'count':info.get('count'),'threshold':info.get('threshold'),
           'phase':str(info.get('outcome') or 'recorded'),
           'confidence':confidence if type(confidence) in (int,float) else None}
 return overlay_enqueue('evidence',channelID=cid,channelName=name,
   streamGeneration=str((payload or {}).get('streamGeneration','')),
   launchID=str((payload or {}).get('launchID','') or ''),
   visitID=str((payload or {}).get('visitID','') or ''),evidence=evidence)
def overlay_publish_state(payload):
 if not isinstance(payload,dict):return False
 cid=payload.get('channelID');name=payload.get('channelName')
 if not isinstance(cid,str) or not cid:return False
 if not isinstance(name,str) or not name:
  row=channels.get(cid) or {};name=row.get('Name') if isinstance(row,dict) else ''
 if not isinstance(name,str) or not name:return False
 state=dict(payload)
 state['eventID']='state-'+cid+'-'+str(int(float(payload.get('observedAt') or time.time())*1000))+'-'+str(payload.get('state') or 'waiting')
 state['at']=payload.get('observedAt') or time.time()
 return overlay_enqueue('state',channelID=cid,channelName=name,
   streamGeneration=str(payload.get('streamGeneration') or ''),
   launchID=str(payload.get('launchID') or ''),
   visitID=str(payload.get('visitID') or ''),state=state)
def language_state_callback(payload):
 if SOURCE_PROFILE!='american':return False
 return overlay_publish_state(payload)
def overlay_publish_correction(cid,name,event_id,count,threshold):
 if not isinstance(event_id,str) or not event_id:return False
 evidence={'eventID':event_id+':corrected','at':time.time(),'language':'en','languageName':'English',
           'count':count,'threshold':threshold,'phase':'corrected','confidence':None}
 return overlay_enqueue('correction',channelID=cid,channelName=name,eventID=event_id,evidence=evidence)
def overlay_correction_allowed(cid):
 bridge=overlay_bridge_state
 if bridge is None:return True
 if bridge.current_channel_id and bridge.current_channel_id!=cid:return False
 if bridge.latest_snapshot is not None and bridge.latest_snapshot.channel_id!=cid:return False
 return True
def overlay_current_channel(fresh=False):
 try:probe=device_language_probe(share=not fresh,origin='overlay_fresh' if fresh else 'overlay')
 except Exception:return None
 if not isinstance(probe,dict):return None
 cid=probe.get('channelID')
 if not isinstance(cid,str) or cid not in channels:return None
 name=channels[cid].get('Name')
 if not isinstance(name,str) or not name:return None
 return {'channelID':cid,'channelName':name,'launchID':str(probe.get('launchID') or ''),
         'streamGeneration':str(probe.get('streamGeneration') or '')}
def _overlay_visit_matches(visit_id,current):
 # Only a durable verified visit can be compared. Unverified legacy/test
 # identities keep the existing channel/stream gate and never invent a visit.
 if not language_visits.is_verified(visit_id):return True
 if not isinstance(current,dict):return False
 launch=current.get('launchID')
 if not isinstance(launch,str) or not launch:return False
 try:
  observed=language_visit_identity().observe(current.get('channelID'),launch,
   current.get('streamGeneration'),now=time.time())
 except Exception:return False
 return bool(isinstance(observed,dict) and observed.get('verified') is True
             and observed.get('visitID')==visit_id)
def _overlay_settle(bridge,stop):
 # Retries stay inside one lifecycle job and are strictly time-bounded; there
 # is no periodic timer or extra schedule.
 deadline=time.monotonic()+OVERLAY_SETTLE_SECONDS
 while not stop.is_set():
  status=bridge.status().get('status')
  if status not in ('retry','deferred','rate_limited'):break
  if time.monotonic()>=deadline:break
  if stop.wait(0.25 if status=='retry' else 0.5):break
  bridge.pump()
def _overlay_process(job,stop):
 global overlay_bridge_fault,programme_overlay_fault,overlay_combined_evidence
 bridge=overlay_bridge_instance()
 if bridge is None:
  overlay_bridge_fault=overlay_bridge_fault or 'authorized_transport_unavailable'
  return 'unavailable'
 kind=job.get('kind')
 if kind=='pump':
  current=overlay_current_channel()
  if current is None:
   overlay_bridge_fault='device_unverified';return 'unverified'
  if bridge.current_channel_id and current['channelID']!=bridge.current_channel_id:
   overlay_bridge_fault='channel_changed';bridge.observe_channel(current['channelID'],current['channelName'])
   return 'suppressed_channel'
  bridge.pump();_overlay_settle(bridge,stop);return bridge.status().get('status','idle')
 current=overlay_current_channel()
 if current is None:
  overlay_bridge_fault='device_unverified';return 'unverified'
 cid=current['channelID'];name=current['channelName']
 if kind=='refresh':
  result=bridge.observe_channel(cid,name)
  # The existing catalogue/playback boundary is the only trigger here; this
  # does not add a timer or a device poll. A disabled detector with no current
  # evidence is reported truthfully instead of an unbounded waiting label.
  if spanish_auto_manager is not None and bridge.latest_snapshot is None:
   try:enabled=bool(spanish_auto_manager.status().get('enabled'))
   except Exception:enabled=False
   if not enabled:
    bridge.publish_state(cid,name,{'state':'disabled','reason':'observation_disabled',
     'at':time.time(),'observedAt':time.time()})
    result=bridge.status().get('status',result) or result
 elif kind=='combined':
  result=overlay_combined_publish(cid,name,probe=current)
 elif kind in ('evidence','correction','state'):
  if job.get('channelID')!=cid:
   overlay_bridge_fault='channel_changed';bridge.observe_channel(cid,name)
   return 'suppressed_channel'
  evidence=job.get('state') if kind=='state' else job.get('evidence')
  if not isinstance(evidence,dict):return 'invalid'
  stream=job.get('streamGeneration') or '';probe_stream=current['streamGeneration']
  if stream and probe_stream and stream!=probe_stream:
   overlay_bridge_fault='stale_stream_generation';return 'stale'
  job_launch=str(job.get('launchID') or '')
  if job_launch and job_launch!=str(current.get('launchID') or ''):
   overlay_bridge_fault='stale_launch';return 'stale'
  visit_id=job.get('visitID') or evidence.get('visitID') or ''
  if not _overlay_visit_matches(visit_id,current):
   overlay_bridge_fault='stale_visit';return 'stale'
  bridge.observe_channel(cid,name)
  result=bridge.publish_state(cid,name,evidence) if kind=='state' else bridge.publish_evidence(cid,name,evidence)
  if kind in ('evidence','correction') and result not in ('suppressed_channel','stale','invalid'):
   # Remember only the last accepted current-scope evidence so sampler state
   # and refresh events keep the combined label coherent without inventing a
   # new language result. The stream/visit match is checked before reuse.
   overlay_combined_evidence={'channelID':cid,
    'launchID':str(job.get('launchID') or evidence.get('launchID') or ''),
    'visitID':str(job.get('visitID') or evidence.get('visitID') or ''),
    'streamGeneration':str(job.get('streamGeneration') or evidence.get('streamGeneration') or ''),
    'evidence':dict(evidence)}
  if kind=='correction' and job.get('eventID'):
   # Retain the correction against both the original event and the corrected
   # snapshot so a delayed duplicate gesture cannot walk backwards.
   bridge.confirm_correction(job['eventID'])
   bridge.confirm_correction()
 else:
  return 'unknown'
 overlay_bridge_fault=''
 # Settle the existing language label first so a slow/unavailable combined
 # transport can never delay it into stale_pending. The combined label is
 # presentation only and is generated from the same scoped evidence.
 _overlay_settle(bridge,stop)
 result=bridge.status().get('status',result) or result
 if kind in ('refresh','evidence','correction','state'):
  combined_evidence=job.get('evidence') if kind in ('evidence','correction') else None
  try:overlay_combined_publish(cid,name,probe=current,evidence=combined_evidence)
  except Exception:programme_overlay_fault='combined_failed'
 return result
def overlay_worker(stop):
 # Existing daemon lifecycle only: it blocks for a triggered job and stops on
 # the shutdown sentinel. It never polls the device on its own.
 global overlay_bridge_fault
 while not stop.is_set():
  try:job=overlay_jobs.get()
  except (queue.Empty,EOFError):break
  if job is None or stop.is_set():break
  try:_overlay_process(job,stop)
  except Exception as error:
   overlay_bridge_fault=type(error).__name__
   log('Language overlay worker '+type(error).__name__)
  finally:
   if isinstance(job,dict) and job.get('kind')=='pump':
    _overlay_pump_retire(job.get('pumpToken'))
def overlay_request_pump():
 # /trial-status is a poll boundary. Coalesce pending retry jobs so a fast
 # status consumer cannot enqueue one device probe per request.
 global overlay_pump_pending,overlay_pump_queued_at,overlay_pump_last_request
 global overlay_pump_token
 bridge=overlay_bridge_state
 if bridge is None:return False
 try:pending=bool(bridge.status().get('pending'))
 except Exception:return False
 if not pending:return False
 with overlay_pump_lock:
  now=_overlay_admission_now()
  overlay_admission_stats['pumpRequests']+=1
  if overlay_pump_pending and now-overlay_pump_queued_at<=OVERLAY_PUMP_QUEUE_TTL:
   overlay_admission_stats['pumpCoalesced']+=1;return False
  if overlay_pump_pending:
   overlay_admission_stats['pumpStale']+=1
  if now-overlay_pump_last_request<OVERLAY_PUMP_MIN_INTERVAL:
   overlay_admission_stats['pumpCoalesced']+=1;return False
  overlay_pump_last_request=now
  overlay_pump_token+=1
  token=overlay_pump_token
  overlay_pump_tokens.add(token)
  overlay_pump_pending=True
  overlay_pump_queued_at=now
 if not overlay_enqueue('pump',pumpToken=token):
  _overlay_pump_retire(token)
  return False
 with overlay_pump_lock:overlay_admission_stats['pumpEnqueued']+=1
 return True
def overlay_status():
 bridge=overlay_bridge_instance()
 fault=overlay_bridge_fault or (bridge.status().get('fault','') if bridge is not None else '')
 base={'configured':bridge is not None,'fault':fault,'queueDepth':overlay_jobs.qsize(),
       'admission':dict(overlay_admission_stats)}
 if bridge is None:return dict(base,revision=overlay_bridge.OVERLAY_REVISION)
 return dict(bridge.status(),**base)
def spanish_auto_status():
 try:description=spanish_classification.describe(spanish_state)
 except (ValueError,TypeError):description={'entries':len(spanish_provider_ids)}
 control={'enabled':bool(reload_spanish_auto_control()),'enforcementEnabled':bool(SPANISH_AUTO_ENFORCEMENT),'threshold':int(SPANISH_EVIDENCE_THRESHOLD),'revision':SPANISH_AUTO_REVISION}
 if SOURCE_PROFILE!='american' or spanish_auto_manager is None:
  return {'revision':spanish_audio.REVISION,'enabled':False,'active':False,'fault':'not_started','classification':description,'control':control}
 try:status=spanish_auto_manager.status()
 except Exception as error:
  return {'revision':spanish_audio.REVISION,'enabled':False,'active':False,'fault':type(error).__name__,'classification':description,'control':control}
 status['classification']=description;status['control']=control
 return status
def spanish_auto_admin():
 control={'enabled':bool(reload_spanish_auto_control(force=True)),'enforcementEnabled':bool(SPANISH_AUTO_ENFORCEMENT),'threshold':int(SPANISH_EVIDENCE_THRESHOLD),'revision':SPANISH_AUTO_REVISION,'file':SPANISH_AUTO_CONTROL_FILE.name}
 return {'control':control,'status':spanish_auto_status()}
language_excluded=spanish_excluded
maybe_reload_languages=maybe_reload_spanish
apply_device_language_request=apply_device_spanish_request
language_auto_status=spanish_auto_status
language_auto_admin=spanish_auto_admin
def ordered_catalogue(snapshot,include_excluded=False):
 now=time.time()
 return sorted((item for item in channels.items() if language_visible(item[0]) and (include_excluded or policy_eligible(snapshot[item[0]],now))),key=lambda item:(snapshot[item[0]]["lastAttempt"],item[0]))
def policy_payload(snapshot):
 with closing(sqlite3.connect((ROOT/'policy.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
  days,ban=policy_penalty(db)
  row=db.execute("SELECT value FROM meta WHERE key='surf_resume'").fetchone()
  resume=json.loads(row[0]) if row else None
  visible={cid:row for cid,row in snapshot.items() if language_visible(cid)}
  if not isinstance(resume,dict) or resume.get('channelID') not in visible:resume=None
 return {"revision":"sqlite-policy-v2","channels":visible,"penalty":{"baseDays":days,"banAfter":ban},"resume":resume}
def policy_event(cid,trace,action,reason):
 with closing(policy_connect(ROOT/'policy.sqlite3')) as db, db:
  if not db.execute("SELECT 1 FROM channel WHERE id=?",(cid,)).fetchone():raise ValueError("Unknown policy channel")
  now=time.time()
  event_id="native-policy:"+trace+":"+action
  prior=db.execute("SELECT cid FROM observed WHERE id=?",(event_id,)).fetchone()
  if prior:
   if prior[0]!=cid:raise ValueError("Policy event identity mismatch")
   return
  if action=="failure" and maintenance_ignores(reason,now) is not None:
   # A declared, time-bounded maintenance window may suppress only the
   # infrastructure reasons listed in the window. Record the original event
   # identity so a later replay cannot be penalised; never clear existing
   # penalties and never suppress channel-specific failures outside the window.
   db.execute("INSERT INTO observed VALUES(?,?,?,?,?,?)",(event_id,now,"NATIVE_POLICY_MAINTENANCE_IGNORED",cid,trace,reason[:300]))
   db.execute("INSERT OR IGNORE INTO sessions(trace,cid,opened) VALUES(?,?,?)",(trace,cid,now))
   record_maintenance_ignored(cid,trace,reason,now)
   log('MAINTENANCE_IGNORED failure channel='+cid)
   return
  if action=="failure":
   deployment=deployment_continuity.failure_ignores(ROOT,cid,reason,now)
   if deployment is not None:
    # A gateway restart invalidated the handles used by the live visit. Only a
    # fixed after-startup reason, the same channel's repeated stale-handle 404s
    # and the short post-restart window qualify; the original event identity is
    # preserved and no health counter is changed.
    db.execute("INSERT INTO observed VALUES(?,?,?,?,?,?)",(event_id,now,"NATIVE_POLICY_DEPLOYMENT_IGNORED",cid,trace,reason[:300]))
    db.execute("INSERT OR IGNORE INTO sessions(trace,cid,opened) VALUES(?,?,?)",(trace,cid,now))
    deployment_continuity.record_ignored(ROOT,'gateway','NATIVE_POLICY_FAILURE',trace,reason,deployment)
    log('DEPLOYMENT_IGNORED failure channel='+cid+' stale='+str(deployment['staleHitCount']))
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
 entry={'at':time.time(),'channel_id':cid,'host':host,'phase':phase,'error':error,'request_id':correlation,'category':('gateway_infrastructure' if phase in ('policy','selector','compatibility') else 'upstream_http' if re.fullmatch(r'HTTP [1-5][0-9]{2}',error) else 'upstream_transport' if error in ('URLError','TimeoutError','ConnectionError','ConnectionResetError','RemoteDisconnected') else 'resolution_or_delivery_unknown')}
 with lock:
  p=ROOT/'source-errors.jsonl'
  if p.exists() and p.stat().st_size>4*1024*1024:
   os.replace(p,ROOT/'source-errors.previous.jsonl')
  with p.open('a') as f:f.write(json.dumps(entry,separators=(',',':'))+'\n')


def record_infrastructure_error(cid,phase,error,correlation=None):
 # Diagnostics must never prevent a fail-closed response.
 try:record_source_error(cid,None,phase,type(error).__name__,correlation)
 except OSError:log('infrastructure diagnostic write failed')

class Server(http.server.ThreadingHTTPServer):
 daemon_threads=True
 allow_reuse_address=True
# The web application executes on the user's LAN browser. Only its exact
# production origin is permitted; native clients without Origin are unchanged.
WEB_ORIGIN='https://jealouswin.pages.dev'
# Serve only the known Cloudflare build assets, never forward user credentials,
# API calls, cookies, query strings, or arbitrary remote destinations.
WEB_ASSET_HOST='jealouswin.pages.dev'
WEB_ROOT_ASSETS=frozenset(('/', '/index.html', '/main.dart.js', '/flutter.js',
 '/flutter_bootstrap.js', '/flutter_service_worker.js', '/version.json',
 '/manifest.json', '/favicon.png', '/player.js', '/hls.min.js'))
def web_asset(path):
 if path in WEB_ROOT_ASSETS:return True
 return bool(re.fullmatch(r'/(?:assets|canvaskit|icons)/(?:[A-Za-z0-9_@.+%~-]+/)*[A-Za-z0-9_@.+%~-]+',path)) and all(
  urllib.parse.unquote(part) not in ('.','..') and '/' not in urllib.parse.unquote(part) and '\\' not in urllib.parse.unquote(part) for part in path.split('/'))
def local_web_origin():
 # Use the configured bind address, never the untrusted HTTP Host header.
 try:address=ipaddress.ip_address(HOST)
 except ValueError:return None
 if address.version!=4 or not address.is_private or address.is_unspecified:return None
 return 'http://'+str(address)+':18796'
def audit_route(path):
 # Never log URL queries, media tokens, identifiers or arbitrary user input.
 if path=='/Users/AuthenticateByName':return path
 if path.startswith(('/media/','/malay/media/')):return '/media/[redacted]'
 if re.fullmatch(r'/Items/[^/]+/PlaybackInfo',path):return '/Items/[id]/PlaybackInfo'
 if re.fullmatch(r'/Users/[^/]+/FavoriteItems/[^/]+',path):return '/Users/[id]/FavoriteItems/[id]'
 if path in ('/System/Info/Public','/LiveTv/Channels','/LiveTv/Programs','/LiveTv/Recordings','/Jealousfin/Policy','/Jealousfin/Policy/Event','/Sessions/Playing','/Sessions/Playing/Progress','/Sessions/Playing/Stopped','/LiveStreams/Close','/trial-status'):return path
 return '/web-asset' if web_asset(path) else '/other'
WEB_HEADERS=frozenset(('accept','authorization','content-type','range','x-emby-token','x-emby-authorization','x-jealousfin-request-id'))
def web_route(path,method):
 if method in ('GET','HEAD'):
  return path in ('/System/Info/Public','/LiveTv/Channels','/LiveTv/Programs','/Jealousfin/Policy','/LiveTv/Recordings') or bool(re.fullmatch(r'/(?:malay/)?media/[^/]+/[^/]+',path))
 if method=='POST':
  return path in ('/Users/AuthenticateByName','/Sessions/Playing','/Sessions/Playing/Progress','/Sessions/Playing/Stopped','/LiveStreams/Close') or bool(re.fullmatch(r'/Items/[a-f0-9]{32}/PlaybackInfo',path)) or bool(re.fullmatch(r'/Users/[^/]+/FavoriteItems/[a-f0-9]{32}',path))
 if method=='DELETE':
  return bool(re.fullmatch(r'/Users/[^/]+/FavoriteItems/[a-f0-9]{32}',path))
 return False

class _PlaybackAdmissionFlight:
 # One foreground owner plus the exact PlaybackInfo waiters that joined it.
 # The flight is removed on completion, so a category is never cached beyond
 # the in-flight recovery it belongs to.
 __slots__=('event','result','error','waiters','key')
 def __init__(self,key):
  self.event=threading.Event();self.result=None;self.error=None
  self.waiters=0;self.key=key
def _playback_credential_scope(token):
 # The flight key must distinguish credentials without storing or logging the
 # secret itself.  A digest is sufficient for coalescing and is never returned
 # to a client.
 raw=token if isinstance(token,str) else ''
 return hashlib.sha256(('jealousfin-playback:'+raw).encode('utf-8')).hexdigest()
def _playback_flight_key(config,token):
 return (_device_probe_key(config),_playback_credential_scope(token))
def _playback_selector_reserve_seconds():
 # A foreground category read is either the bounded read itself or a wait for
 # an in-flight status observation, so reserve the larger of the two.
 try:wait=float(prototype_selector.STATUS_WAIT_SECONDS)
 except (AttributeError,TypeError,ValueError):wait=0.0
 try:read=float(prototype_selector.STATUS_READ_TIMEOUT_SECONDS)
 except (AttributeError,TypeError,ValueError):read=wait
 return max(wait,read)
def _playback_resolution_reserve_seconds(cid):
 source_id=sources.get(cid) if isinstance(cid,str) else None
 if not isinstance(source_id,str):return float(gateway_config['providerTimeoutSeconds'])
 try:count=max(1,len(resolve_bases(source_id,gateway_config)))
 except Exception:count=1
 return float(gateway_config['providerTimeoutSeconds'])*count
def _playback_admission_plan(config,cid,now=None,started=None):
 # Wait only as long as the existing status backoff, and only when a fresh
 # category read plus the worst-case upstream resolution still fit inside the
 # native PlaybackInfo deadline.  Returns (wait, monotonic deadline) or None.
 now=_device_probe_now() if now is None else now
 with DEVICE_PROBE_LOCK:
  remaining=_device_probe_status_remaining_locked(config,now)
 try:
  remaining=max(remaining,prototype_selector.status_observation_remaining(config))
 except Exception:pass
 started=now if started is None else started
 available=(PLAYBACK_ADMISSION_BUDGET_SECONDS-max(0.0,now-started)-
  _playback_resolution_reserve_seconds(cid)-
  _playback_selector_reserve_seconds()-PLAYBACK_ADMISSION_MARGIN_SECONDS)
 if available<=0 or remaining>available:return None
 return remaining,now+available
def _playback_admission_wait(wait,deadline):
 # Bounded by a real monotonic deadline; never sleeps past it.
 while wait>0:
  now=_device_probe_now()
  if now>=deadline:return False
  step=min(wait,deadline-now)
  if step<=0:return False
  PLAYBACK_ADMISSION_WAIT(step)
  wait-=step
 return _device_probe_now()<deadline
def _playback_profile_recovery(config,token,cid,started=None):
 key=_playback_flight_key(config,token)
 plan=_playback_admission_plan(config,cid,started=started)
 if plan is None:
  with PLAYBACK_ADMISSION_LOCK:PLAYBACK_ADMISSION_STATS['refused']+=1
  raise RuntimeError('Device probe backoff')
 wait,deadline=plan
 with PLAYBACK_ADMISSION_LOCK:
  flight=PLAYBACK_ADMISSION_FLIGHTS.get(key)
  owner=flight is None
  if owner:
   flight=_PlaybackAdmissionFlight(key)
   PLAYBACK_ADMISSION_FLIGHTS[key]=flight
   PLAYBACK_ADMISSION_STATS['started']+=1
  else:flight.waiters+=1
 if not owner:
  # Never hold the admission lock while waiting; each waiter shares the same
  # monotonic deadline and the single in-flight category read.
  timeout=max(0.0,deadline+_playback_selector_reserve_seconds()-_device_probe_now())
  if not flight.event.wait(timeout):
   with PLAYBACK_ADMISSION_LOCK:PLAYBACK_ADMISSION_STATS['timeouts']+=1
   raise RuntimeError('Device probe backoff')
  with PLAYBACK_ADMISSION_LOCK:PLAYBACK_ADMISSION_STATS['shared']+=1
  if flight.error is not None:raise flight.error
  return _device_probe_copy(flight.result)
 timed_out=False
 try:
  if wait>0 and not _playback_admission_wait(wait,deadline):timed_out=True
  elif _device_probe_now()>=deadline:timed_out=True
  if timed_out:raise RuntimeError('Device probe backoff')
  # Exactly one fresh authenticated category read after the existing backoff.
  # The normal admission and profile checks are unchanged; nothing is cached.
  result=selected_profile_admitted(config,token,_recovery=True)
  if _device_probe_now()>=deadline+_playback_selector_reserve_seconds():
   timed_out=True;raise RuntimeError('Playback admission deadline exceeded')
 except BaseException as error:
  with PLAYBACK_ADMISSION_LOCK:
   if PLAYBACK_ADMISSION_FLIGHTS.get(key) is flight:
    PLAYBACK_ADMISSION_FLIGHTS.pop(key,None)
   if timed_out:PLAYBACK_ADMISSION_STATS['timeouts']+=1
   else:PLAYBACK_ADMISSION_STATS['failures']+=1
   flight.error=error;flight.event.set()
  raise
 with PLAYBACK_ADMISSION_LOCK:
  if PLAYBACK_ADMISSION_FLIGHTS.get(key) is flight:
   PLAYBACK_ADMISSION_FLIGHTS.pop(key,None)
  PLAYBACK_ADMISSION_STATS['recovered']+=1
  flight.result=result;flight.event.set()
 return _device_probe_copy(result)
def playback_admission_foreground(path,cid,malay):
 # Recovery is restricted to English PlaybackInfo for a known catalogue
 # channel.  The Malay proxy keeps its immediate fail-closed admission.
 return bool(path.endswith('/PlaybackInfo') and not malay and cid in sources)
def selected_profile_admitted(config,token,foreground=False,cid=None,_recovery=False,reconnect=False):
 started=_device_probe_now()
 key=_device_probe_key(config)
 # Foreground PlaybackInfo plus the authenticated catalogue/Policy reconnect
 # boundaries may wait out a shared status backoff under the existing monotonic
 # budget.  Other background consumers stay immediately fail-closed.
 recover=foreground or reconnect
 if recover:
  with PLAYBACK_ADMISSION_LOCK:
   reserved=_playback_flight_key(config,token) in PLAYBACK_ADMISSION_FLIGHTS
  if reserved or prototype_selector.status_observation_remaining(config)>0:
   return _playback_profile_recovery(config,token,cid,started)
 with DEVICE_PROBE_LOCK:
  admitted=_device_probe_admit_locked(config,'playback-recovery' if _recovery else 'profile',True,('status',))
  fault=_device_probe_local_fault()
 if not admitted:
  # Status-only consumers must not be blocked by a snapshot/identity backoff.
  # Background callers keep the immediate fail-closed backoff response; only a
  # foreground PlaybackInfo or catalogue/Policy reconnect may wait it out.
  if recover and fault in ('device_probe_backoff','device_probe_rate_limited','device_probe_playback_reserved'):
   if fault=='device_probe_playback_reserved':
    with PLAYBACK_ADMISSION_LOCK:
     same_scope=_playback_flight_key(config,token) in PLAYBACK_ADMISSION_FLIGHTS
    if not same_scope:raise RuntimeError('Device probe backoff')
   return _playback_profile_recovery(config,token,cid,started)
  raise RuntimeError('Device probe backoff')
 try:profile=prototype_selector.selected_profile(config,token)
 except Exception:
  with DEVICE_PROBE_LOCK:
   _device_probe_record_failure_locked(key,'device_status_unavailable','profile')
  # Retry only when the selector recorded a failed fresh status observation,
  # never a successfully observed but invalid category.
  if recover and prototype_selector.status_observation_remaining(config)>0:
   return _playback_profile_recovery(config,token,cid,started)
  raise
 with DEVICE_PROBE_LOCK:
  # A healthy status read clears only the status lane; it must never clear or
  # renew an unrelated snapshot/identity failure.
  _device_probe_record_success_locked(key,('status',))
 return profile
class Handler(http.server.BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def browser_origin(self):
  origins=self.headers.get_all('Origin') or []
  return origins[0] if len(origins)==1 and origins[0] in (WEB_ORIGIN,local_web_origin()) else None
 def browser_allowed(self,method):
  return self.browser_origin() is not None and web_route(urllib.parse.urlsplit(self.path).path,method)
 def do_OPTIONS(self):
  requested=self.headers.get('Access-Control-Request-Method','')
  raw=self.headers.get('Access-Control-Request-Headers','')
  names={s.strip().lower() for s in raw.split(',') if s.strip()}
  if not self.browser_allowed(requested) or not names.issubset(WEB_HEADERS):
   return self.reply(403,{})
  extra={'Access-Control-Allow-Methods':requested,'Access-Control-Allow-Headers':', '.join(sorted(names)),
         'Access-Control-Max-Age':'600'}
  if self.headers.get('Access-Control-Request-Private-Network')=='true':
   extra['Access-Control-Allow-Private-Network']='true'
  return self.reply(204,b'',extra=extra)
 def log_message(self,*args):pass
 def log_request(self,code='-',size='-'):
  # Status-only structured diagnostics, including rejected requests and preflight.
  try:
   path=urllib.parse.urlsplit(self.path).path
   origin=self.browser_origin()
   requested=self.headers.get('Access-Control-Request-Method','')
   headers={s.strip().lower() for s in self.headers.get('Access-Control-Request-Headers','').split(',') if s.strip()}
   method=self.command if self.command in ('GET','HEAD','POST','DELETE','OPTIONS','PUT','PATCH') else 'other'
   log('HTTP_AUDIT '+json.dumps({'profile':SOURCE_PROFILE,'client':self.client_address[0],
    'method':method,'route':audit_route(path),'status':int(code) if str(code).isdigit() else None,
    'origin':origin or ('unapproved' if self.headers.get_all('Origin') else 'none'),
    'preflightMethod':requested if requested in ('GET','HEAD','POST','DELETE') else None,
    'preflightHeaders':sorted(headers.intersection(WEB_HEADERS)),
    'unknownPreflightHeaders':bool(headers-WEB_HEADERS),
    'privateNetwork':self.headers.get('Access-Control-Request-Private-Network')=='true'},
    separators=(',',':')))
  except Exception:pass
 def serve_web_asset(self,path):
  # This is a LAN-only compatibility entry point. HTTPS remains the public
  # distribution source; no browser-supplied headers are forwarded to it.
  if SOURCE_PROFILE!='american' or not local_web_origin():return self.reply(404,{})
  try:
   client=ipaddress.ip_address(self.client_address[0])
   if not (client.is_private or client.is_loopback):return self.reply(403,{})
  except ValueError:return self.reply(403,{})
  connection=http.client.HTTPSConnection(WEB_ASSET_HOST,timeout=20)
  try:
   connection.request('GET',path,headers={'Accept-Encoding':'identity','User-Agent':'Jealousfin-LAN-assets/1'})
   upstream=connection.getresponse()
   if upstream.status!=200:return self.reply(502,{})
   body=upstream.read(MAX_BODY+1)
   if len(body)>MAX_BODY:return self.reply(502,{})
   mime=upstream.getheader('Content-Type','application/octet-stream')
   if not re.fullmatch(r'[A-Za-z0-9_.+/-]+(?:; ?charset=[A-Za-z0-9_-]+)?',mime):mime='application/octet-stream'
   return self.reply(200,body,mime,{'Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY'})
  except (OSError,http.client.HTTPException,ValueError):
   return self.reply(502,{})
  finally:connection.close()
 def reply(self,code,body,mime='application/json',extra=None):
  if not isinstance(body,bytes):body=json.dumps(body,separators=(',',':')).encode()
  self.send_response(code);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('Connection','close')
  self.send_header('Vary','Origin, Access-Control-Request-Method, Access-Control-Request-Headers, Access-Control-Request-Private-Network')
  if self.browser_origin() and (self.command=='OPTIONS' and code==204 or self.browser_allowed(self.command)):
   self.send_header('Access-Control-Allow-Origin',self.browser_origin())
   self.send_header('Access-Control-Expose-Headers','Content-Length, Content-Range, Accept-Ranges, Content-Type')
  for k,v in (extra or {}).items():self.send_header(k,v)
  self.end_headers();self.close_connection=True
  if self.command!='HEAD':
   try:self.wfile.write(body)
   except (BrokenPipeError,ConnectionResetError):pass
 def authorized(self,q):
  h=self.headers.get('Authorization','')+' '+self.headers.get('X-Emby-Authorization','')
  return hmac.compare_digest(q.get('api_key',[''])[0],TOKEN) or hmac.compare_digest(self.headers.get('X-Emby-Token',''),TOKEN) or ('Token="'+TOKEN+'"') in h
 def proxy_malay(self,config,path,body=None):
  # Fixed local backend; forward only authenticated, enumerated Jellyfin routes.
  headers={k:self.headers[k] for k in ('Authorization','X-Emby-Authorization','X-Emby-Token','Content-Type','Range','X-Jealousfin-Request-ID','Accept') if self.headers.get(k)}
  code,data,response=prototype_selector.read_response(HOST,config['backendPort'],self.path,method=self.command,headers=headers,body=body,limit=prototype_selector.MAX_MEDIA if path.startswith('/malay/media/') else prototype_selector.MAX_JSON,timeout=45)
  if code==200 and path=='/LiveTv/Channels':data=prototype_selector.tag_catalogue(data)
  extra={k:v for k,v in response.items() if k.lower() in ('content-range','accept-ranges')}
  mime=next((v for k,v in response.items() if k.lower()=='content-type'),'application/octet-stream')
  return self.reply(code,data,mime,extra)
 def route_prototype(self,path,q):
  # Removal of the selector file immediately returns the American API to
  # baseline. Restrict the opt-in alias to one configured LAN client.
  if SOURCE_PROFILE!='american':return False
  config=prototype_selector.load_selector(ROOT)
  if config is None or self.client_address[0]!=config['deviceIP']:return False
  if path.startswith('/malay/media/'):
   parts=path.split('/')
   if len(parts)!=5 or not hmac.compare_digest(parts[3],TOKEN):self.reply(401,{});return True
   self.proxy_malay(config,path);return True
  if not self.authorized(q):return False
  token=os.environ.get('JEALOUSFIN_DEVICE_TOKEN','')
  # Read a pending manual Spanish request only on catalogue and playback
  # boundaries. The health poll is deliberately excluded to avoid a callback
  # every few seconds. Failure never blocks the request.
  if path=='/LiveTv/Channels':
   # Admission first: a failed reconnect attempt must not add an optional UI
   # command before the fresh status read.  The manual classification remains
   # synchronous and still precedes the served catalogue.
   malay=selected_profile_admitted(config,token,reconnect=True)=='malay'
   apply_device_spanish_request(config,token)
   overlay_enqueue('refresh')
   if malay:
    self.proxy_malay(config,path);return True
   return False
  if path=='/Jealousfin/Policy':
   # Authenticated Policy polls share the bounded admission recovery already
   # used by catalogue reconnects.  The policy snapshot is read only after a
   # fresh category/Malay decision, so storage failures below stay immediate
   # and are never retried here.
   if selected_profile_admitted(config,token,reconnect=True)=='malay':
    self.proxy_malay(config,path);return True
   return False
  if path=='/LiveTv/Programs':
   if selected_profile_admitted(config,token)=='malay':
    self.proxy_malay(config,path);return True
   return False
  if path=='/Jealousfin/Policy/Event' and self.command=='POST':
   # Peek by consuming once, then restore the local handler's input for
   # American events. Old-session events always go to their actual owner.
   n=int(self.headers.get('Content-Length','0'))
   if not 0<n<=8192:return False
   body=self.rfile.read(n)
   cid=prototype_selector.channel_id(path,body)
   if cid in prototype_selector.source_ids(ROOT):
    self.proxy_malay(config,path,body);return True
   self.rfile=io.BytesIO(body)
   return False
  cid=prototype_selector.channel_id(path)
  if cid:
   malay=cid in prototype_selector.source_ids(ROOT)
   if path.endswith('/PlaybackInfo'):
    token=os.environ.get('JEALOUSFIN_DEVICE_TOKEN','')
    foreground=playback_admission_foreground(path,cid,malay)
    selected=selected_profile_admitted(config,token,foreground=foreground,cid=cid)
    if (selected=='malay')!=malay:
     self.reply(409,{'JealousfinPolicyError':'excluded'});return True
    if re.fullmatch(r'/Items/[a-f0-9]{32}/PlaybackInfo',path):
     # Optional UI callbacks follow successful fresh profile admission.
     # Keep both admitted profiles and the original async marker boundary.
     enqueue_playback_marker(config,token)
     overlay_enqueue('refresh')
   if malay:
    n=int(self.headers.get('Content-Length','0'))
    if not 0<=n<=262144:self.reply(413,{});return True
    body=self.rfile.read(n) if n else None
    self.proxy_malay(config,path,body);return True
  return False
 def handle_request(self):
  reload_gateway_config()
  correlation=request_correlation(self.headers.get('X-Jealousfin-Request-ID'))
  p=urllib.parse.urlsplit(self.path);path=p.path;q=urllib.parse.parse_qs(p.query)
  try:
   if self.route_prototype(path,q):return
  except (OSError,RuntimeError,ValueError,KeyError,TypeError,http.client.HTTPException) as error:
   record_infrastructure_error(None,'selector',error,correlation)
   return self.reply(503,{'JealousfinPolicyError':'unavailable'})
  if SOURCE_PROFILE=='malay' and path.startswith('/malay/media/'):path=path[len('/malay'):]
  with lock:counts[path.split('/')[1] if path.startswith('/') else 'other']=counts.get(path.split('/')[1] if path.startswith('/') else 'other',0)+1
  if path=='/System/Info/Public':return self.reply(200,{'ServerName':'Jealousfin IPTV Trial' if SOURCE_PROFILE=='american' else 'Jealousfin Malay Prototype','Version':'10.10.0','Id':USER_ID})
  if path=='/trial-status' and self.client_address[0] in (HOST,'127.0.0.1'):
   try:snapshot=policy_rows();ready=True
   except (sqlite3.Error,OSError,RuntimeError,ValueError):snapshot={};ready=False
   try:language_description=spanish_classification.describe(spanish_state)
   except (ValueError,TypeError):language_description={'entries':len(spanish_provider_ids)}
   try:language_pending_description=language_pending.describe(ROOT)
   except (ValueError,OSError,TypeError):language_pending_description={'entries':0}
   language_status=spanish_auto_status()
   overlay_request_pump()
   return self.reply(200,{'sourceProfile':SOURCE_PROFILE,'channels':len(channels),'requests':counts,'resources':len(resources),'policyRevision':'sqlite-policy-v2','policyReady':ready,'policyChannels':len(snapshot),'configurationRevision':gateway_config['revision'],'configurationFault':config_fault,'resourceLimit':RESOURCE_LIMIT,'segmentCacheEntries':len(segment_cache),'segmentCacheBytes':segment_cache_bytes,'favorites':len(favorites),'catalogueSource':catalogue_source,'catalogueFault':catalogue_fault,'programmeCount':len(programme_snapshot()),'programmeObservedAt':max(programme_observed_at,paged_last_success),'programmeFault':programme_fault,'programmeRefreshFailures':programme_refresh_failures,'programmeLastAttemptAt':programme_last_attempt,'programmeNextAttemptAt':programme_next_attempt,'externalProgrammeCount':len(guide_snapshot(time.time())),'externalProgrammeObservedAt':guide_observed_at,'externalProgrammeFault':guide_fault,'episodeGuideObservedAt':episode_observed_at,'episodeGuideFault':episode_fault,'episodeLookupFault':episode_lookup_fault,'episodeLookupErrors':episode_lookup_errors,'episodeLookupDeferred':episode_lookup_deferred,'episodeLookupLastError':episode_lookup_last_error,'episodeCandidateErrors':episode_candidate_errors,'episodeCandidateLastError':episode_candidate_last_error,'episodeProgrammeCount':sum(bool(r.get('episodeTitle')) for r in programme_snapshot().values()),'episodeVerifiedCount':sum(bool(r.get('episodeNumber')) for r in programme_snapshot().values()),'episodeSeasonYearCount':sum(bool(r.get('seasonYear')) for r in programme_snapshot().values()),'regionalProgrammeCount':len(regional_snapshot(time.time())),'regionalProgrammeFault':regional_fault,'regionalProgrammeObservedAt':regional_observed_at,'metadataResearch':discovery_status(),'ukManifest':dict(uk_manifest_status,trialEnabled=gateway_config['ukTrialEnabled'],cohortSize=len(uk_cohort(gateway_config))),'expandedManifest':dict(expanded_manifest_status,cohortSize=len(expanded_cohort(gateway_config))),'trialCohortSize':len(trial_cohort(gateway_config)),'programmeYearCount':sum(cached_year(row) is not None for row in programme_snapshot().values()),'programmeYearFault':year_fault,'programmeYearErrors':year_errors,'programmeYearLastError':year_last_error,'programmeYearDeferred':sum(until>time.time() for until in list(year_retry.values())),'programmeYearLastLookup':year_last_lookup,'languageExclusions':language_description,'languagePending':language_pending_description,'languageAuto':language_status,'spanishClassification':language_description,'spanishAuto':language_status,'episodeAudio':episode_audio_status(),'overlay':overlay_status(),'programmeOverlay':overlay_combined_status(),'scheduleEpisode':schedule_episode_status(),'deliveryQuality':quality_summary()})
  if path in ('/spanish-auto','/language-auto') and self.client_address[0] in (HOST,'127.0.0.1'):
   if self.command=='GET':return self.reply(200,spanish_auto_admin())
   if self.command=='POST':
    n=int(self.headers.get('Content-Length','0'))
    if not 0<n<=4096:return self.reply(400,{})
    try:data=json.loads(self.rfile.read(n))
    except (ValueError,TypeError):return self.reply(400,{})
    allowed={'enabled','enforcementEnabled','threshold'}
    if not isinstance(data,dict) or not data or not set(data).issubset(allowed):return self.reply(400,{})
    try:set_language_auto_control(enabled=data.get('enabled'),enforcement=data.get('enforcementEnabled'),threshold=data.get('threshold'))
    except (ValueError,OSError,TypeError):return self.reply(503,{})
    return self.reply(200,spanish_auto_admin())
   return self.reply(405,{})
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
   if not entry:
    MEDIA_CONTINUITY.stale_handle(rid)
    return self.reply(404,{})
   cid,url=entry
   playback_activity.touch(cid)
   started=time.monotonic();kind='segment' if rid in segment_resources else 'other'
   try:
    rg=self.headers.get('Range')
    if rg and not re.fullmatch(r'bytes=\d*-\d*',rg):return self.reply(416,{})
    (data,headers,status,final),cache_hit=fetch_media(rid,url,rg)
    record_delivery(cid,kind,time.monotonic()-started,len(data),cache_hit)
    if spanish_auto_manager is not None and not data.lstrip(b'\xef\xbb\xbf \r\n').startswith(b'#EXTM3U'):
     if rid in key_resources and status==200:
      if 16<=len(data)<=64:
       if len(key_cache)>=256 and rid not in key_cache:
        try:key_cache.pop(next(iter(key_cache)))
        except (StopIteration,KeyError):pass
       key_cache[rid]=bytes(data)
       spanish_auto_manager.tap_key(rid,data)
     elif rid in map_bases:
      spanish_auto_manager.tap_map(map_bases[rid],data)
     elif kind=='segment' and rg is None and status==200:
      base=resource_bases.get(rid)
      if base:
       crypto=segment_keys.get(rid) or {}
       key_rid=crypto.get('keyRid')
       spanish_auto_manager.tap(cid,sources.get(cid),channels.get(cid,{}).get('Name',''),rid,base,data,rid in audio_segments,headers.get('Content-Type','application/octet-stream'),correlation,key_bytes=key_cache.get(key_rid) if key_rid else None,key_rid=key_rid,iv=crypto.get('iv'),sequence=crypto.get('sequence'),request_started_at=started)
    if data.lstrip(b'\xef\xbb\xbf \r\n').startswith(b'#EXTM3U'):
     if len(data)>MAX_PLAYLIST:
      record_source_error(cid,url,'media','Playlist exceeds gateway limit',correlation);return self.reply(502,{})
     return self.reply(200,rewrite(data,final,cid,correlation,audio_only=rid in audio_playlists),'application/vnd.apple.mpegurl')
    extra={k:headers[k] for k in ('Content-Range','Accept-Ranges') if headers.get(k)}
    return self.reply(status,data,headers.get('Content-Type','application/octet-stream'),extra)
   except MediaCompatibilityError:
    record_delivery(cid,kind,time.monotonic()-started,0,error=True);record_source_error(cid,url,'compatibility','Unconfigured public media endpoint',correlation);return self.reply(502,{})
   except urllib.error.HTTPError as e:
    record_delivery(cid,kind,time.monotonic()-started,0,error=True);record_source_error(cid,url,'media','HTTP '+str(e.code),correlation);log('upstream HTTP '+str(e.code));return self.reply(502,{})
   except Exception as e:
    record_delivery(cid,kind,time.monotonic()-started,0,error=True);record_source_error(cid,url,'media',type(e).__name__,correlation);log('upstream failure '+type(e).__name__);return self.reply(502,{})
  if not self.authorized(q):return self.reply(401,{})
  if path in ('/LiveTv/Channels','/Jealousfin/Policy'):
   try:snapshot=policy_rows()
   except (sqlite3.Error,OSError,RuntimeError,ValueError) as error:
    record_infrastructure_error(None,'policy',error,correlation);return self.reply(503,{'JealousfinPolicyError':'unavailable'})
   if path=='/Jealousfin/Policy':return self.reply(200,policy_payload(snapshot))
   include=q.get('JealousfinIncludePolicy',['false'])[0]=='true'
   with lock:out=[dict(c,UserData={'IsFavorite':cid in favorites}) for cid,c in ordered_catalogue(snapshot,include)]
   if SOURCE_PROFILE=='american' and gateway_config['ukTrialEnabled']:
    selector=prototype_selector.load_selector(ROOT)
    if selector and self.client_address[0]==selector['deviceIP']:out=uk_trial_projection(out,gateway_config)
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
    if action in ('failure','success'):playback_activity.correlate_trace(cid,trace)
    return self.reply(200,policy_payload(policy_rows()))
   except (KeyError,ValueError,TypeError):return self.reply(400,{})
   except (sqlite3.Error,OSError,RuntimeError,ValueError) as error:
    record_infrastructure_error(None,'policy',error,correlation);return self.reply(503,{'JealousfinPolicyError':'unavailable'})
  if path=='/LiveTv/Programs':
   # The current-title feed is not a full EPG. For queries needing a real
   # time range, do not invent times; the client can use CurrentProgram.
   selected=set(','.join(q.get('ChannelIds',[])).split(','))-{''}
   out=[programme_item(cid,row) for cid,row in programme_snapshot().items() if language_visible(cid) and (not selected or cid in selected)]
   return self.reply(200,{'Items':out,'TotalRecordCount':len(out),'JealousfinCurrentOnly':True})
  if path=='/LiveTv/Recordings':return self.reply(200,{'Items':[],'TotalRecordCount':0})
  m=re.fullmatch(r'/Items/([a-f0-9]{32})/PlaybackInfo',path)
  if m and m[1] in sources:
   n=int(self.headers.get('Content-Length','0'))
   if n>262144:return self.reply(413,{})
   if n:self.rfile.read(n)
   cid=m[1]
   if not language_visible(cid):return self.reply(409,{'JealousfinPolicyError':'excluded'})
   try:snapshot=policy_rows()
   except (sqlite3.Error,OSError,RuntimeError,ValueError) as error:
    record_infrastructure_error(None,'policy',error,correlation);return self.reply(503,{'JealousfinPolicyError':'unavailable'})
   if not policy_eligible(snapshot[cid],time.time()):return self.reply(409,{'JealousfinPolicyError':'excluded'})
   record_attempt(cid)
   with lock:
    now=time.monotonic();active_channels[cid]=now+gateway_config['activeProtectionSeconds'];evict_resources(now)
   try:url=register(resolve(cid),cid,correlation);url=url.rsplit('.',1)[0]+'.m3u8'
   except MediaCompatibilityError:
    record_source_error(cid,None,'compatibility','Unconfigured public media endpoint',correlation)
    return self.reply(409,{'JealousfinPolicyError':'excluded'})
   except Exception as e:
    record_source_error(cid,None,'resolve','HTTP '+str(e.code) if isinstance(e,urllib.error.HTTPError) else ('No usable stream source' if isinstance(e,ValueError) and str(e)=='No usable stream source' else type(e).__name__),correlation);return self.reply(502,{})
   try:snapshot=policy_rows()
   except (sqlite3.Error,OSError,RuntimeError,ValueError) as error:
    record_infrastructure_error(None,'policy',error,correlation);return self.reply(503,{'JealousfinPolicyError':'unavailable'})
   if not language_visible(cid) or not policy_eligible(snapshot[cid],time.time()):return self.reply(409,{'JealousfinPolicyError':'excluded'})
   play_id=uuid.uuid4().hex
   playback_activity.issue(cid,play_id)
   playback_activity.touch(cid)
   return self.reply(200,{'PlaySessionId':play_id,'MediaSources':[{'Id':cid,'TranscodingUrl':url,'SupportsTranscoding':True,'SupportsDirectPlay':False,'SupportsDirectStream':False}]})
  if path.startswith('/Sessions/Playing') or path=='/LiveStreams/Close':
   n=int(self.headers.get('Content-Length','0'))
   if n>262144:return self.reply(413,{})
   payload=None
   if n:
    try:payload=json.loads(self.rfile.read(n))
    except (ValueError,TypeError):payload=None
   action={'/Sessions/Playing':'start','/Sessions/Playing/Progress':'progress','/Sessions/Playing/Stopped':'stop'}.get(path)
   report=programme_research.parse_playback_report(payload,action) if action else None
   if report and report['channelID'] in channels:playback_activity.observe(report,wall=time.time())
   return self.reply(200,{})
  m=re.fullmatch(r'/Users/[^/]+/FavoriteItems/([a-f0-9]{32})',path)
  if m and m[1] in channels:
   if self.command not in ('POST','DELETE'):return self.reply(405,{})
   set_favorite(m[1],self.command=='POST')
   return self.reply(200,{})
  return self.reply(404,{})
 def do_GET(self):
  # Serve a narrowly scoped static entry point before the API route checks.
  if self.command in ('GET','HEAD') and web_asset(urllib.parse.urlsplit(self.path).path):
   if self.headers.get_all('Origin') and self.browser_origin() is None:return self.reply(403,{})
   return self.serve_web_asset(urllib.parse.urlsplit(self.path).path)
  # Reject unexpected browser origins before authentication or side effects.
  # CORS is not authentication: permitted routes still require existing tokens.
  if self.headers.get_all('Origin') and not self.browser_allowed(self.command):
   return self.reply(403,{})
  try:self.handle_request()
  except Exception as e:
   log('request failure '+type(e).__name__)
   try:self.reply(500,{})
   except Exception:pass
 do_POST=do_GET
 do_DELETE=do_GET
 do_HEAD=do_GET
def main():
 global PROVIDER,spanish_auto_manager
 PROVIDER=provider()
 MEDIA_CONTINUITY.begin_generation()
 reload_gateway_config(force=True)
 initialize_catalogue()
 load_favorites()
 maybe_reload_spanish(force=True)
 server=Server((HOST,PORT),Handler)
 (ROOT/'trial-info.json').write_text(json.dumps({'sourceProfile':SOURCE_PROFILE,'url':f'http://{HOST}:{PORT}','pid':os.getpid(),'expiresAt':None,'channels':[{'id':x,'name':c['Name']} for x,c in channels.items()]}))
 stop=threading.Event()
 overlay_publisher=None
 if SOURCE_PROFILE=='american':
  model=resolve_whisper_model()
  spanish_auto_manager=language_audio.Manager(ROOT,model or (ROOT/'models'/'unavailable.bin'),language_auto_commit,lambda:reload_spanish_auto_control(),probe=device_language_probe,log=log,threads=4,visits=language_visit_identity(),on_speech=episode_speech_callback,on_state=language_state_callback)
  if not spanish_auto_manager.start():log('Language auto unavailable '+str(spanish_auto_manager.fault))
  pending=threading.Thread(target=language_pending_worker,args=(stop,),name='language-pending',daemon=True);pending.start()
  overlay_publisher=threading.Thread(target=overlay_worker,args=(stop,),name='language-overlay-publisher',daemon=True);overlay_publisher.start()
  episode_prefetch=threading.Thread(target=episode_source_prefetch_worker,args=(stop,),name='episode-source-prefetch',daemon=True);episode_prefetch.start()
 worker=threading.Thread(target=programme_worker,args=(stop,),name='programme-refresh',daemon=True);worker.start()
 guide=threading.Thread(target=guide_worker,args=(stop,),name='external-guide-refresh',daemon=True);guide.start()
 if SOURCE_PROFILE=='american':
  catalog=threading.Thread(target=catalog_worker,args=(stop,),name='episode-catalog-refresh',daemon=True);catalog.start()
 epg=threading.Thread(target=episode_guide_worker,args=(stop,),name='episode-guide-refresh',daemon=True);epg.start()
 episodes=threading.Thread(target=episode_worker,args=(stop,),name='episode-metadata-refresh',daemon=True);episodes.start()
 regional=threading.Thread(target=regional_worker,args=(stop,),name='regional-guide-refresh',daemon=True);regional.start()
 research=threading.Thread(target=discovery_worker,args=(stop,),name='programme-research',daemon=True);research.start()
 years=threading.Thread(target=year_worker,args=(stop,),name='programme-year-refresh',daemon=True);years.start()
 log('READY channels='+str(len(channels)))
 try:server.serve_forever(poll_interval=.5)
 finally:
  stop.set()
  if overlay_publisher is not None:
   try:overlay_jobs.put_nowait(None)
   except queue.Full:pass
   overlay_publisher.join(timeout=2)
  if spanish_auto_manager is not None:spanish_auto_manager.stop()
  server.server_close()
  try:(ROOT/'trial-info.json').unlink()
  except FileNotFoundError:pass
if __name__=='__main__':main()

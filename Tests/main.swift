import Foundation

var checks=0
func check(_ condition:@autoclosure()->Bool,_ label:String){
    checks += 1
    guard condition() else {fatalError("FAIL: \(label)")}
}
let start=Date(timeIntervalSince1970:1_700_000_000)
var h=ChannelHealth()
var date=start
for n in 1...7 {
    h.fail(at:date,reason:"fixture")
    check(h.failures==n,"failure count")
    check(!h.banned,"not banned early")
    let end=date.addingTimeInterval(Double(n*3)*86400)
    check(h.hiatusUntil==end,"progressive delay")
    check(!h.eligible(at:end.addingTimeInterval(-1)),"before expiry")
    h.fail(at:date,reason:"duplicate attempt during hiatus")
    check(h.failures==n,"not double counted")
    check(h.eligible(at:end),"at expiry")
    date=end
}
h.fail(at:date,reason:"final attempt")
check(h.banned,"permanent after final failed retest")
check(!h.eligible(at:date.addingTimeInterval(1000*86400)),"permanent")
h.succeed(at:date)
check(h.banned,"success does not bypass approval")
h.approve(at:date)
check(h.eligible(at:date),"explicit reinstatement")
check(h.failures==0,"reset streak")
check(h.lifetimeFailures==8,"retain historical count")
var a=ChannelHealth()
a.fail(at:start,reason:"fixture")
a.succeed(at:start.addingTimeInterval(3*86400))
check(a.failures==0 && a.successes==1,"rehabilitation")
var ledger=HealthLedger()
let event=HealthEvent(at:start,channelID:"a",channelName:"Fixture",action:"failure",reason:"fixture")
ledger.events=[event,event]
check(ledger.states()["a"]?.failures==1,"event id dedup")
var remote=HealthLedger();remote.events=[event]
ledger.merge(remote)
check(ledger.states()["a"]?.lifetimeFailures==1,"idempotent merge")
let encoded=try JSONCodec.encoder().encode(ledger)
let restored=try JSONCodec.decoder().decode(HealthLedger.self,from:encoded)
check(restored.states()==ledger.states(),"persistence roundtrip")
check(JSONCodec.date("2026-11-24T13:37:49.0000000Z") != nil,"Jellyfin fractional timestamp")
print("PASS: \(checks) channel policy and persistence assertions")

let done=Set((0..<100).map{String(format:"%03d.json",$0)})
let queue=Array(done)+["100.json","101.json","100.json"]
check(CommandQueue.pending(queue,receipts:done,limit:20)==["100.json","101.json"],"completed commands do not starve new ones")
check(CommandQueue.pending(queue,receipts:done,limit:1)==["100.json"],"batch bounded")
check(CommandQueue.pending(queue,receipts:done,limit:0).isEmpty,"zero command budget")
check(SurfClock.remaining(deadline:start.addingTimeInterval(42.1),now:start)==43,"rotation countdown")
check(SurfClock.remaining(deadline:start,now:start.addingTimeInterval(9))==0,"rotation clamps")
check(SurfClock.resumed(now:start,remaining:42)==start.addingTimeInterval(42),"pause preserves rotation")
check(PlaybackFailurePolicy.confirms(serverHealthy:true,recentSuccess:true,consecutiveFailures:1,reason:"Startup timeout"),"corroborated timeout")
for reason in ["HTTP 401","HTTP 403","HTTP 404","HTTP 429","HTTP 503","network offline","DNS failure","No media source returned","certificate error"] {
    check(!PlaybackFailurePolicy.confirms(serverHealthy:true,recentSuccess:true,consecutiveFailures:1,reason:reason),"infrastructure or ambiguous error: "+reason)
}
check(!PlaybackFailurePolicy.confirms(serverHealthy:false,recentSuccess:true,consecutiveFailures:1,reason:"Startup timeout"),"server outage")
check(!PlaybackFailurePolicy.confirms(serverHealthy:true,recentSuccess:false,consecutiveFailures:1,reason:"Startup timeout"),"no positive baseline")
check(!PlaybackFailurePolicy.confirms(serverHealthy:true,recentSuccess:true,consecutiveFailures:3,reason:"Startup timeout"),"systemic failure guard")
check(!PlaybackFailurePolicy.confirms(serverHealthy:true,recentSuccess:true,consecutiveFailures:1,reason:"Startup timeout",paused:true),"user pause")
check(!PlaybackFailurePolicy.confirms(serverHealthy:true,recentSuccess:true,consecutiveFailures:1,reason:"Startup timeout",cancelled:true),"cancellation")
print("PASS: \(checks) total assertions including scheduling, command fairness and failure classification")

var video=VideoEvidence()
video.observe(frameTime:0,monotonic:100)
check(!video.qualifies(progress:4,monotonic:101),"one frame cannot qualify")
video.observe(frameTime:0,monotonic:101)
check(video.frameCount==1,"duplicate video timestamp")
video.observe(frameTime:1,monotonic:101)
video.observe(frameTime:2,monotonic:102)
check(!video.qualifies(progress:2,monotonic:102),"insufficient clock progress")
check(video.qualifies(progress:3,monotonic:103),"fresh multiple frames and progress")
check(!video.qualifies(progress:10,monotonic:105),"stale video sample")
check(!video.qualifies(progress:Double.nan,monotonic:103),"invalid clock")
video.observe(frameTime:Double.nan,monotonic:104)
check(video.frameCount==3,"reject invalid decoded time")
check(PlaybackFailurePolicy.confirms(serverHealthy:true,recentSuccess:true,consecutiveFailures:1,reason:"Stream HTTP 404"),"repeated stream-level failure differs from catalogue 404")
print("PASS: \(checks) total assertions including fresh video evidence")

let command=Command(id:"command-001",action:"get_status",arguments:[:],expiresAt:nil)
let commandData=try JSONCodec.encoder().encode(command)
let parsed=try CommandQueue.decode(commandData,expectedID:"command-001")
check(parsed.id==command.id,"command identity match")
check((try? CommandQueue.decode(commandData,expectedID:"command-002"))==nil,"command filename mismatch")
check((try? CommandQueue.decode(Data("broken".utf8),expectedID:"command-001"))==nil,"malformed command isolated")
let bad=Command(schemaVersion:2,id:"command-001",action:"get_status",arguments:[:],expiresAt:nil)
let badData=try JSONCodec.encoder().encode(bad)
check((try? CommandQueue.decode(badData,expectedID:"command-001"))==nil,"unsupported schema rejected")
print("PASS: \(checks) total assertions including malformed command handling")

var cadence=TelemetryCadence()
check(cadence.shouldSample(at:100),"automatic first system sample")
check(!cadence.shouldSample(at:101),"no excessive polling")
check(cadence.shouldSample(at:115),"15 second sample")
check(cadence.shouldSample(at:116,force:true),"forced lifecycle sample")
check(!cadence.shouldSample(at:117),"no duplicate scheduled sample")
check(cadence.shouldSample(at:500),"resume after long suspension")
check(!cadence.shouldSample(at:Double.nan),"reject invalid monotonic time")
check(cadence.shouldSample(at:1),"reset monotonic clock safely")
print("PASS: \(checks) total assertions including automatic telemetry cadence")

let policy=RuntimePolicy.baseline(Settings())
check((try? policy.checked()) != nil,"runtime baseline valid")
var invalidPolicy=policy;invalidPolicy.retries=999
check((try? invalidPolicy.checked())==nil,"invalid patch rejected")
let step=DeviceTestStep(kind:"system",channelID:nil,seconds:1,source:nil)
let plan=DeviceTestPlan(id:"qa",steps:[step],repeatCount:2,repeatForever:false)
check((try? plan.checked()) != nil,"test plan valid")
var cp=DeviceTestCheckpoint(plan:plan)
let outcome=DeviceTestResult(id:cp.resultID,planID:plan.id,cycle:0,stepIndex:0,kind:"system",channelID:nil,startedAt:Date(),status:"passed",evidence:[:],launchID:"launch",patchID:"baseline")
cp.advance(outcome)
check(cp.cycle==1 && cp.canResume,"campaign advances and resumes")
cp.advance(outcome)
check(cp.cycle==1,"duplicate result does not advance")
var last=outcome;last.cycle=1;last.id=cp.resultID
cp.advance(last)
check(cp.state=="completed" && !cp.canResume,"finite campaign completes")
let moduleDecision=ModuleDecision(state:["counter":"1"],actions:[],nextSeconds:10,insights:[])
check((try? moduleDecision.checked()) != nil,"dynamic decision valid")
var badDecision=moduleDecision;badDecision.nextSeconds=0
check((try? badDecision.checked())==nil,"reject tight loop")
print("PASS: \(checks) total assertions including campaign and runtime policy")


let layoutData=try Data(contentsOf:URL(fileURLWithPath:"Examples/dynamic-ui.json"))
let layout=try JSONCodec.decoder().decode(DynamicUILayout.self,from:layoutData)
check((try? layout.checked()) != nil,"sample runtime UI layout valid")
check(layout.node("qa-btn")?.action?.command=="get_telemetry","nested command discovery")
check(layout.node("qa-web")?.kind=="web","custom component discovery")
check(layout.formDefaults()["minutes"]=="5","form default")
check(layout.retainedValues(["minutes":"10","removed":"old"])==["minutes":"10"],"valid existing field retained across revision")
check(layout.retainedValues(["minutes":"99"])==["minutes":"5"],"invalid old option restored to default")
let bindingValues=["minutes":"10"]
let bindingState:[String:Any]=["surfing":true,"systemMetrics":["networkState":"satisfied"],"eligible":12]
let bound=try DynamicUIBinding.arguments(["minutes":"$form.minutes","network":"$state.systemMetrics.networkState","literal":"unchanged"],values:bindingValues,state:bindingState)
check(bound==["minutes":"10","network":"satisfied","literal":"unchanged"],"UI action parameter binding")
check(DynamicUIBinding.truth(DynamicUIBinding.resolve("state.surfing",values:[:],state:bindingState)),"boolean visibility")
check(DynamicUIBinding.resolve("state.eligible",values:[:],state:bindingState)=="12","live numeric binding")
check((try? DynamicUIBinding.arguments(["minutes":"$form.missing"],values:[:],state:[:]))==nil,"missing binding rejected")
var badLayout=layout;badLayout.schemaVersion=2
check((try? badLayout.checked())==nil,"unknown UI schema rejected")
badLayout=layout;badLayout.slots.append(layout.slots[0])
check((try? badLayout.checked())==nil,"duplicate slot rejected")
badLayout=layout;badLayout.pages[0].nodes.append(badLayout.pages[0].nodes[0])
check((try? badLayout.checked())==nil,"duplicate node ID rejected")
badLayout=layout;badLayout.slots[0].nodes[0].children?[0].action?.command="approve_reinstatement"
check((try? badLayout.checked())==nil,"UI cannot bypass explicit reinstatement")
badLayout=layout;badLayout.pages[0].nodes[4].height=1e9
check((try? badLayout.checked())==nil,"invalid web size rejected")
let store=DynamicUIStore(active:layout,previous:nil,values:["minutes":"10"])
let storedBytes=try JSONCodec.encoder().encode(store)
let decodedStore=try JSONCodec.decoder().decode(DynamicUIStore.self,from:storedBytes)
check(decodedStore.active==layout && decodedStore.values["minutes"]=="10","UI persists across restart")
print("PASS: \(checks) total assertions including dynamic UI layouts and bindings")


let decisionNow=Date(timeIntervalSince1970:1700000000)
let decisionFixture=DecisionPrompt(schemaVersion:1,id:"diagnostic-repair",title:"Playback recovery",message:"Apply revised playback settings?",priority:"High",evidence:["Stalls":"3"],expiresAt:decisionNow.addingTimeInterval(600),expectedChannelID:"channel-a",expectedPatchID:"baseline",choices:[DecisionChoice(id:"fix",title:"Fix it",detail:"Reconnect to Jellyfin",action:DynamicUIAction(command:"reconnect",arguments:nil)),DecisionChoice(id:"cancel",title:"Cancel",detail:nil,action:nil)])
var decisionJournalFixture=DecisionJournal()
try decisionJournalFixture.insert(decisionFixture,at:decisionNow)
try decisionJournalFixture.insert(decisionFixture,at:decisionNow)
check(decisionJournalFixture.records.count==1,"duplicate decision id is idempotent")
check(decisionJournalFixture.next(at:decisionNow)?.id==decisionFixture.id,"decision queued")
var decisionConflictFixture=decisionFixture;decisionConflictFixture.title="Changed decision"
var conflictRejected=false
do{try decisionJournalFixture.insert(decisionConflictFixture,at:decisionNow)}catch{conflictRejected=true}
check(conflictRejected,"conflicting prompt cannot overwrite prior approval")
var decisionRecordFixture=decisionJournalFixture.records[0]
var hiddenApprovalRejected=false
do{try decisionRecordFixture.choose("fix",at:decisionNow)}catch{hiddenApprovalRejected=true}
check(hiddenApprovalRejected,"unpresented decision cannot be approved")
decisionRecordFixture.present(at:decisionNow)
try decisionRecordFixture.choose("cancel",at:decisionNow)
check(decisionRecordFixture.status=="declined" && decisionRecordFixture.chosenAction==nil && decisionRecordFixture.terminal,"cancel has no action")
decisionRecordFixture=decisionJournalFixture.records[0];decisionRecordFixture.present(at:decisionNow)
try decisionRecordFixture.choose("fix",at:decisionNow)
check(decisionRecordFixture.status=="approved" && decisionRecordFixture.chosenAction?.command=="reconnect","explicit approval binds fixed repair")
check(!decisionFixture.contextMatches(channelID:"channel-b",patchID:"baseline"),"stale channel detected")
check(!decisionFixture.contextMatches(channelID:"channel-a",patchID:"new"),"stale module detected")
check(decisionFixture.contextMatches(channelID:"channel-a",patchID:"baseline"),"matching context")
var decisionReceiptFixture=Receipt(id:"other",action:"reconnect",status:"succeeded",result:"OK")
decisionRecordFixture.finish(decisionReceiptFixture,at:decisionNow)
check(decisionRecordFixture.status=="approved","unrelated receipt cannot complete decision")
decisionReceiptFixture.id=decisionRecordFixture.commandID
decisionRecordFixture.finish(decisionReceiptFixture,at:decisionNow)
check(decisionRecordFixture.status=="succeeded" && decisionRecordFixture.terminal,"stable receipt completes approved decision")
var decisionExpiredFixture=decisionJournalFixture.records[0];decisionExpiredFixture.expire(at:decisionNow.addingTimeInterval(601))
check(decisionExpiredFixture.status=="expired" && decisionExpiredFixture.chosenAction==nil,"expiry never applies a repair")
var decisionUnsafeFixture=decisionFixture;decisionUnsafeFixture.choices[0].action=DynamicUIAction(command:"approve_reinstatement",arguments:[:])
check((try? decisionUnsafeFixture.checked(now:decisionNow))==nil,"decision cannot bypass reinstatement approval")
decisionJournalFixture.records[0]=decisionRecordFixture
decisionJournalFixture.markUploaded(id:decisionRecordFixture.id,revision:1)
check(decisionJournalFixture.pendingUpload.count==1,"older acknowledgement retains newer decision state")
decisionJournalFixture.markUploaded(id:decisionRecordFixture.id,revision:decisionRecordFixture.revision)
check(decisionJournalFixture.pendingUpload.isEmpty,"latest decision uploaded")
let decisionRestoredFixture=try JSONCodec.decoder().decode(DecisionJournal.self,from:JSONCodec.encoder().encode(decisionJournalFixture))
check(decisionRestoredFixture.records.first?.receipt?.id==decisionRecordFixture.commandID,"decision and receipt persist across restart")
print("PASS: \(checks) total assertions including interactive decisions")


let apiDefaults=JellyfinAdapterSpec()
check((try? apiDefaults.checked()) != nil,"native API adapter defaults valid")
var apiBad=apiDefaults;apiBad.logoPath="../{id}"
check((try? apiBad.checked())==nil,"invalid image route")
check(JellyfinAdapterRules.operation("Items/fixture/PlaybackInfo")=="playback_info","playback operation classification")
check(JellyfinAdapterRules.operation("Sessions/Playing/Progress")=="session_progress","session operation classification")
check(JellyfinAdapterRules.string(at:"User.Id",in:["User":["Id":"fixture"]])=="fixture","nested auth response field")
check(!JellyfinAdapterRules.validPath("https://example.invalid"),"absolute patched request disallowed")
check(!JellyfinAdapterRules.validPath("Items/../secret"),"parent path disallowed")
check(!JellyfinAdapterRules.validPath("Items/%2e%2e"),"encoded traversal disallowed")
let apiOrigin=URL(string:"http://example.invalid:8096")!
check(JellyfinAdapterRules.sameOrigin(URL(string:"http://example.invalid:8096/live.m3u8")!,apiOrigin),"same origin stream")
check(!JellyfinAdapterRules.sameOrigin(URL(string:"http://example.invalid:8103/live.m3u8")!,apiOrigin),"different port not same origin")
check(!JellyfinAdapterRules.sameOrigin(URL(string:"http://elsewhere.invalid:8096/live.m3u8")!,apiOrigin),"different host not same origin")
let apiPlan:[String:Any]=["path":"LiveTv/Channels","method":"GET","query":["Limit":"50"],"body":NSNull()]
let apiParsed=try JellyfinAdapterRules.request(apiPlan,authenticate:false)
check(apiParsed.path=="LiveTv/Channels" && apiParsed.query["Limit"]=="50","patched request plan")
check((try? JellyfinAdapterRules.request(apiPlan,authenticate:true))==nil,"authentication must remain POST")
var apiDecision=decisionFixture;apiDecision.expectedJellyfinAPIRevision="rev-a"
check(!apiDecision.contextMatches(channelID:"channel-a",patchID:"baseline",apiRevision:"rev-b"),"API repair context must match")
check(apiDecision.contextMatches(channelID:"channel-a",patchID:"baseline",apiRevision:"rev-a"),"API repair context matches")
print("PASS: \(checks) total assertions including Jellyfin adapter rules")


let bundledReleaseFixture=try BundledPatchBaseline.load()
check((try? bundledReleaseFixture.checked()) != nil,"embedded release baseline validates")
let firstPatchFixture=PatchSource(id:"first",source:"var hooks={};")
let secondPatchFixture=PatchSource(id:"second",source:"var hooks={configure:function(p){return p;}};")
check(BaselineMerge.select(local:Optional<PatchSource>.none,previous:nil,incoming:firstPatchFixture,hadLocal:false,hadPrevious:false)==firstPatchFixture,"clean install inherits release patch")
check(BaselineMerge.select(local:firstPatchFixture,previous:firstPatchFixture,incoming:secondPatchFixture,hadLocal:true,hadPrevious:true)==secondPatchFixture,"upgrade follows promoted revision")
check(BaselineMerge.select(local:secondPatchFixture,previous:firstPatchFixture,incoming:Optional<PatchSource>.none,hadLocal:true,hadPrevious:true)==secondPatchFixture,"independent local patch retained")
check(BaselineMerge.select(local:firstPatchFixture,previous:firstPatchFixture,incoming:Optional<PatchSource>.none,hadLocal:true,hadPrevious:true)==nil,"promoted reset removes previous bundled patch")
check(BaselineMerge.select(local:Optional<PatchSource>.none,previous:firstPatchFixture,incoming:secondPatchFixture,hadLocal:true,hadPrevious:true)==nil,"explicit local reset retained")
let releaseFixture=ReleaseBaseline(revision:"reviewed",archiveID:"archive",deviceID:"fixture",evidence:"regression evidence",sources:PatchSourceSet(runtime:firstPatchFixture))
check((try? releaseFixture.checked()) != nil,"reviewed source set valid")
let releaseRestoredFixture=try JSONCodec.decoder().decode(ReleaseBaseline.self,from:JSONCodec.encoder().encode(releaseFixture))
check(releaseRestoredFixture==releaseFixture,"promotion serialization round trip")
print("PASS: \(checks) total assertions including promoted baseline migration")

let surfSportsFixture=Channel(id:"surf-sports",name:"Surf: Sports",number:nil,logo:false,favorite:false,program:nil,genres:[])
check(SharedSurf.mode(for:surfSportsFixture)=="sports","shared category resolution")
check(SharedSurf.channel(for:"Sports",in:[surfSportsFixture])?.id=="surf-sports","shared category does not depend on genres")
check(SharedSurf.channel(for:"Favorites",in:[surfSportsFixture])==nil,"Favorites retains app-owned rotation")
let sharedOrigin=URL(string:"https://fixture.invalid")!
check((try? SharedSurf.ticketURL("/api/surf/sports/hls/index.m3u8?ticket=fixture",origin:sharedOrigin,mode:"sports")) != nil,"valid same-origin shared ticket")
for bad in ["https://elsewhere.invalid/api/surf/sports/hls/index.m3u8?ticket=fixture","//elsewhere.invalid/api/surf/sports/hls/index.m3u8?ticket=fixture","/api/surf/all/hls/index.m3u8?ticket=fixture","/api/surf/sports/hls/index.m3u8?api_key=fixture","/api/surf/sports/hls/index.m3u8?ticket=fixture&api_key=fixture","/api/surf/sports/hls/index.m3u8?ticket="]{
    check((try? SharedSurf.ticketURL(bad,origin:sharedOrigin,mode:"sports"))==nil,"reject unsafe ticket route")
}
print("PASS: \(checks) total assertions including shared Surf routing and ticket isolation")

func continuity(_ kind:String,_ current:String?="same",_ validated:Bool=true,_ item:Bool=true,_ session:Bool=true,_ playing:Bool=true,_ paused:Bool=false,_ error:Bool=false)->Bool {
    CampaignPlaybackPolicy.reuse(kind:kind,requestedID:"same",currentID:current,validated:validated,hasItem:item,hasSession:session,playing:playing,paused:paused,hasError:error)
}
check(continuity("endurance"),"endurance preserves validated live session")
check(continuity("pause_resume"),"pause test preserves validated live session")
check(continuity("background"),"background test preserves validated live session")
check(!continuity("playback"),"cold-start playback always opens fresh")
check(!continuity("endurance","other"),"different requested channel opens fresh")
check(!continuity("endurance","same",false),"unvalidated stream cannot satisfy continuity")
check(!continuity("endurance","same",true,false),"missing player item cannot satisfy continuity")
check(!continuity("endurance","same",true,true,false),"missing session cannot satisfy continuity")
check(!continuity("endurance","same",true,true,true,false),"stalled stream cannot satisfy continuity")
check(!continuity("endurance","same",true,true,true,true,true),"paused stream cannot satisfy continuity")
check(!continuity("endurance","same",true,true,true,true,false,true),"failed stream cannot satisfy continuity")
print("PASS: \(checks) total assertions including continuity and cold-start separation")

let lanGateway=URL(string:"http://192.168.31.243:8096")!
check(SharedSurf.gatewayOrigin(for:lanGateway)?.absoluteString=="http://192.168.31.243:8097","LAN shared HLS uses same private host catalog port")
check(SharedSurf.gatewayOrigin(for:URL(string:"https://fixture.invalid")!)?.absoluteString=="https://fixture.invalid","HTTPS gateway stays same origin")
for value in ["http://203.0.113.4:8096","http://example.org:8096","http://192.168.31.243:8098","http://192.168.31.243:8096/proxy","http://user@192.168.31.243:8096","http://192.168.31.243:8096?x=1","http://172.32.1.2:8096"] {
    check(SharedSurf.gatewayOrigin(for:URL(string:value)!)==nil,"reject unsupported gateway origin")
}
for value in ["http://10.1.2.3:8096","http://172.16.1.2:8096","http://172.31.1.2:8096"] {
    check(SharedSurf.gatewayOrigin(for:URL(string:value)!)?.port==8097,"supported private LAN range")
}
print("PASS: \(checks) total assertions including LAN shared gateway isolation")

var repositorySettings=Settings()
check(repositorySettings.repositoryAddress=="https://github.com/mghazalyh/jealousdb","new settings prefill JealousDB")
repositorySettings.githubToken="fixture-token"
repositorySettings.server="http://fixture.invalid:8096"
repositorySettings.username="fixture-user"
repositorySettings.password="fixture-password"
repositorySettings.rotationMinutes=17
repositorySettings.repositoryAddress="https://github.com/fixture-owner/custom-repo"
let encodedRepositorySettings=try JSONCodec.encoder().encode(repositorySettings)
let decodedRepositorySettings=try JSONCodec.decoder().decode(Settings.self,from:encodedRepositorySettings)
check(decodedRepositorySettings.repositoryAddress==repositorySettings.repositoryAddress && decodedRepositorySettings.githubToken=="fixture-token","edited repository and token survive settings round trip")
var legacyRepositorySettings=try JSONSerialization.jsonObject(with:encodedRepositorySettings) as! [String:Any]
legacyRepositorySettings.removeValue(forKey:"githubRepositoryURL")
let migratedRepositorySettings=try JSONCodec.decoder().decode(Settings.self,from:JSONSerialization.data(withJSONObject:legacyRepositorySettings))
check(migratedRepositorySettings.repositoryAddress=="https://github.com/mghazalyh/jealousdb" && migratedRepositorySettings.server==repositorySettings.server && migratedRepositorySettings.username==repositorySettings.username && migratedRepositorySettings.password==repositorySettings.password && migratedRepositorySettings.githubToken==repositorySettings.githubToken && migratedRepositorySettings.rotationMinutes==17,"legacy settings migrate without losing credentials or preferences")
check(GitHubRepositoryAddress.apiRoot(" HTTPS://GitHub.com/Fixture-Owner/Custom-Repo/ ")=="https://api.github.com/repos/fixture-owner/custom-repo/","canonical repository URL derives fixed GitHub API host")
for invalidRepositoryAddress in ["","http://github.com/owner/repo","https://github.com.evil.invalid/owner/repo","https://github.com@evil.invalid/owner/repo","https://user@github.com/owner/repo","https://github.com:443/owner/repo","https://github.com/owner/repo?x=1","https://github.com/owner/repo#fragment","https://github.com/owner/repo/extra","https://github.com/owner/%2e%2e","https://github.com/owner/..","https://github.com/owner/repo.git"] {
    check(GitHubRepositoryAddress.apiRoot(invalidRepositoryAddress)==nil,"reject invalid repository URL")
}
print("PASS: \(checks) total assertions including editable repository settings and legacy migration")

check(Settings().controlMode == .lan,"LAN is default remote control mode")
var legacyControlSettings=Settings()
legacyControlSettings.remoteControlMode=nil
let legacyControlData=try JSONCodec.encoder().encode(legacyControlSettings)
let legacyDecoded=try JSONCodec.decoder().decode(Settings.self,from:legacyControlData)
check(legacyDecoded.controlMode == .lan,"legacy settings default to LAN")
legacyControlSettings.controlMode = .github
let githubControlData=try JSONCodec.encoder().encode(legacyControlSettings)
let githubDecoded=try JSONCodec.decoder().decode(Settings.self,from:githubControlData)
check(githubDecoded.controlMode == .github,"explicit GitHub control mode persists")
func lanRequest(_ size:Int,body:Data=Data())->Data{
    var data=Data("POST /command HTTP/1.1\r\nContent-Length: \(size)\r\n\r\n".utf8)
    data.append(body);return data
}
func lanAccepted(_ data:Data,_ expected:Int)->Bool{
    if case .request(let method,let path,let body)=LANHTTP.parse(data){return method=="POST" && path=="/command" && body.count==expected}
    return false
}
check(lanAccepted(lanRequest(192*1024,body:Data(repeating:65,count:192*1024)),192*1024),"LAN accepts full size hot patch")
check(lanAccepted(lanRequest(LANHTTP.maxBody,body:Data(repeating:65,count:LANHTTP.maxBody)),LANHTTP.maxBody),"LAN accepts four MiB request")
if case .incomplete=LANHTTP.parse(lanRequest(200000,body:Data(repeating:65,count:65536))){check(true,"LAN waits for fragmented body")}else{check(false,"LAN waits for fragmented body")}
if case .invalid(let status)=LANHTTP.parse(lanRequest(LANHTTP.maxBody+1)){check(status.hasPrefix("413"),"LAN returns 413 for oversized request")}else{check(false,"LAN returns 413 for oversized request")}
print("PASS: \(checks) total assertions including LAN control selection and request limits")

let privateDefaultsFixture=JellyfinConnectionDefaults(server:"http://fixture.invalid:8096",username:"fixture-user",password:"fixture-password")
var freshConnection=Settings()
freshConnection.applyConnectionDefaults(privateDefaultsFixture)
check(freshConnection.server==privateDefaultsFixture.server && freshConnection.username==privateDefaultsFixture.username && freshConnection.password==privateDefaultsFixture.password,"private deployment connection defaults prefill fresh settings")
freshConnection.password="saved-password"
freshConnection.applyConnectionDefaults(privateDefaultsFixture)
check(freshConnection.password=="saved-password","saved credentials are not overwritten")
var otherConnection=Settings()
otherConnection.server="https://other.invalid"
otherConnection.applyConnectionDefaults(privateDefaultsFixture)
check(otherConnection.username.isEmpty && otherConnection.password.isEmpty,"defaults never cross into a different saved server")
var alternateAccount=Settings()
alternateAccount.server=privateDefaultsFixture.server
alternateAccount.username="another-account"
alternateAccount.applyConnectionDefaults(privateDefaultsFixture)
check(alternateAccount.password.isEmpty,"defaults never cross into another account")
print("PASS: \(checks) total assertions including private deployment connection defaults")


check(SurfCommandSemantics.rotationOwner(surfing:false,shared:true)=="none","inactive rotation owner")
check(SurfCommandSemantics.rotationOwner(surfing:true,shared:true)=="server","shared rotation owner")
check(SurfCommandSemantics.rotationOwner(surfing:true,shared:false)=="device","local rotation owner")
check((try? SurfCommandSemantics.requireRotationControl(surfing:true,shared:false)) != nil,"local rotation control supported")
check((try? SurfCommandSemantics.requireRotationControl(surfing:true,shared:true)) == nil,"server rotation control rejected")
check((try? SurfCommandSemantics.requireRotationControl(surfing:false,shared:false)) == nil,"inactive rotation control rejected")
let sharedContinuityPlan=DeviceTestPlan(id:"shared-continuity",steps:[DeviceTestStep(kind:"shared_surf_continuity",channelID:nil,seconds:30,source:nil)],repeatCount:1,repeatForever:false)
check((try? sharedContinuityPlan.checked()) != nil,"shared continuity has separate test kind")
print("PASS: \(checks) assertions including honest shared rotation semantics")

var playbackAttempt=PlaybackAttemptPolicy()
playbackAttempt.segmentContainer="mp4"
playbackAttempt.forceEncode=true
playbackAttempt.videoBitrate=2_000_000
playbackAttempt.segmentSeconds=3
let rewritten=try playbackAttempt.checked().applying(to:URL(string:"http://fixture.invalid/videos/1/master.m3u8?SegmentContainer=ts&api_key=fixture&LiveStreamId=live")!)
let rewrittenQuery=URLComponents(url:rewritten,resolvingAgainstBaseURL:false)!.queryItems!
check(rewrittenQuery.filter{$0.name.lowercased()=="segmentcontainer"}.count==1,"replace format exactly once")
check(rewrittenQuery.contains(URLQueryItem(name:"SegmentContainer",value:"mp4")),"fMP4 transport policy")
check(rewrittenQuery.contains(URLQueryItem(name:"AllowVideoStreamCopy",value:"false")),"actual HLS URL forces encoding")
check(rewrittenQuery.contains(URLQueryItem(name:"api_key",value:"fixture")),"native credentials preserved")
check(rewrittenQuery.contains(URLQueryItem(name:"LiveStreamId",value:"live")),"live stream identity preserved")
playbackAttempt.maxWidth=99999
check((try? playbackAttempt.checked())==nil,"reject excessive playback dimensions")
check((try? PrivateConnectionDefaults(schemaVersion:1,server:"http://fixture.invalid",username:"fixture",password:"fixture").checked()) != nil,"private connection defaults schema")
check((try? PrivateConnectionDefaults(schemaVersion:1,server:"http://user:secret@fixture.invalid",username:"fixture",password:"fixture").checked()) == nil,"reject embedded URL credentials")
print("PASS: \(checks) assertions including ABI 2 playback and provisioning rules")

let fullscreenLayout = DynamicUILayout(schemaVersion:1,revision:"fullscreen-fixture",slots:[DynamicUISlot(location:"player.fullscreen.controls",nodes:[])],pages:[])
check((try? fullscreenLayout.checked()) != nil,"fullscreen hot patch slot supported")
check(DynamicUILayout.commands.contains("set_fullscreen_controls"),"fullscreen overlay remotely controllable")
print("PASS: \(checks) assertions including fullscreen hot patch support")

var futureLayout=DynamicUILayout(schemaVersion:1,revision:"replace-ui-fixture",slots:[DynamicUISlot(location:"player.fullscreen.transport",nodes:[])],pages:[])
futureLayout.presentation=DynamicPresentation(videoGravity:"fill",fullscreenOpacity:0.7,fullscreenMaxHeight:0.65,fullscreenInitiallyVisible:false)
check((try? futureLayout.checked()) != nil,"versioned replacement layout and presentation supported")
let futureRoundtrip=try JSONCodec.decoder().decode(DynamicUILayout.self,from:JSONCodec.encoder().encode(futureLayout))
check(futureRoundtrip==futureLayout,"presentation included in persistent revision")
futureLayout.presentation?.fullscreenOpacity=4
check((try? futureLayout.checked())==nil,"reject invalid presentation bounds")
let legacyUI=try JSONCodec.decoder().decode(DynamicUILayout.self,from:Data("{\"schemaVersion\":1,\"revision\":\"legacy\",\"slots\":[],\"pages\":[]}".utf8))
check(legacyUI.presentation==nil && (try? legacyUI.checked()) != nil,"legacy UI layout without presentation remains valid")
var prefFixture=Settings();prefFixture.password="fixture-private-password";prefFixture.githubToken="fixture-private-token"
let updatedPrefs=try PatchablePreferences.applying(["qualityMbps":"5","random":"true","surfCategory":"Sports"],to:prefFixture)
check(updatedPrefs.qualityMbps==5 && updatedPrefs.random && updatedPrefs.surfCategory=="Sports","typed multi-setting update")
check(updatedPrefs.password==prefFixture.password && updatedPrefs.githubToken==prefFixture.githubToken,"preference patch preserves private credentials")
check((try? PatchablePreferences.applying(["password":"overwrite"],to:prefFixture))==nil,"preference patch cannot alter credentials")
check((try? PatchablePreferences.applying(["qualityMbps":"7","retries":"999"],to:prefFixture))==nil,"invalid preference transaction rejected")
check(!String(describing:PatchablePreferences.snapshot(prefFixture)).contains("fixture-private"),"preference snapshot excludes secrets")
check(DynamicUILayout.commands.contains("set_preferences"),"preference command exposed to UI")
print("PASS: \(checks) assertions including replaceable UI and preferences")
let replacementExample=try JSONCodec.decoder().decode(DynamicUILayout.self,from:Data(contentsOf:URL(fileURLWithPath:"Examples/fullscreen-replacement.json")))
check((try? replacementExample.checked()) != nil,"complete fullscreen replacement example validates")
print("PASS: \(checks) total assertions")

let configFixture=try HotConfiguration.decode(Data(contentsOf:URL(fileURLWithPath:"Examples/hot-configuration.json")))
check(configFixture.revision=="conservative-configuration-example","configuration example decodes")
let plainAttempt=configFixture.attemptPolicy(channelID:"other",attempt:2,defaults:PlaybackAttemptPolicy())
check(plainAttempt.segmentContainer=="server","unmatched channel retains baseline")
let configuredAttempt=configFixture.attemptPolicy(channelID:"40abc828a6be4477ef90560e66f4b69e",attempt:2,defaults:PlaybackAttemptPolicy())
check(configuredAttempt.segmentContainer=="mp4" && configuredAttempt.videoBitrate==2000000,"scoped second-attempt configuration")
check(configFixture.attemptPolicy(channelID:"40abc828a6be4477ef90560e66f4b69e",attempt:1,defaults:PlaybackAttemptPolicy())==plainAttempt,"first attempt not modified")
let configStore=try HotConfigurationStore().activating(configFixture,expected:"native-baseline")
check(configStore.revision==configFixture.revision,"configuration CAS activation")
check((try? configStore.activating(nil,expected:"stale"))==nil,"stale configuration mutation rejected")
let resetConfig=try configStore.activating(nil,expected:configStore.revision)
check(resetConfig.revision=="native-baseline" && resetConfig.previous==configFixture,"configuration rollback state retained")
let badConfig=Data("{\"schemaVersion\":1,\"revision\":\"bad\",\"operations\":{\"systemSampleSeconds\":0}}".utf8)
check((try? HotConfiguration.decode(badConfig))==nil,"reject unsafe telemetry interval")
let typoConfig=Data("{\"schemaVersion\":1,\"revision\":\"typo\",\"operations\":{\"sampleSecondz\":15}}".utf8)
check((try? HotConfiguration.decode(typoConfig))==nil,"reject unsupported configuration field")
let partialConfig=try HotConfiguration.decode(Data("{\"schemaVersion\":1,\"revision\":\"partial\",\"runtime\":{\"retries\":3}}".utf8))
check(partialConfig.runtime?.retries==3 && partialConfig.runtime?.startupTimeout==25,"partial runtime configuration composes native defaults")
check((try? InterpreterBudget(timeoutMs:100,memoryMiB:128,maxCommands:2).checked())==nil,"interpreter ceiling enforced")
print("PASS: \(checks) assertions including hot configuration")
let stageFixture=try StagedHotConfiguration(expectedRevision:"native-baseline",candidate:configFixture).checked(against:HotConfigurationStore())
check(stageFixture.candidate==configFixture,"stage validates without activation")
check((try? stageFixture.checked(against:configStore))==nil,"stale staged file rejected")
let trialFixture=try HotConfigurationTrial(before:HotConfigurationStore(),candidateRevision:configFixture.revision,expiresAt:Date().addingTimeInterval(60)).checked()
check(!trialFixture.expired(at:Date()) && trialFixture.expired(at:Date().addingTimeInterval(61)),"trial expiry is deterministic")
let trialRoundTrip=try JSONCodec.decoder().decode(HotConfigurationTrial.self,from:JSONCodec.encoder().encode(trialFixture))
check(trialRoundTrip.id==trialFixture.id && trialRoundTrip.before.revision=="native-baseline","trial journal preserves recovery identity")
let presentationConfig=try HotConfiguration.decode(Data("{\"schemaVersion\":1,\"revision\":\"pip-example\",\"presentation\":{\"nativePlaybackControls\":true,\"pictureInPictureAllowed\":true,\"automaticPictureInPicture\":false}}".utf8))
check(presentationConfig.presentation?.nativePlaybackControls==true,"native presentation configuration restores AVKit controls")
print("PASS: \(checks) assertions including staged trials and native presentation")
let eventConfig=try HotConfiguration.decode(Data("{\"schemaVersion\":1,\"revision\":\"events\",\"operations\":{\"playerEventDiagnostics\":true,\"playerEventSampleSeconds\":10}}".utf8))
check(eventConfig.operations?.playerEventSampleSeconds==10,"configurable native event diagnostics")
check((try? OperationalConfiguration(playerEventSampleSeconds:0).checked())==nil,"reject unbounded access-log event cadence")
print("PASS: \(checks) assertions including event diagnostics")
let deviceSurfConfig=try HotConfiguration.decode(Data("{\"schemaVersion\":1,\"revision\":\"device-surf\",\"operations\":{\"surfTransport\":\"device\"}}".utf8))
check(deviceSurfConfig.operations?.surfTransport=="device","device rotation is explicit opt-in")
check((try? OperationalConfiguration(surfTransport:"unknown").checked())==nil,"unsupported rotation transport rejected")
print("PASS: \(checks) assertions including Surf transport configuration")
let manifestFixture=try ApplicationRevision(id:"application-fixture",sources:PatchSourceSet(),configuration:configFixture).checked()
check(manifestFixture.identity["configuration"]==configFixture.revision,"application manifest identifies all four components")
var unsupportedManifest=manifestFixture;unsupportedManifest.runtimeABI=99
check((try? unsupportedManifest.checked())==nil,"manifest native ABI gate")
let gatewayFixture=GatewayConfiguration(lanServerPort:8096,lanGatewayPort:8098,routePrefix:"/custom/surf",ticketField:"url",timeoutSeconds:15)
let gatewayURL=SharedSurf.gatewayOrigin(for:URL(string:"http://192.168.1.2:8096")!,configuration:gatewayFixture)
check(gatewayURL?.port==8098,"configured same-host private gateway port")
check(SharedSurf.gatewayOrigin(for:URL(string:"http://8.8.8.8:8096")!,configuration:gatewayFixture)==nil,"public HTTP gateway prohibited")
check((try? SharedSurf.ticketURL("/custom/surf/all/hls/index.m3u8?ticket=fixture",origin:gatewayURL!,mode:"all",configuration:gatewayFixture)) != nil,"configured ticket route accepted")
check((try? GatewayConfiguration(routePrefix:"/../evil").checked())==nil,"gateway traversal rejected")
let authRequest=Data("GET /status HTTP/1.1\r\nAuthorization: Bearer test-key\r\n\r\n".utf8)
check(LANHTTP.authorized(authRequest,token:"test-key"),"LAN valid bearer")
check(!LANHTTP.authorized(authRequest,token:"wrong-key"),"LAN invalid bearer")
check(!LANHTTP.authorized(Data("GET /status HTTP/1.1\r\n\r\n".utf8),token:"test-key"),"LAN absent bearer rejected")
check(LANHTTP.authorized(Data(),token:nil),"legacy LAN compatibility")
print("PASS: \(checks) assertions including application revisions, gateway and LAN authentication")

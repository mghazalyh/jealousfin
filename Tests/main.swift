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
check(conflictRejected,"decisionConflictFixture decisionFixture cannot overwrite prior approval")
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
check(decisionExpiredFixture.status=="decisionExpiredFixture" && decisionExpiredFixture.chosenAction==nil,"expiry never applies a repair")
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

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

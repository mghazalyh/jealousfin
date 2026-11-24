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

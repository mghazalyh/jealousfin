import Foundation

// Runs the actual native transport against an isolated URLProtocol fixture.
// No production Jellyfin, credentials or private device data are involved.
final class JellyfinFixture:URLProtocol {
    override class func canInit(with request:URLRequest)->Bool{true}
    override class func canonicalRequest(for request:URLRequest)->URLRequest{request}
    override func startLoading(){
        guard let url=request.url else{return}
        var status=200
        var object:[String:Any]=[:]
        switch url.path {
        case "/fixture/auth":
            if request.httpMethod=="POST" && request.value(forHTTPHeaderField:"X-Emby-Authorization") != nil {
                object=["Session":["Token":"fixture-token","UserID":"fixture-user"]]
            }else{status=401}
        case "/fixture/catalogue":
            if request.value(forHTTPHeaderField:"X-Emby-Authorization")?.contains("fixture-token")==true &&
                URLComponents(url:url,resolvingAgainstBaseURL:false)?.queryItems?.contains(where:{$0.name=="Limit" && $0.value=="200"})==true{
                object=["Channels":[["Key":"channel-a","Title":"Fixture channel"]]]
            }else{status=403}
        case "/api/surf/all/hls/ticket","/api/surf/movies-series/hls/ticket","/api/surf/sports/hls/ticket":
            if request.httpMethod=="POST" && request.value(forHTTPHeaderField:"X-Emby-Token")=="fixture-token" {
                object=["url":url.path.replacingOccurrences(of:"ticket",with:"index.m3u8")+"?ticket=fixture-ticket"]
            }else{status=401}
        case "/Users/AuthenticateByName":object=["AccessToken":"fixture-token","User":["Id":"fixture-user"]]
        case "/LiveTv/Channels","/Jealousfin/Policy","/Jealousfin/Policy/Event":
            let row:[String:Any]=["lastAttempt":0,"hiatusUntil":0,"banned":false,"failures":0,"lastFailure":0,"reason":"","successes":0,"approvedAt":0,"lastSuccess":0]
            let snapshot:[String:Any]=["revision":"sqlite-policy-v2","channels":["channel-b":row],"penalty":["baseDays":3,"banAfter":8]]
            if url.path=="/LiveTv/Channels"{
                if URLComponents(url:url,resolvingAgainstBaseURL:false)?.queryItems?.contains(where:{$0.name=="JealousfinIncludePolicy" && $0.value=="true"})==true{
                    let now=Date().timeIntervalSince1970
                    let episode:[String:Any]=["title":"Beverly Hills, 90210","source":"provider-catalogue","observedAt":now,"year":1990,"episodeTitle":"Slipping Away","seasonNumber":9,"episodeNumber":17,"seasonYear":1998,"episodeSource":"pluto-xmltv","episodeObservedAt":now]
                    let current:[String:Any]=["Id":"program-b","Name":"S09E17: Slipping Away (1998)","ChannelId":"channel-b","JealousfinProgramme":episode]
                    object=["Items":[["Id":"channel-b","Name":"Policy fixture","CurrentProgram":current]],"JealousfinPolicy":snapshot]
                }else{status=400}
            }else{object=snapshot}
        case "/Items/channel-b/PlaybackInfo":
            status=409;object=["JealousfinPolicyError":"excluded"]
        case "/System/Info/Public":object=["ServerName":"Fixture"]
        case "/Items/channel-a/PlaybackInfo":
            object=["PlaySessionId":"fixture-play","MediaSources":[["Id":"fixture-source","LiveStreamId":"fixture-live","TranscodingUrl":"/original.m3u8?api_key=old"]]]
        case "/Sessions/Playing","/Sessions/Playing/Progress","/Sessions/Playing/Stopped","/LiveStreams/Close":status=204
        default:status=404
        }
        let data=(try? JSONSerialization.data(withJSONObject:object)) ?? Data()
        let response=HTTPURLResponse(url:url,statusCode:status,httpVersion:"HTTP/1.1",headerFields:["Content-Type":"application/json"])!
        client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed)
        if status != 204{client?.urlProtocol(self,didLoad:data)}
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading(){}
}

final class CatalogueStartupFixture:URLProtocol {
    private static let lock=NSLock()
    private static var attempts:[String:Int]=[:]
    static func count(_ key:String)->Int{lock.lock();defer{lock.unlock()};return attempts[key,default:0]}
    override class func canInit(with request:URLRequest)->Bool{true}
    override class func canonicalRequest(for request:URLRequest)->URLRequest{request}
    override func startLoading(){
        guard let url=request.url else{return}
        let host=url.host ?? ""
        let key=host+url.path
        Self.lock.lock();Self.attempts[key,default:0]+=1;let attempt=Self.attempts[key]!;Self.lock.unlock()
        var code=200
        var object:[String:Any]=[:]
        if url.path=="/Users/AuthenticateByName" {
            object=["AccessToken":"fixture-token","User":["Id":"fixture-user"]]
            if host=="bad-auth.invalid"{code=401}
        }else if url.path=="/LiveTv/Channels" {
            let row:[String:Any]=["lastAttempt":0,"hiatusUntil":0,"banned":false,"failures":0,"lastFailure":0,"reason":"","successes":0]
            object=["Items":[["Id":"channel-a","Name":"Ready channel"]],
                    "JealousfinPolicy":["revision":"sqlite-policy-v2","channels":["channel-a":row]]]
            if host=="recover.invalid" && attempt==1{code=503;object=["JealousfinPolicyError":"unavailable"]}
            if host=="recover.invalid" && attempt==2{object=["Items":[]]}
            if host=="empty.invalid"{object=["Items": []]}
            if host=="missing-policy.invalid"{object=["Items":[["Id":"channel-a","Name":"Ready channel"]]]}
            if host=="mismatch.invalid"{object["Items"]=[["Id":"other","Name":"Other"]]}
            if host=="malformed.invalid"{object["Items"]=[["Id":"channel-a"]]}
            if host=="duplicate.invalid"{object["Items"]=[["Id":"channel-a","Name":"One"],["Id":"channel-a","Name":"Two"]]}
            if host=="expired.invalid"{code=401}
        }else{code=404}
        let data=(try? JSONSerialization.data(withJSONObject:object)) ?? Data()
        let response=HTTPURLResponse(url:url,statusCode:code,httpVersion:"HTTP/1.1",headerFields:["Content-Type":"application/json"])!
        client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed)
        client?.urlProtocol(self,didLoad:data);client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading(){}
}

// Deterministic concurrent-commit fixture: no production repository writes.
final class TorrioConflictFixture:URLProtocol {
    private static let lock=NSLock()
    private static var counts:[String:Int]=[:]
    static func reset(){lock.lock();defer{lock.unlock()};counts=[:]}
    static func count(_ key:String)->Int{lock.lock();defer{lock.unlock()};return counts[key,default:0]}
    override class func canInit(with request:URLRequest)->Bool{true}
    override class func canonicalRequest(for request:URLRequest)->URLRequest{request}
    override func startLoading(){
        guard let url=request.url else{return}
        let path=url.lastPathComponent
        let method=request.httpMethod ?? "GET"
        let key=method+" "+path
        Self.lock.lock();Self.counts[key,default:0]+=1;let n=Self.counts[key]!;Self.lock.unlock()
        var status=200
        var object:[String:Any]=[:]
        func blob(_ sha:String,_ data:Data){object=["sha":sha,"content":data.base64EncodedString()]}
        if method=="GET" {
            switch path {
            case "route-probe.json":
                if url.scheme=="https",url.host=="api.github.com",url.path=="/repos/fixture-owner/fixture-repo/contents/jealousfin/route-probe.json",
                   URLComponents(url:url,resolvingAgainstBaseURL:false)?.queryItems==[URLQueryItem(name:"ref",value:"main")],
                   request.value(forHTTPHeaderField:"Authorization")=="Bearer fixture" {blob("alternate-route",Data("ok".utf8))}
                else{status=403}
            case "snapshot.json":blob(n==1 ? "old" : "new",Data("old snapshot".utf8))
            case "cas.json":blob("changed",Data("concurrent value".utf8))
            case "immutable.json":
                if n==1{status=404}else{blob("existing",Data("receipt".utf8))}
            case "collision.json":
                if n==1{status=404}else{blob("existing",Data("different receipt".utf8))}
            case "channel-health-v1.json":
                let event=HealthEvent(id:n==1 ? "remote-one":"remote-two",channelID:"a",channelName:"A",action:"success",reason:"fixture")
                var ledger=HealthLedger()
                ledger.events=[event]
                if n>1{ledger.events.append(HealthEvent(id:"remote-one",channelID:"a",channelName:"A",action:"success",reason:"fixture"))}
                blob(n==1 ? "old":"new",(try? JSONCodec.encoder().encode(ledger)) ?? Data())
            default:status=404
            }
        }else if method=="PUT" {
            if path=="auth.json"{status=401}
            else if path=="exhausted.json"{status=409}
            else{status=n==1 ? 409:200}
        }else{status=405}
        let data=(try? JSONSerialization.data(withJSONObject:object)) ?? Data()
        let response=HTTPURLResponse(url:url,statusCode:status,httpVersion:"HTTP/1.1",headerFields:["Content-Type":"application/json"])!
        client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed)
        client?.urlProtocol(self,didLoad:data);client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading(){}
}

@MainActor enum HotPatchIntegration{
    static func require(_ value:Bool,_ message:String)throws{
        guard value else{throw AppError.message("Integration failed: "+message)}
        print("PASS: hot patch "+message)
    }
    static func run(engine:ScriptEngine) async throws{
        TorrioConflictFixture.reset()
        let dbConfig=URLSessionConfiguration.ephemeral;dbConfig.protocolClasses=[TorrioConflictFixture.self]
        let conflictDB=TorrioDB(address:Settings().repositoryAddress,transport:URLSession(configuration:dbConfig));conflictDB.token="fixture"
        try require(conflictDB.base=="https://api.github.com/repos/mghazalyh/jealousdb/contents/jealousfin/","prefilled JealousDB route")
        let alternateDB=TorrioDB(address:"https://github.com/fixture-owner/fixture-repo",transport:URLSession(configuration:dbConfig))
        alternateDB.token="fixture"
        let routeProbe=try await alternateDB.read("route-probe.json")
        try require(routeProbe?.sha=="alternate-route","native authenticated request uses edited repository")
        let invalidDB=TorrioDB(address:"https://example.invalid/fixture-owner/fixture-repo",transport:URLSession(configuration:dbConfig))
        invalidDB.token="fixture"
        var invalidRejected=false
        do{_ = try await invalidDB.read("invalid-route.json")}catch{invalidRejected=true}
        try require(invalidRejected && TorrioConflictFixture.count("GET invalid-route.json")==0,"invalid repository never sends a token")
        alternateDB.enabled=false
        var offlineRejected=false
        do{_ = try await alternateDB.read("offline-probe.json")}catch{offlineRejected=true}
        try require(offlineRejected && TorrioConflictFixture.count("GET offline-probe.json")==0,"LAN mode blocks database transport before network request")
        let receipt=Data("receipt".utf8)
        try await conflictDB.replaceDeviceSnapshot("snapshot.json",data:Data("new snapshot".utf8))
        try require(TorrioConflictFixture.count("PUT snapshot.json")==2 && TorrioConflictFixture.count("GET snapshot.json")==2,"TorrioDB heartbeat conflict re-reads and retries")
        try await conflictDB.immutable("immutable.json",data:receipt)
        try require(TorrioConflictFixture.count("PUT immutable.json")==1,"TorrioDB identical concurrent receipt is idempotent")
        var conflictRejected=false
        do{try await conflictDB.immutable("collision.json",data:receipt)}catch{conflictRejected=true}
        try require(conflictRejected && TorrioConflictFixture.count("PUT collision.json")==1,"TorrioDB immutable collision never overwrites")
        conflictRejected=false
        do{try await conflictDB.put("cas.json",data:receipt,sha:"old")}catch{conflictRejected=true}
        try require(conflictRejected && TorrioConflictFixture.count("PUT cas.json")==1,"TorrioDB compare-and-swap preserves concurrent baseline")
        var local=HealthLedger()
        local.events=[HealthEvent(id:"local",channelID:"a",channelName:"A",action:"success",reason:"fixture")]
        let merged=try await conflictDB.merge(local)
        try require(Set(merged.events.map(\.id))==Set(["local","remote-one","remote-two"]) && TorrioConflictFixture.count("PUT channel-health-v1.json")==2,"TorrioDB ledger conflict merges fresh remote events")
        conflictRejected=false
        do{try await conflictDB.put("auth.json",data:receipt)}catch{conflictRejected=true}
        try require(conflictRejected && TorrioConflictFixture.count("PUT auth.json")==1,"TorrioDB unauthorized write is not retried")
        conflictRejected=false
        do{try await conflictDB.replaceDeviceSnapshot("exhausted.json",data:receipt)}catch{conflictRejected=true}
        try require(conflictRejected && TorrioConflictFixture.count("PUT exhausted.json")==4,"TorrioDB conflict retry is bounded")
        let root=FileManager.default.temporaryDirectory.appendingPathComponent("jealousfin-tests-"+UUID().uuidString)
        defer{try? FileManager.default.removeItem(at:root)}
        let disk=Disk(root:root)
        let settings=Settings()
        let first="var hooks={configure:function(p){p.retries=3;return p;},select:function(p){return p.candidates[0].id;},tick:function(p){return {state:{count:String(Number(p.state.count||0)+1)},actions:[{action:'get_status'}],nextSeconds:10};}};"
        let second="var hooks={configure:function(p){p.retries=4;return p;}};"
        let runtime=EngineeringRuntime(disk:disk,engine:engine)
        let p1=try await runtime.install(id:"one",source:first,settings:settings)
        try require(p1.retries==3 && runtime.activeID=="one","actual runtime installation")
        let channel=Channel(id:"channel-a",name:"Fixture",number:nil,logo:false,favorite:false,program:nil,genres:[])
        let selected=try await runtime.select(candidates:[channel],currentID:nil)
        try require(selected=="channel-a","channel selection hook")
        let decision=try await runtime.decision(snapshot:[:],state:["count":"2"],events:[],receipts:[])
        try require(decision?.state?["count"]=="3" && decision?.actions?.first?.action=="get_status","event decision hook")
        let p2=try await runtime.install(id:"two",source:second,settings:settings)
        try require(p2.retries==4 && runtime.store.previous?.id=="one","live module replacement")
        var rejected=false
        do{_ = try await runtime.install(id:"bad",source:"var hooks={configure:function(p){p.retries=999;return p;}};",settings:settings)}
        catch{rejected=true}
        try require(rejected && runtime.activeID=="two" && runtime.store.previous?.id=="one","rejected patch preserves rollback target")
        let reloaded=EngineeringRuntime(disk:Disk(root:root),engine:engine)
        let boot=try await reloaded.boot(settings:settings)
        try require(boot.retries==4 && reloaded.activeID=="two","reload from durable storage")
        let rolled=try await reloaded.rollback(settings:settings)
        try require(rolled.retries==3 && reloaded.activeID=="one","actual runtime rollback")
        let interrupted=RuntimePatchStore(active:RuntimePatch(id:"interrupted",source:second),previous:RuntimePatch(id:"one",source:first),activationPending:true)
        try require(disk.save(interrupted,"runtime-patch.json"),"write interruption fixture")
        let recovered=EngineeringRuntime(disk:Disk(root:root),engine:engine)
        let recovery=try await recovered.boot(settings:settings)
        try require(recovery.retries==3 && recovered.activeID=="one","interrupted activation recovers previous module")
        let reset=try recovered.reset(settings:settings)
        try require(reset.retries==settings.retries && recovered.activeID=="native-baseline","reset actual module")

        let source="""
        var hooks={
            configure:function(p){p.authorizationHeader="both";p.tokenPath="Session.Token";p.userIDPath="Session.UserID";return p;},
            request:function(p){if(p.operation==="authenticate"){p.path="fixture/auth";}if(p.operation==="channels"){p.path="fixture/catalogue";p.query.Limit="200";}return p;},
            response:function(p){if(p.operation==="channels"){return {Items:p.response.Channels.map(function(c){return {Id:c.Key,Name:c.Title};})};}return p.response;},
            stream:function(p){p.path="/patched.m3u8";p.method="Transcode";return p;}
        };
        """
        let adapter=try await JellyfinAPIAdapter.load(JellyfinAdapterRevision(id:"api-test",source:source),engine:engine)
        let config=URLSessionConfiguration.ephemeral
        config.protocolClasses=[JellyfinFixture.self]
        let client=Jellyfin(deviceID:"fixture",transport:URLSession(configuration:config))
        client.adapter=adapter
        var fixtureSettings=Settings();fixtureSettings.server="http://fixture.invalid:8096";fixtureSettings.username="fixture";fixtureSettings.password="test-only"
        try await client.connect(fixtureSettings)
        try require(client.connected && client.userID=="fixture-user","patched authentication through HTTP transport")
        let catalogue=try await client.channels()
        try require(catalogue.count==1 && catalogue[0].id=="channel-a","patched request and response through HTTP transport")
        let stream=try await client.stream(catalogue[0],mbps:5)
        try require(stream.url.path=="/patched.m3u8" && stream.method=="Transcode" && URLComponents(url:stream.url,resolvingAgainstBaseURL:false)?.queryItems?.first(where:{$0.name=="api_key"})?.value=="fixture-token","patched playback URL and native credential injection")
        await client.close(stream,position:0)
        try require(client.inFlight==0,"session cleanup drains transport")
        let startupConfig=URLSessionConfiguration.ephemeral
        startupConfig.protocolClasses=[CatalogueStartupFixture.self]
        let startup=Jellyfin(deviceID:"startup",transport:URLSession(configuration:startupConfig))
        var startupSettings=fixtureSettings;startupSettings.server="http://recover.invalid:8096"
        var exposedPartial=false
        startup.log={_,code,_ in if code=="CATALOGUE_STARTUP_RETRY" && startup.connected{exposedPartial=true}}
        let recoveredCatalogue=try await startup.connectCatalogue(startupSettings,retryDelays:[0,0])
        try require(recoveredCatalogue.count==1 && startup.connected && startup.gatewayHealth?.channels.count==1 && !exposedPartial,"atomic startup retries transient policy and empty catalogue without exposing authentication")
        try require(CatalogueStartupFixture.count("recover.invalid/Users/AuthenticateByName")==1 && CatalogueStartupFixture.count("recover.invalid/LiveTv/Channels")==3,"catalogue retry reuses authentication and is bounded")
        startup.log=nil
        for host in ["empty.invalid","mismatch.invalid","malformed.invalid","duplicate.invalid","expired.invalid","bad-auth.invalid","missing-policy.invalid"] {
            startupSettings.server="http://"+host+(host=="missing-policy.invalid" ? ":18796":":8096")
            var failed=false
            do{_ = try await startup.connectCatalogue(startupSettings,retryDelays:[0,0])}catch{failed=true}
            try require(failed && !startup.connected && startup.gatewayHealth==nil,"failed startup cannot expose stale credentials or policy: "+host)
            let reads=CatalogueStartupFixture.count(host+"/LiveTv/Channels")
            try require(reads==(host=="bad-auth.invalid" ? 0:host=="expired.invalid" ? 1:3),"bounded startup failure: "+host)
        }
        startupSettings.server="http://recover.invalid:8096"
        var cancelled=false
        do{_ = try await startup.connectCatalogue(startupSettings,stillCurrent:{false},retryDelays:[0])}catch is CancellationError{cancelled=true}
        try require(cancelled && !startup.connected && startup.gatewayHealth==nil,"settings change cancels staged catalogue")
        var changedEpoch=false
        do{
            _ = try await startup.connectCatalogue(startupSettings,stillCurrent:{
                if !changedEpoch{changedEpoch=true;startup.invalidateConnection()}
                return true
            },retryDelays:[0])
            throw AppError.message("stale connection adopted")
        }catch is CancellationError{}
        try require(changedEpoch && !startup.connected && startup.gatewayHealth==nil,"same-account connection epoch change rejects staged response")

        let nativePolicyClient=Jellyfin(deviceID:"policy-fixture",transport:URLSession(configuration:config))
        var policySettings=fixtureSettings;policySettings.server="http://fixture.invalid:18796"
        try await nativePolicyClient.connect(policySettings)
        let policyCatalogue=try await nativePolicyClient.channels()
        try require(policyCatalogue.count==1 && nativePolicyClient.gatewayHealth?.revision=="sqlite-policy-v2","native full catalogue and gateway health negotiation")
        try require(policyCatalogue[0].program?.name=="S09E17: Slipping Away (1998)" && policyCatalogue[0].program?.evidence?.episodeTitle=="Slipping Away" && policyCatalogue[0].program?.evidence?.seasonYear==1998,"gateway programme title, episode and season year pass through with policy intact")
        try await nativePolicyClient.refreshGatewayHealth()
        try require(nativePolicyClient.gatewayHealth?.penalty?.baseDays==3,"native gateway refresh")
        var excluded=false
        do{_ = try await nativePolicyClient.stream(policyCatalogue[0],mbps:5)}catch GatewayPolicyError.excluded{excluded=true}
        try require(excluded,"typed gateway rejection is not a new playback failure")
        try await nativePolicyClient.submitGatewayHealth(channelID:"channel-b",trace:"fixture-trace",action:"failure",reason:"fixture")
        try require(nativePolicyClient.gatewayHealth?.channels.count==1,"native authenticated policy event transport")
        let healthy=await client.healthy()
        try require(healthy,"patched server health probe")
        var secureSettings=fixtureSettings;secureSettings.server="https://fixture.invalid"
        let gateway=Jellyfin(deviceID:"gateway",transport:URLSession(configuration:config))
        gateway.adapter=adapter
        try await gateway.connect(secureSettings)
        try require(gateway.sharedSurfAvailable,"shared gateway capability detected")
        let surf=Channel(id:"surf-fixture",name:"Surf: Sports",number:nil,logo:false,favorite:false,program:nil,genres:[])
        let shared=try await gateway.stream(surf,mbps:5)
        try require(shared.isSharedSurf && shared.liveStreamID==nil && shared.url.path=="/api/surf/sports/hls/index.m3u8","shared Surf skips PlaybackInfo and adapter stream rewrite")
        try require(URLComponents(url:shared.url,resolvingAgainstBaseURL:false)?.queryItems==[URLQueryItem(name:"ticket",value:"fixture-ticket")],"shared ticket stays native and excludes Jellyfin token")
        await gateway.close(shared,position:0)
        try require(gateway.inFlight==0,"shared close does not allocate or close Jellyfin live stream")
        let eventModule=try PlatformModule.decode(Data("""
        {"id":"telemetry","revision":"v1","hooks":["onTelemetrySample"],"source":"var hooks={onTelemetrySample:function(p){return {state:{count:String(Number(p.state.count||0)+1)},actions:[{action:'get_status'}]};}};","shadow":true}
        """.utf8))
        let input:[String:Any]=["hook":"onTelemetrySample","state":["count":"4"],"snapshot":[:],"event":[:],"data":[:]]
        let moduleDecision=try await PlatformEvaluator.evaluate(eventModule,input:input,hostBudget:InterpreterBudget())
        try require(moduleDecision.state?["count"]=="5" && moduleDecision.actions?.first?.action=="get_status","actual event hook evaluation")
        var invalidModule=eventModule
        invalidModule.source="var hooks={onTelemetrySample:function(p){api.command('get_status',{});return {};}};"
        var platformRejected=false
        do{_ = try await PlatformEvaluator.evaluate(invalidModule,input:input,hostBudget:InterpreterBudget())}catch{platformRejected=true}
        try require(platformRejected,"event hook cannot execute commands directly")
        let catalogueAdapter=try await JellyfinAPIAdapter.load(JellyfinAdapterRevision(id:"catalogue-only",source:"var hooks={response:function(p){if(p.operation==='channels'){p.response.Items.reverse();}return p.response;},stream:function(p){return {path:'/changed',method:'Transcode'};}};",scope:"catalogue"),engine:engine)
        let sorted=try await catalogueAdapter.response(operation:"channels",response:["Items":[["Id":"a"],["Id":"b"]],"JealousfinPolicyRevision":"sqlite-policy-v1"])
        try require((sorted["Items"] as? [[String:String]])?.first?["Id"]=="b","scoped adapter transforms catalogue")
        let unchanged=try await catalogueAdapter.stream(path:"/original",method:"DirectPlay",itemID:"a")
        try require(unchanged.0=="/original" && unchanged.1=="DirectPlay","catalogue adapter cannot change active stream")
        let policyAdapter=try await JellyfinAPIAdapter.load(JellyfinAdapterRevision(id:"policy-tamper",source:"var hooks={response:function(p){if(p.response.JealousfinPolicy){delete p.response.JealousfinPolicy;}return p.response;}};",scope:"catalogue"),engine:engine)
        platformRejected=false
        do{_ = try await policyAdapter.response(operation:"channels",response:["Items":[],"JealousfinPolicy":["revision":"sqlite-policy-v2"]])}catch{platformRejected=true}
        try require(platformRejected,"catalogue adapter cannot suppress authoritative policy")
        let selectorRuntime=EngineeringRuntime(disk:Disk(root:root.appendingPathComponent("selector")),engine:engine)
        _ = try await selectorRuntime.install(id:"defer-selector",source:"var hooks={selectDecision:function(p){return {outcome:'defer',retrySeconds:12};}};",settings:settings)
        let deferred=try await selectorRuntime.selection(candidates:[channel],currentID:nil)
        try require(deferred.outcome=="defer" && deferred.retrySeconds==12,"explicit native selector defer")
        let layout=try JSONCodec.decoder().decode(DynamicUILayout.self,from:Data(contentsOf:URL(fileURLWithPath:"Examples/dynamic-ui.json"))).checked()
        let baseline=ReleaseBaseline(revision:"release-one",sources:PatchSourceSet(runtime:PatchSource(id:"one",source:first),jellyfinAPI:PatchSource(id:"api-test",source:source),ui:layout))
        let installDisk=Disk(root:root.appendingPathComponent("fresh"))
        try BaselineInstaller.install(baseline,disk:installDisk)
        try require(installDisk.load(RuntimePatchStore.self,"runtime-patch.json")?.active?.id=="one" && installDisk.load(JellyfinAdapterStore.self,"jellyfin-api.json")?.active?.id=="api-test" && installDisk.load(DynamicUIStore.self,"dynamic-ui.json")?.active?.revision==layout.revision,"real clean-install baseline migration")
        let newer=ReleaseBaseline(revision:"release-two",sources:PatchSourceSet(runtime:PatchSource(id:"two",source:second),ui:layout))
        try BaselineInstaller.install(newer,disk:installDisk)
        try require(installDisk.load(RuntimePatchStore.self,"runtime-patch.json")?.active?.id=="two" && installDisk.load(JellyfinAdapterStore.self,"jellyfin-api.json")?.active==nil,"real upgrade applies new module and removes retired API")
        let override=RuntimePatchStore(active:RuntimePatch(id:"local",source:first))
        try require(installDisk.save(override,"runtime-patch.json"),"write local override fixture")
        try BaselineInstaller.install(ReleaseBaseline(revision:"release-three",sources:PatchSourceSet()),disk:installDisk)
        try require(installDisk.load(RuntimePatchStore.self,"runtime-patch.json")?.active?.id=="local","real upgrade preserves independent local override")

        // Release epochs reset only patch-owned files, before recovery.
        let epochDisk=Disk(root:root.appendingPathComponent("release-epoch"))
        let firstEpoch=PatchReleaseEpoch(version:"0.1.13",build:"1021")
        let secondEpoch=PatchReleaseEpoch(version:"0.1.13",build:"1022")
        let preserved=["settings.json","health.json","surf-rotation.json","device.json","channels.json","receipt-fixture.json","event-fixture.json","batch-fixture.json","startup-profiles.json"]
        let obsolete=["runtime-patch.json","jellyfin-api.json","dynamic-ui.json","hot-config.json","hot-config-staged.json","hot-config-trial.json","patch-restore.json","last-patch-archive.json","application-trial.json","application-active.json","installed-baseline.json","module-checkpoint.json","native-safe-mode.json","native-safe-reset-complete.json","device-campaign.json","decision-journal.json","patch-version-old.json","patch-archive-old.json","hot-config-version-old.json","hot-config-recovery-old.json","application-revision-old.json","platform-modules.json","platform-deferred-api.json"]
        for name in preserved+obsolete{
            try require(epochDisk.save("fixture",name),"seed release fixture")
        }
        let recoveryFolder=epochDisk.root.appendingPathComponent("native-recovery-old",isDirectory:true)
        try FileManager.default.createDirectory(at:recoveryFolder,withIntermediateDirectories:true)
        let didReset=try PatchReleaseReset.prepare(firstEpoch,disk:epochDisk)
        try require(didReset,"legacy installation starts a clean patch epoch")
        try require(obsolete.allSatisfy{!FileManager.default.fileExists(atPath:epochDisk.root.appendingPathComponent($0).path)},"all legacy patch state removed")
        try require(!FileManager.default.fileExists(atPath:recoveryFolder.path),"obsolete native recovery directory removed")
        try require(preserved.allSatisfy{epochDisk.load(String.self,$0)=="fixture"},"settings, health, rotation and diagnostics retained")
        try require(epochDisk.save("same-build-patch","patch-version-new.json"),"seed same build history")
        let sameReset=try PatchReleaseReset.prepare(firstEpoch,disk:epochDisk)
        try require(!sameReset && epochDisk.load(String.self,"patch-version-new.json")=="same-build-patch","ordinary relaunch preserves current epoch")
        let upgradeReset=try PatchReleaseReset.prepare(secondEpoch,disk:epochDisk)
        try require(upgradeReset && epochDisk.load(String.self,"patch-version-new.json")==nil,"new build clears previous patch history")
        try require(preserved.allSatisfy{epochDisk.load(String.self,$0)=="fixture"},"upgrade preserves non-patch data")
        try require(firstEpoch.archivePath(deviceID:"fixture",id:"old") != secondEpoch.archivePath(deviceID:"fixture",id:"old"),"remote archive path isolated by release")
        let versionOnly=PatchReleaseEpoch(version:"0.1.14",build:"1022")
        try require(versionOnly.namespace != secondEpoch.namespace,"marketing version also scopes archives")
        let invalidEpoch=PatchReleaseEpoch(version:"",build:"1022")
        var rejectedEpoch=false
        do{_ = try PatchReleaseReset.prepare(invalidEpoch,disk:epochDisk)}catch{rejectedEpoch=true}
        try require(rejectedEpoch && epochDisk.load(PatchReleaseEpoch.self,PatchReleaseReset.marker)==secondEpoch,"invalid metadata does not reset data")
        try BaselineInstaller.install(ReleaseBaseline.empty,disk:epochDisk)
        try require(epochDisk.load(RuntimePatchStore.self,"runtime-patch.json")?.active==nil && epochDisk.load(DynamicUIStore.self,"dynamic-ui.json")?.active==nil,"new IPA boots native baseline")
        print("PASS: release-scoped patch reset, same-build retention, legacy migration and remote isolation")

        let versionDisk=Disk(root:root.appendingPathComponent("versions"))
        let oldVersion=PatchArchive(deviceID:"fixture",reason:"checkpoint",build:"1",sources:baseline.sources,label:"Working playback",knownGood:true)
        let newVersion=PatchArchive(deviceID:"fixture",reason:"runtime-activated",build:"1",sources:newer.sources,parentID:oldVersion.id)
        try PatchVersionStore.retain(oldVersion,disk:versionDisk)
        try PatchVersionStore.retain(newVersion,disk:versionDisk)
        try PatchVersionStore.retain(oldVersion,disk:versionDisk)
        try require(PatchVersionStore.history(disk:versionDisk).count==2,"immutable history survives multiple revisions")
        var conflicting=oldVersion;conflicting.reason="edited"
        var collision=false
        do{try PatchVersionStore.retain(conflicting,disk:versionDisk)}catch{collision=true}
        try require(collision,"immutable history rejects overwrite")
        let history=PatchVersionStore.history(disk:Disk(root:versionDisk.root))
        try require(history.contains(where:{$0.id==oldVersion.id && $0.knownGood && $0.label=="Working playback"}),"known-good checkpoint survives reload")
        guard let archived=PatchVersionStore.load(oldVersion.id,disk:versionDisk) else{throw AppError.message("Missing old version")}
        try require(archived.sources==baseline.sources,"older complete source set recoverable")
        let comparison=PatchVersionComparison(from:oldVersion,to:newVersion)
        try require(comparison.runtimeChanged && comparison.apiChanged && !comparison.uiChanged,"source comparison detects changed components")
        let onlyRuntime=PatchVersionScope.runtime.merge(current:newVersion.sources,target:oldVersion.sources)
        try require(onlyRuntime.runtime==oldVersion.sources.runtime && onlyRuntime.jellyfinAPI==newVersion.sources.jellyfinAPI && onlyRuntime.ui==newVersion.sources.ui,"component restore preserves unrelated components")
        let before=PatchRestoreSnapshot(runtime:RuntimePatchStore(active:RuntimePatch(id:"two",source:second)),api:JellyfinAdapterStore(),ui:DynamicUIStore(active:layout,values:["minutes":"10"]))
        let after=PatchRestoreSnapshot(runtime:RuntimePatchStore(active:RuntimePatch(id:"one",source:first)),api:JellyfinAdapterStore(active:JellyfinAdapterRevision(id:"api-test",source:source)),ui:DynamicUIStore(active:layout,values:["minutes":"10"]))
        try PatchVersionStore.write(before,disk:versionDisk)
        try PatchVersionStore.commit(before:before,after:after,targetID:oldVersion.id,scope:.all,disk:versionDisk)
        let restoredRuntime=EngineeringRuntime(disk:Disk(root:versionDisk.root),engine:engine)
        let restoredPolicy=try await restoredRuntime.boot(settings:settings)
        try require(restoredPolicy.retries==3 && versionDisk.load(JellyfinAdapterStore.self,"jellyfin-api.json")?.active?.id=="api-test","actual older source set restored and executable")
        let completeRecovery=try PatchVersionStore.recover(disk:versionDisk)
        try require(!completeRecovery,"committed restore survives relaunch")
        let journal=PatchRestoreJournal(before:before,targetID:oldVersion.id,scope:.all)
        try require(versionDisk.save(journal,"patch-restore.json"),"write interrupted restoration fixture")
        // Simulate a process dying after only its first store was replaced.
        try require(versionDisk.save(after.runtime,"runtime-patch.json"),"write partial restoration fixture")
        let recoveredRestore=try PatchVersionStore.recover(disk:Disk(root:versionDisk.root))
        try require(recoveredRestore && versionDisk.load(RuntimePatchStore.self,"runtime-patch.json")?.active?.id=="two" && versionDisk.load(JellyfinAdapterStore.self,"jellyfin-api.json")?.active==nil && versionDisk.load(DynamicUIStore.self,"dynamic-ui.json")?.values["minutes"]=="10","interrupted restoration recovers all previous stores")
    }
}

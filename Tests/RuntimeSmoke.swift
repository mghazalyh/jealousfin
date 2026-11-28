import Foundation
import Network

@main struct RuntimeSmoke {
    @MainActor static func main() async throws {
        let engine=ScriptEngine()
        let (value,commands)=try await engine.evaluateAsync("api.command('get_status', {}); api.state.answer + 1",snapshot:["answer":41])
        precondition(value=="42" && commands.count==1 && commands[0].action=="get_status")
        let start=ProcessInfo.processInfo.systemUptime
        var interrupted=false
        do{_ = try await engine.evaluateAsync("while (true) {}",snapshot:[:])}
        catch{interrupted=error.localizedDescription.lowercased().contains("deadline")}
        precondition(interrupted && ProcessInfo.processInfo.systemUptime-start<10)
        let (after,_)=try await engine.evaluateAsync("6*7",snapshot:[:])
        precondition(after=="42")
        var exception=false
        do{_ = try await engine.evaluateAsync("throw new Error('fixture')",snapshot:[:])}
        catch{exception=true}
        precondition(exception)
        let adapterSource="""
        var hooks = {
          configure: function(p) { p.timeoutSeconds=35; p.authorizationHeader="both"; return p; },
          request: function(p) { if(p.operation==="channels"){p.query.Limit="200";} return p; },
          response: function(p) { if(p.operation==="channels"){return {Items:p.response.Channels};} return p.response; },
          stream: function(p) {p.method="Transcode";return p;}
        };
        """
        let adapter=try await JellyfinAPIAdapter.load(JellyfinAdapterRevision(id:"fixture-api",source:adapterSource),engine:engine)
        precondition(adapter.spec.timeoutSeconds==35)
        let request=try await adapter.request(operation:"channels",path:"LiveTv/Channels",method:"GET",query:[:],body:nil)
        precondition(request.query["Limit"]=="200")
        let login=try await adapter.request(operation:"authenticate",path:"Users/AuthenticateByName",method:"POST",query:[:],body:["Username":"fixture-user","Pw":"fixture-password"])
        precondition(login.body?["Pw"] as? String=="fixture-password")
        let response=try await adapter.response(operation:"channels",response:["Channels":[["Id":"fixture"]]])
        precondition((response["Items"] as? [[String:String]])?.first?["Id"]=="fixture")
        let stream=try await adapter.stream(path:"/live.m3u8",method:"DirectStream",itemID:"fixture")
        precondition(stream.1=="Transcode")
        let baseline=try BundledPatchBaseline.load()
        if let api=baseline.sources.jellyfinAPI{
            _ = try await JellyfinAPIAdapter.load(JellyfinAdapterRevision(id:api.id,source:api.source),engine:engine)
        }
        if let runtime=baseline.sources.runtime{
            let defaults=try JSONCodec.encoder().encode(RuntimePolicy.baseline(Settings()))
            guard let input=try JSONSerialization.jsonObject(with:defaults) as? [String:Any] else{fatalError("Invalid policy fixture")}
            let source="(function(){\n"+runtime.source+"\nreturn JSON.stringify(typeof hooks !== 'undefined' && typeof hooks.configure === 'function' ? hooks.configure(api.state) : null);\n})()"
            let (output,actions)=try await engine.evaluateAsync(source,snapshot:input)
            precondition(actions.isEmpty)
            let config=try JSONCodec.decoder().decode(RuntimePolicy?.self,from:Data(output.utf8))
            _ = try (config ?? RuntimePolicy.baseline(Settings())).checked()
        }
        let fixtureRoot=FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer{try? FileManager.default.removeItem(at:fixtureRoot)}
        let runtime=EngineeringRuntime(disk:Disk(root:fixtureRoot),engine:engine)
        let fixtureChannel=Channel(id:"one",name:"Fixture",number:nil,logo:false,favorite:false,program:nil,genres:[])
        _ = try await runtime.install(id:"abi2",source:"var hooks={playbackAttempt:function(p){p.policy.segmentContainer=p.attempt===1?'ts':'mp4';p.policy.forceEncode=p.attempt>1;return p.policy;}};",settings:Settings())
        let first=try await runtime.playbackAttempt(channel:fixtureChannel,attempt:1,previousFailure:"",defaults:PlaybackAttemptPolicy())
        let second=try await runtime.playbackAttempt(channel:fixtureChannel,attempt:2,previousFailure:"timeout",defaults:PlaybackAttemptPolicy())
        precondition(first.segmentContainer=="ts" && !first.forceEncode && second.segmentContainer=="mp4" && second.forceEncode)
        var invalidRejected=false
        do{_ = try await runtime.install(id:"abi2-invalid",source:"var hooks={playbackAttempt:function(p){p.policy.maxWidth=99999;return p.policy;}};",settings:Settings())}catch{invalidRejected=true}
        precondition(invalidRejected && runtime.activeID=="abi2")
        _ = try await runtime.install(id:"abi2-next",source:"var hooks={playbackAttempt:function(p){p.policy.segmentContainer='ts';return p.policy;}};",settings:Settings())
        _ = try await runtime.rollback(settings:Settings())
        let restored=try await runtime.playbackAttempt(channel:fixtureChannel,attempt:2,previousFailure:"timeout",defaults:PlaybackAttemptPolicy())
        precondition(restored.segmentContainer=="mp4")
        print("PASS: ABI 2 per-attempt transport hook, bounded values and rollback")
        let budgetEngine=ScriptEngine()
        budgetEngine.budget=InterpreterBudget(timeoutMs:150,memoryMiB:16,maxCommands:2)
        let budgetStart=ProcessInfo.processInfo.systemUptime
        var shortTimeout=false
        do{_ = try await budgetEngine.evaluateAsync("while(true){}",snapshot:[:])}catch{shortTimeout=error.localizedDescription.lowercased().contains("deadline")}
        precondition(shortTimeout && ProcessInfo.processInfo.systemUptime-budgetStart<2)
        var cappedCommands=false
        do{_ = try await budgetEngine.evaluateAsync("for(let i=0;i<3;i++)api.command('get_status',{});",snapshot:[:])}catch{cappedCommands=true}
        precondition(cappedCommands && budgetEngine.failures==2)
        let (recovered,_)=try await budgetEngine.evaluateAsync("40+2",snapshot:[:])
        precondition(recovered=="42" && budgetEngine.evaluations==3)
        var footprint:UInt64=0,cpu:UInt64=0
        let flags=jf_process_resources(&footprint,&cpu)
        precondition(flags & 1 != 0 && flags & 2 != 0 && footprint>0 && cpu>0)
        let hotConfigFixture=try HotConfiguration.decode(Data(contentsOf:URL(fileURLWithPath:"Examples/hot-configuration.json")))
        let configDisk=Disk(root:fixtureRoot.appendingPathComponent("config"))
        let stored=try HotConfigurationStore().activating(hotConfigFixture,expected:"native-baseline")
        try configDisk.saveConfiguration(stored)
        precondition(configDisk.load(HotConfigurationStore.self,"hot-config.json")==stored)
        var collision=hotConfigFixture;collision.operations?.systemSampleSeconds=30
        var collisionRejected=false
        do{try configDisk.saveConfiguration(HotConfigurationStore(active:collision,previous:nil))}catch{collisionRejected=true}
        precondition(collisionRejected && configDisk.load(HotConfigurationStore.self,"hot-config.json")==stored)
        for index in 0..<5{precondition(configDisk.save(["event":index],"event-\(index).json"))}
        precondition(configDisk.save(["receipt":1],"receipt-fixture.json"))
        precondition(configDisk.queuedCount(prefix:"event-")==5)
        precondition(configDisk.save(["event":99],"event-0.json"))
        precondition(configDisk.queuedCount(prefix:"event-")==5)
        let trimmed=configDisk.trimDiagnosticEvents(maxCount:2,maxBytes:1024*1024)
        precondition(trimmed.removed==3 && configDisk.queuedCount(prefix:"event-")==2 && configDisk.files(prefix:"receipt-").count==1)
        precondition(configDisk.save(["event":6],"event-6.json"))
        precondition(configDisk.queuedCount(prefix:"event-")==3)
        configDisk.remove(configDisk.root.appendingPathComponent("event-6.json"))
        precondition(configDisk.queuedCount(prefix:"event-")==2)
        for index in 7..<12{precondition(configDisk.save(["event":index],"event-\(index).json"))}
        let asyncTrimmed=await configDisk.trimDiagnosticEventsAsync(maxCount:2,maxBytes:1024*1024)
        precondition(asyncTrimmed.removed==5 && configDisk.queuedCount(prefix:"event-")==2 && configDisk.files(prefix:"receipt-").count==1)
        // Durable diagnostics: exact index accounting, byte bound, retention
        // journal and unclean-launch evidence. Only event-* files may be pruned.
        let diagRoot=fixtureRoot.appendingPathComponent("diagnostics")
        let diagDisk=Disk(root:diagRoot)
        precondition(diagDisk.previousUncleanLaunch==nil && diagDisk.queuedCount(prefix:"event-")==0)
        for index in 0..<6{precondition(diagDisk.save(Event(severity:"INFO",component:"focused",code:"EVENT_\(index)"),"event-\(index).json"))}
        precondition(diagDisk.queuedCount(prefix:"event-")==6 && diagDisk.files(prefix:"event-").count==6)
        precondition(diagDisk.save(Event(severity:"INFO",component:"focused",code:"OVERWRITE"),"event-0.json"))
        precondition(diagDisk.queuedCount(prefix:"event-")==6)
        precondition(diagDisk.save(["receipt":"preserved"],"receipt-focused.json"))
        let focusedReceipt=Receipt(id:"focused-receipt",action:"fixture",status:"interrupted",result:"preserved")
        precondition(diagDisk.save(focusedReceipt,"receipt-focused-receipt.json"))
        precondition(diagDisk.save(["settings":"preserved"],"settings.json"))
        let focusedTrim=await diagDisk.trimDiagnosticEventsAsync(maxCount:2,maxBytes:1024*1024)
        precondition(focusedTrim.removed==4 && diagDisk.queuedCount(prefix:"event-")==2)
        precondition(diagDisk.retention.totalEventsRemoved==4 && diagDisk.retention.lastError==nil && diagDisk.retention.lastEventCount==2)
        precondition(diagDisk.files(prefix:"receipt-").count==2 && diagDisk.load([String:String].self,"settings.json")?["settings"]=="preserved")
        let relaunchedDisk=Disk(root:diagRoot)
        precondition(relaunchedDisk.previousUncleanLaunch?.launchID==diagDisk.diagnosticLaunchID)
        precondition(relaunchedDisk.retention.totalEventsRemoved==4)
        precondition(relaunchedDisk.queuedCount(prefix:"event-")==3)
        let preservedReceipt=relaunchedDisk.load(Receipt.self,"receipt-focused-receipt.json")
        precondition(preservedReceipt?.id==focusedReceipt.id && preservedReceipt?.status=="interrupted" && preservedReceipt?.result=="preserved")
        relaunchedDisk.markCleanShutdown()
        let cleanDisk=Disk(root:diagRoot)
        precondition(cleanDisk.previousUncleanLaunch==nil && cleanDisk.queuedCount(prefix:"event-")==3)
        let byteTrim=await cleanDisk.trimDiagnosticEventsAsync(maxCount:100,maxBytes:1)
        precondition(byteTrim.removed>=1 && cleanDisk.queuedCount(prefix:"event-")<3)
        let asyncDisk=Disk(root:fixtureRoot.appendingPathComponent("async-events"))
        for index in 0..<3 {
            let appended=await asyncDisk.appendEventAsync(Event(severity:"INFO",component:"focused",code:"ASYNC_\(index)"),"event-async-\(index).json")
            precondition(appended)
        }
        precondition(asyncDisk.queuedCount(prefix:"event-")==3 && asyncDisk.files(prefix:"event-").count==3)
        let asyncFocusedTrim=await asyncDisk.trimDiagnosticEventsAsync(maxCount:1,maxBytes:1024*1024)
        precondition(asyncFocusedTrim.removed==2 && asyncDisk.queuedCount(prefix:"event-")==1)
        let orderedDisk=Disk(root:fixtureRoot.appendingPathComponent("ordered-events"))
        let appendQueue=EventAppendQueue(disk:orderedDisk,maxPending:3)
        for index in 0..<5{appendQueue.enqueue(Event(severity:"INFO",component:"focused",code:"QUEUE_\(index)"),name:"event-queue-\(index).json")}
        precondition(appendQueue.pendingCount==3 && appendQueue.dropped==2)
        await appendQueue.flush()
        precondition(orderedDisk.files(prefix:"event-").map{$0.lastPathComponent}==["event-queue-2.json","event-queue-3.json","event-queue-4.json"])
        let queuedEvents=await orderedDisk.loadRecentEvents(limit:3)
        precondition(queuedEvents.map(\.code)==["QUEUE_2","QUEUE_3","QUEUE_4"])
        let recentDisk=Disk(root:fixtureRoot.appendingPathComponent("recent-events"))
        for index in 0..<4{
            let name="event-recent-\(index).json"
            precondition(recentDisk.save(Event(severity:"INFO",component:"focused",code:"RECENT_\(index)"),name))
            let stamp=Date(timeIntervalSince1970:1_700_000_000+Double(index))
            try FileManager.default.setAttributes([.creationDate:stamp],ofItemAtPath:recentDisk.root.appendingPathComponent(name).path)
        }
        let recentEvents=await recentDisk.loadRecentEvents(limit:2)
        precondition(recentEvents.map(\.code)==["RECENT_2","RECENT_3"])
        let lifecycleDisk=Disk(root:fixtureRoot.appendingPathComponent("lifecycle-events"))
        let lifecycleQueue=EventAppendQueue(disk:lifecycleDisk,maxPending:8)
        for index in 0..<3{lifecycleQueue.enqueue(Event(severity:"INFO",component:"focused",code:"LIFECYCLE_\(index)"),name:"event-lifecycle-\(index).json")}
        await lifecycleQueue.flush()
        lifecycleDisk.markCleanShutdown()
        let cleanLifecycleDisk=Disk(root:fixtureRoot.appendingPathComponent("lifecycle-events"))
        precondition(cleanLifecycleDisk.previousUncleanLaunch==nil && cleanLifecycleDisk.queuedCount(prefix:"event-")==3)
        print("PASS: exact diagnostic accounting, byte bound, durable retention, bounded ordered off-main append, startup replay order and clean lifecycle evidence")
        let trial=try HotConfigurationTrial(before:HotConfigurationStore(),candidateRevision:hotConfigFixture.revision,expiresAt:Date().addingTimeInterval(60)).checked()
        precondition(configDisk.save(trial,"hot-config-trial.json"))
        let recoveredTrial=try configDisk.recoverConfigurationTrial()
        precondition(recoveredTrial?.pending==false && recoveredTrial?.outcome=="recovered-on-launch")
        precondition(configDisk.load(HotConfigurationStore.self,"hot-config.json")?.revision=="native-baseline")
        let secondRecovery=try configDisk.recoverConfigurationTrial()
        precondition(secondRecovery?.pending==false)
        precondition(configDisk.load(HotConfiguration.self,"hot-config-version-"+hotConfigFixture.revision+".json")==hotConfigFixture)
        print("PASS: interrupted trial recovery restores exact prior configuration and retains candidate archive")
        let appDisk=Disk(root:fixtureRoot.appendingPathComponent("app-revision"))
        let baselineSnapshot=ApplicationSnapshot(patches:PatchRestoreSnapshot(runtime:RuntimePatchStore(),api:JellyfinAdapterStore(),ui:DynamicUIStore()),configuration:HotConfigurationStore())
        let candidate=try ApplicationRevision(id:"trial-fixture",sources:PatchSourceSet(),configuration:hotConfigFixture).checked()
        let afterSnapshot=ApplicationSnapshot(patches:baselineSnapshot.patches,configuration:stored)
        let journal=ApplicationTrialJournal(id:UUID().uuidString,before:baselineSnapshot,target:candidate,expiresAt:Date().addingTimeInterval(60),outcome:nil)
        try ApplicationRevisionStore.begin(journal,after:afterSnapshot,disk:appDisk)
        precondition(appDisk.load(HotConfigurationStore.self,"hot-config.json")?.revision==hotConfigFixture.revision)
        let recoveredApplication=try ApplicationRevisionStore.recover(disk:appDisk)
        precondition(recoveredApplication?.outcome=="recovered-on-launch" && recoveredApplication?.pending==false)
        precondition(appDisk.load(HotConfigurationStore.self,"hot-config.json")?.revision=="native-baseline")
        precondition(appDisk.load(ApplicationRevision.self,"application-revision-trial-fixture.json")==candidate)
        precondition(appDisk.save(["fixture":"preserved"],"settings.json"))
        precondition(appDisk.save(["receipt":"preserved"],"receipt-fixture.json"))
        try Data("broken journal".utf8).write(to:appDisk.root.appendingPathComponent("patch-restore.json"))
        try ApplicationRevisionStore.safeReset(disk:appDisk)
        precondition(appDisk.load(Bool.self,"native-safe-mode.json")==true)
        precondition(appDisk.load(Bool.self,"native-safe-reset-complete.json")==true)
        precondition(appDisk.load([String:String].self,"settings.json")?["fixture"]=="preserved")
        precondition(appDisk.load([String:String].self,"receipt-fixture.json")?["receipt"]=="preserved")
        precondition(appDisk.load(ApplicationRevision.self,"application-revision-trial-fixture.json")==candidate)
        try ApplicationRevisionStore.completeSafeResetIfNeeded(disk:appDisk)
        print("PASS: unified application journal recovery, native safe reset and archived data preservation")

        print("PASS: configured interpreter deadline, command cap, recovery, process counters, immutable configuration and isolated log retention")
        try await HotPatchIntegration.run(engine:engine)
        let lanFixture=LANServer(port:.any,advertise:false)
        lanFixture.handler={body,complete in complete("200 OK",body)}
        try lanFixture.start()
        defer{lanFixture.stop()}
        for _ in 0..<100 {
            if lanFixture.port != nil{break}
            try await Task.sleep(nanoseconds:20_000_000)
        }
        guard let port=lanFixture.port,let url=URL(string:"http://127.0.0.1:\(port)/command") else{throw AppError.message("LAN listener did not start")}
        let config=URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest=15
        let localTransport=URLSession(configuration:config)
        for size in [64*1024,192*1024,1024*1024,LANHTTP.maxBody] {
            var request=URLRequest(url:url);request.httpMethod="POST"
            request.httpBody=Data(repeating:65,count:size)
            let (reply,response)=try await localTransport.data(for:request)
            precondition((response as? HTTPURLResponse)?.statusCode==200 && reply==request.httpBody)
            print("PASS: actual LAN HTTP listener round trip \(size) bytes")
            fflush(stdout)
        }
        // Send only the declared oversized header. The server must reject
        // without waiting for a multi-megabyte upload or draining its body.
        let oversizeProbe=Process()
        oversizeProbe.executableURL=URL(fileURLWithPath:"/usr/bin/python3")
        oversizeProbe.arguments=["-c","""
import socket, sys
with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=10) as s:
    s.settimeout(10)
    s.sendall(("POST /command HTTP/1.1\\r\\nHost: localhost\\r\\nContent-Length: "+sys.argv[2]+"\\r\\n\\r\\n").encode())
    data=b""
    while b"\\r\\n" not in data:
        part=s.recv(4096)
        if not part: break
        data+=part
    assert data.startswith(b"HTTP/1.1 413 "), repr(data[:200])
""",String(port),String(LANHTTP.maxBody+1)]
        try oversizeProbe.run()
        oversizeProbe.waitUntilExit()
        guard oversizeProbe.terminationStatus==0 else{throw AppError.message("Oversized HTTP header was not rejected")}
        print("PASS: actual LAN listener rejects over-limit request")
        let commandFixture=LANServer(port:.any,advertise:false)
        var dispatchCount=0
        commandFixture.handler={body,complete in
            Task { @MainActor in
                let response=await LANCommandResponse.make(body){cmd in
                    dispatchCount+=1
                    return Receipt(id:cmd.id,action:cmd.action,status:cmd.action=="get_status" ? "succeeded":"failed",result:"fixture")
                }
                complete(response.0,response.1)
            }
        }
        try commandFixture.start()
        defer{commandFixture.stop()}
        for _ in 0..<100 {
            if commandFixture.port != nil{break}
            try await Task.sleep(nanoseconds:20_000_000)
        }
        guard let commandPort=commandFixture.port,let commandURL=URL(string:"http://127.0.0.1:\(commandPort)/command") else {
            throw AppError.message("Command fixture did not start")
        }
        func commandRequest(_ data:Data)async throws->(Int,Data){
            var req=URLRequest(url:commandURL);req.httpMethod="POST";req.httpBody=data;req.timeoutInterval=5
            let (body,response)=try await localTransport.data(for:req)
            return ((response as? HTTPURLResponse)?.statusCode ?? -1,body)
        }
        for data in [Data("{not-json".utf8),Data("{}".utf8),Data("[]".utf8)] {
            let (status,body)=try await commandRequest(data)
            precondition(status==400)
            let reply=try JSONSerialization.jsonObject(with:body) as? [String:Any]
            precondition(reply?["status"] as? String=="failed")
        }
        precondition(dispatchCount==0)
        for (action,wanted) in [("get_status","succeeded"),("unknown","failed")] {
            let command=Command(id:"fixture-"+action,action:action,arguments:[:],expiresAt:nil)
            let (status,body)=try await commandRequest(JSONCodec.encoder().encode(command))
            precondition(status==200)
            let receipt=try JSONCodec.decoder().decode(Receipt.self,from:body)
            precondition(receipt.id==command.id && receipt.status==wanted)
        }
        precondition(dispatchCount==2)
        print("PASS: malformed LAN envelopes return 400 without dispatch; decoded commands retain HTTP 200 receipts")
        // LAN lifecycle: idle/partial clients release their slots, completed
        // responses release the slot at the connection cap, a handler that
        // outlives its deadline cannot retain the listener, and authentication
        // plus the /health bypass are unchanged.
        let lifecycleFixture=LANServer(port:.any,advertise:false,maxConnections:1,idleTimeout:0.4,responseTimeout:5,queryTimeout:5)
        lifecycleFixture.handler={body,complete in complete("200 OK",body)}
        try lifecycleFixture.start()
        defer{lifecycleFixture.stop()}
        for _ in 0..<100 {if lifecycleFixture.port != nil{break};try await Task.sleep(nanoseconds:20_000_000)}
        guard let lifecyclePort=lifecycleFixture.port else{throw AppError.message("LAN lifecycle listener did not start")}
        let idleProbe=Process()
        idleProbe.executableURL=URL(fileURLWithPath:"/usr/bin/python3")
        idleProbe.arguments=["-c","""
import socket, sys, time
port=int(sys.argv[1])
s=socket.create_connection(("127.0.0.1",port),timeout=3)
s.sendall(b"GET /health HTTP/1.1\\r\\nHost: x\\r\\n")
time.sleep(1.0)
try:
    s.sendall(b"X"); data=s.recv(1024)
except Exception:
    data=b""
assert data==b"", data
s.close()
t=socket.create_connection(("127.0.0.1",port),timeout=3)
t.sendall(b"GET /health HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n")
data=b""
while b"\\r\\n\\r\\n" not in data:
    part=t.recv(1024)
    if not part: break
    data+=part
assert data.startswith(b"HTTP/1.1 200 "), data[:100]
t.close()
""",String(lifecyclePort)]
        try idleProbe.run()
        idleProbe.waitUntilExit()
        guard idleProbe.terminationStatus==0 else{throw AppError.message("Idle LAN connection was not cleaned up")}
        guard let lifecycleURL=URL(string:"http://127.0.0.1:\(lifecyclePort)/command") else{throw AppError.message("Invalid LAN URL")}
        for index in 0..<12 {
            var req=URLRequest(url:lifecycleURL);req.httpMethod="POST";req.httpBody=Data("fixture-\(index)".utf8);req.timeoutInterval=5
            let (reply,response)=try await localTransport.data(for:req)
            precondition((response as? HTTPURLResponse)?.statusCode==200 && reply==req.httpBody)
        }
        print("PASS: idle and completed LAN control connections release bounded slots")
        let deadlineFixture=LANServer(port:.any,advertise:false,maxConnections:1,idleTimeout:2,responseTimeout:0.4,queryTimeout:5)
        deadlineFixture.handler={_,complete in Task{try? await Task.sleep(nanoseconds:1_000_000_000);complete("200 OK",Data("late".utf8))}}
        try deadlineFixture.start()
        defer{deadlineFixture.stop()}
        for _ in 0..<100 {if deadlineFixture.port != nil{break};try await Task.sleep(nanoseconds:20_000_000)}
        guard let deadlinePort=deadlineFixture.port,
              let deadlineURL=URL(string:"http://127.0.0.1:\(deadlinePort)/command"),
              let deadlineHealth=URL(string:"http://127.0.0.1:\(deadlinePort)/health") else{throw AppError.message("LAN deadline fixture did not start")}
        var lateTimedOut=false
        do{
            var req=URLRequest(url:deadlineURL);req.httpMethod="POST";req.httpBody=Data("late".utf8);req.timeoutInterval=5
            _ = try await localTransport.data(for:req)
        }catch{lateTimedOut=true}
        precondition(lateTimedOut)
        try await Task.sleep(nanoseconds:1_200_000_000)
        let (_,health)=try await localTransport.data(for:URLRequest(url:deadlineHealth))
        precondition((health as? HTTPURLResponse)?.statusCode==200)
        print("PASS: late command completion cannot retain a LAN control connection")
        let authFixture=LANServer(port:.any,advertise:false,maxConnections:4,idleTimeout:2,responseTimeout:5,queryTimeout:5)
        authFixture.setAccessToken("fixture-token")
        authFixture.handler={_,complete in complete("200 OK",Data("{}".utf8))}
        authFixture.queryHandler={path,complete in complete(path=="/health" || path=="/status" ? "200 OK":"404 Not Found",Data("{}".utf8))}
        try authFixture.start()
        defer{authFixture.stop()}
        for _ in 0..<100 {if authFixture.port != nil{break};try await Task.sleep(nanoseconds:20_000_000)}
        guard let authPort=authFixture.port else{throw AppError.message("LAN auth fixture did not start")}
        func authRequest(_ path:String,method:String="GET",token:String?=nil,body:Data?=nil)async throws->Int{
            guard let url=URL(string:"http://127.0.0.1:\(authPort)"+path) else{throw AppError.message("Invalid LAN URL")}
            var req=URLRequest(url:url);req.httpMethod=method;req.httpBody=body
            if let token=token{req.setValue("Bearer "+token,forHTTPHeaderField:"Authorization")}
            req.timeoutInterval=5
            let (_,response)=try await localTransport.data(for:req)
            return (response as? HTTPURLResponse)?.statusCode ?? -1
        }
        let healthStatus=try await authRequest("/health")
        let unauthenticatedStatus=try await authRequest("/status")
        let authenticatedStatus=try await authRequest("/status",token:"fixture-token")
        let wrongTokenCommand=try await authRequest("/command",method:"POST",token:"wrong",body:Data("{}".utf8))
        let authenticatedCommand=try await authRequest("/command",method:"POST",token:"fixture-token",body:Data("{}".utf8))
        precondition(healthStatus==200 && unauthenticatedStatus==401 && authenticatedStatus==200 && wrongTokenCommand==401 && authenticatedCommand==200)
        print("PASS: LAN authentication and /health bypass preserved after connection cleanup")
        // A second listener initially cannot bind a held port. It must recover
        // without a UI tick, and an explicit stop must cancel its retries.
        let retryFixture=LANServer(port:NWEndpoint.Port(rawValue:authPort)!,advertise:false)
        defer{retryFixture.stop()}
        do{try retryFixture.start()}catch{}
        try await Task.sleep(nanoseconds:300_000_000)
        authFixture.stop()
        var retryReady=false
        for _ in 0..<100 {
            if let retryPort=retryFixture.port,
               let retryURL=URL(string:"http://127.0.0.1:\(retryPort)/health"){
                var req=URLRequest(url:retryURL);req.timeoutInterval=0.5
                if let (_,reply)=try? await localTransport.data(for:req),(reply as? HTTPURLResponse)?.statusCode==200{retryReady=true;break}
            }
            try await Task.sleep(nanoseconds:200_000_000)
        }
        precondition(retryReady,"listener did not recover after port became available")
        retryFixture.stop()
        try await Task.sleep(nanoseconds:1_200_000_000)
        precondition(retryFixture.port==nil,"explicit stop must cancel retry")
        print("PASS: LAN listener bind recovery and explicit stop")

        print("PASS: runtime bridge, interrupt recovery, Jellyfin hooks and embedded release baseline")
    }
}

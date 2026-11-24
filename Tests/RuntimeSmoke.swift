import Foundation

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
        print("PASS: runtime bridge, interrupt recovery, Jellyfin hooks and embedded release baseline")
    }
}

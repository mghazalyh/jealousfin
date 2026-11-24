import Foundation

// Pure hooks run in the existing interruptible interpreter. The native host
// owns HTTP, credentials, session lifecycle, and durable revision activation.
@MainActor final class JellyfinAPIAdapter{
    let revision:JellyfinAdapterRevision
    let spec:JellyfinAdapterSpec
    private let engine:ScriptEngine
    init(revision:JellyfinAdapterRevision,spec:JellyfinAdapterSpec,engine:ScriptEngine){
        self.revision=revision;self.spec=spec;self.engine=engine
    }
    private static func evaluate(_ revision:JellyfinAdapterRevision,engine:ScriptEngine,hook:String,input:[String:Any]) async throws -> [String:Any]? {
        let source="(function(){\n"+revision.source+"\nreturn JSON.stringify(typeof hooks !== 'undefined' && typeof hooks['"+hook+"'] === 'function' ? hooks['"+hook+"'](api.state) : null);\n})()"
        let (output,commands)=try await engine.evaluateAsync(source,snapshot:input)
        guard commands.isEmpty else{throw AppError.message("Jellyfin API hooks cannot dispatch commands")}
        let value=try JSONSerialization.jsonObject(with:Data(output.utf8),options:.fragmentsAllowed)
        if value is NSNull{return nil}
        guard let dictionary=value as? [String:Any] else{throw AppError.message("Jellyfin API hook must return an object or null")}
        return dictionary
    }
    static func load(_ revision:JellyfinAdapterRevision,engine:ScriptEngine)async throws->JellyfinAPIAdapter{
        guard DynamicUILayout.validID(revision.id),!revision.source.isEmpty,revision.source.utf8.count<=192*1024 else{throw AppError.message("Invalid Jellyfin API revision")}
        let baseline=JellyfinAdapterSpec()
        let data=try JSONCodec.encoder().encode(baseline)
        guard let input=try JSONSerialization.jsonObject(with:data) as? [String:Any] else{throw AppError.message("Invalid native adapter defaults")}
        let configured=try await evaluate(revision,engine:engine,hook:"configure",input:input) ?? input
        let spec=try JSONCodec.decoder().decode(JellyfinAdapterSpec.self,from:JSONSerialization.data(withJSONObject:configured)).checked()
        let adapter=JellyfinAPIAdapter(revision:revision,spec:spec,engine:engine)
        // Exercise the same request/response interfaces on deterministic data.
        _ = try await adapter.request(operation:"health",path:"System/Info/Public",method:"GET",query:[:],body:nil)
        _ = try await adapter.response(operation:"health",response:["ServerName":"Fixture","Version":"10.11.0"])
        return adapter
    }
    func request(operation:String,path:String,method:String,query:[String:String],body:[String:Any]?) async throws -> (path:String,method:String,query:[String:String],body:[String:Any]?){
        let login=operation=="authenticate"
        let input:[String:Any]=["operation":operation,"path":path,"method":method,"query":query,"body":login ? NSNull() as Any : (body.map{$0 as Any} ?? NSNull())]
        let output=try await Self.evaluate(revision,engine:engine,hook:"request",input:input) ?? input
        var result=try JellyfinAdapterRules.request(output,authenticate:login)
        if login {
            var payload=result.body ?? [:]
            payload[spec.usernameKey]=body?["Username"] as? String ?? ""
            payload[spec.passwordKey]=body?["Pw"] as? String ?? ""
            result.body=payload
        }
        return result
    }
    func response(operation:String,response:[String:Any])async throws->[String:Any]{
        // Authentication responses are extracted natively via configurable
        // field paths. Credentials are not passed into the interpreter.
        if operation=="authenticate"{return response}
        return try await Self.evaluate(revision,engine:engine,hook:"response",input:["operation":operation,"response":response]) ?? response
    }
    func stream(path:String,method:String,itemID:String)async throws->(String,String){
        let result=try await Self.evaluate(revision,engine:engine,hook:"stream",input:["path":path,"method":method,"itemID":itemID])
        guard let result=result else{return(path,method)}
        guard let p=result["path"] as? String,!p.isEmpty,p.utf8.count<=16384,
            let m=result["method"] as? String,["DirectPlay","DirectStream","Transcode"].contains(m)
        else{throw AppError.message("Invalid patched stream selection")}
        return(p,m)
    }
}

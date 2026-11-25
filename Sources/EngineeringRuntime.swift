import Foundation

// Native host ABI v1. Module source is interpreted and returns JSON, not native code.
// Hooks can select from known channels or replace policies without changing the IPA.
@MainActor final class EngineeringRuntime {
    private let disk: Disk
    private let engine: ScriptEngine
    private(set) var store: RuntimePatchStore
    private(set) var loading = false
    private(set) var fault: String?
    init(disk: Disk, engine: ScriptEngine) {
        self.disk=disk;self.engine=engine
        store=disk.load(RuntimePatchStore.self,"runtime-patch.json") ?? RuntimePatchStore()
    }
    var activeID:String {store.active?.id ?? "native-baseline"}
    private func persist(_ value:RuntimePatchStore)throws {
        guard disk.save(value,"runtime-patch.json") else{throw AppError.message("Cannot persist runtime module")}
        store=value
    }
    func boot(settings:Settings) async throws -> RuntimePolicy {
        if store.activationPending {
            let restored=RuntimePatchStore(active:store.previous,previous:nil,activationPending:false)
            try persist(restored)
            fault="Interrupted runtime activation rolled back"
        }
        guard let patch=store.active else{return try RuntimePolicy.baseline(settings).checked()}
        do {
            try persist(RuntimePatchStore(active:patch,previous:store.previous,activationPending:true))
            let p=try await policy(patch,settings:settings)
            try persist(RuntimePatchStore(active:patch,previous:store.previous,activationPending:false))
            return p
        } catch {
            let reason=String(describing:type(of:error))
            try persist(RuntimePatchStore(active:nil,previous:store.previous,activationPending:false))
            fault="Runtime activation failed: "+reason
            return try RuntimePolicy.baseline(settings).checked()
        }
    }
    func install(id:String,source:String,settings:Settings) async throws -> RuntimePolicy {
        guard !loading,id.range(of:"^[A-Za-z0-9_-]{1,80}$",options:.regularExpression) != nil,
              !source.isEmpty,source.utf8.count<=192*1024 else{throw AppError.message("Invalid module or activation already running")}
        loading=true;defer{loading=false}
        let candidate=RuntimePatch(id:id,source:source)
        let old=store.active
        let previous=store.previous
        guard old?.id != id else{throw AppError.message("Use a new revision ID")}
        try persist(RuntimePatchStore(active:candidate,previous:old,activationPending:true))
        do{
            let p=try await policy(candidate,settings:settings)
            try persist(RuntimePatchStore(active:candidate,previous:old,activationPending:false))
            fault=nil
            return p
        }catch{
            try persist(RuntimePatchStore(active:old,previous:previous,activationPending:false))
            fault="Candidate module rejected"
            throw error
        }
    }
    func rollback(settings:Settings) async throws -> RuntimePolicy {
        guard !loading else{throw AppError.message("Activation in progress")}
        loading=true;defer{loading=false}
        let previous=store.previous
        let p:RuntimePolicy
        if let previous=previous {p=try await policy(previous,settings:settings)}
        else {p=try RuntimePolicy.baseline(settings).checked()}
        try persist(RuntimePatchStore(active:previous,previous:nil,activationPending:false))
        fault=nil
        return p
    }
    func reset(settings:Settings)throws -> RuntimePolicy {
        guard !loading else{throw AppError.message("Activation in progress")}
        let p=try RuntimePolicy.baseline(settings).checked()
        try persist(RuntimePatchStore(active:nil,previous:nil,activationPending:false))
        fault=nil
        return p
    }
    private func evaluate<T:Decodable>(_ patch:RuntimePatch,hook:String,input:[String:Any],as type:T.Type) async throws -> T {
        let wrapper="(function(){\n"+patch.source+"\nreturn JSON.stringify(typeof hooks !== 'undefined' && typeof hooks['"+hook+"'] === 'function' ? hooks['"+hook+"'](api.state) : null);\n})()"
        let (output,commands)=try await engine.evaluateAsync(wrapper,snapshot:input)
        guard commands.isEmpty else{throw AppError.message("Policy hooks must return values, not execute commands")}
        return try JSONCodec.decoder().decode(type,from:Data(output.utf8))
    }
    func preview(_ patch:RuntimePatch?,settings:Settings) async throws -> RuntimePolicy {
        guard let patch=patch else{return try RuntimePolicy.baseline(settings).checked()}
        return try await policy(patch,settings:settings)
    }
    private func policy(_ patch:RuntimePatch,settings:Settings) async throws -> RuntimePolicy {
        let defaults=RuntimePolicy.baseline(settings)
        let encoded=try JSONCodec.encoder().encode(defaults)
        guard let input=try JSONSerialization.jsonObject(with:encoded) as? [String:Any] else{throw AppError.message("Cannot encode native policy")}
        let configured=try await evaluate(patch,hook:"configure",input:input,as:RuntimePolicy?.self)
        let result=try (configured ?? defaults).checked()
        let fixture=Channel(id:"abi-fixture",name:"Fixture",number:nil,logo:false,favorite:false,program:nil,genres:[])
        for attempt in [1,2] {
            _ = try await playbackAttempt(patch:patch,channel:fixture,attempt:attempt,previousFailure:attempt==1 ? "" : "Startup timeout",defaults:result.playback ?? PlaybackAttemptPolicy())
        }
        let selectionInput:[String:Any]=["candidates":[["id":"abi-fixture","name":"Fixture","favorite":false]],"currentID":""]
        if let id=try await evaluate(patch,hook:"select",input:selectionInput,as:String?.self),id != "abi-fixture"{throw AppError.message("Invalid selection in preflight")}
        if let choice=try await evaluate(patch,hook:"selectDecision",input:selectionInput,as:PlatformSelection?.self){_ = try choice.checked(ids:["abi-fixture"])}
        // Tick preflight uses the real snapshot shape in the event-module host;
        // legacy tick may depend on arbitrary existing checkpoint state.
        return result
    }
    func playbackAttempt(channel:Channel,attempt:Int,previousFailure:String,defaults:PlaybackAttemptPolicy)async throws->PlaybackAttemptPolicy {
        guard let patch=store.active,!loading else{return try defaults.checked()}
        return try await playbackAttempt(patch:patch,channel:channel,attempt:attempt,previousFailure:previousFailure,defaults:defaults)
    }
    private func playbackAttempt(patch:RuntimePatch,channel:Channel,attempt:Int,previousFailure:String,defaults:PlaybackAttemptPolicy)async throws->PlaybackAttemptPolicy {
        let encoded=try JSONCodec.encoder().encode(defaults)
        let policy=try JSONSerialization.jsonObject(with:encoded)
        let input:[String:Any]=["channelID":channel.id,"channelName":channel.name,"shared":channel.isSurf,"attempt":attempt,"previousFailure":previousFailure,"policy":policy]
        let result=try await evaluate(patch,hook:"playbackAttempt",input:input,as:PlaybackAttemptPolicy?.self)
        return try (result ?? defaults).checked()
    }
    func select(candidates:[Channel],currentID:String?) async throws -> String? {
        guard let patch=store.active,!loading else{return nil}
        let input:[String:Any]=["candidates":candidates.map{["id":$0.id,"name":$0.name,"favorite":$0.favorite] as [String:Any]},"currentID":currentID ?? ""]
        let id=try await evaluate(patch,hook:"select",input:input,as:String?.self)
        guard let id=id else{return nil}
        guard candidates.contains(where:{$0.id==id}) else{throw AppError.message("Module selected an ineligible channel")}
        return id
    }
    func selection(candidates:[Channel],currentID:String?)async throws->PlatformSelection{
        guard let patch=store.active,!loading else{return PlatformSelection(outcome:"fallback")}
        let input:[String:Any]=["candidates":candidates.map{["id":$0.id,"name":$0.name,"favorite":$0.favorite] as [String:Any]},"currentID":currentID ?? ""]
        if let choice=try await evaluate(patch,hook:"selectDecision",input:input,as:PlatformSelection?.self){
            return try choice.checked(ids:Set(candidates.map(\.id)))
        }
        if let id=try await select(candidates:candidates,currentID:currentID){return PlatformSelection(outcome:"select",channelID:id)}
        return PlatformSelection(outcome:"fallback")
    }
    func decision(snapshot:[String:Any],state:[String:String],events:[[String:Any]],receipts:[String]) async throws -> ModuleDecision? {
        guard let patch=store.active,!loading else{return nil}
        let input:[String:Any]=["snapshot":snapshot,"state":state,"events":events,"receipts":receipts]
        let value=try await evaluate(patch,hook:"tick",input:input,as:ModuleDecision?.self)
        return try value?.checked()
    }
}

@MainActor enum PlatformEvaluator{
    static func evaluate(_ module:PlatformModule,input:[String:Any],hostBudget:InterpreterBudget)async throws->ModuleDecision{
        _ = try module.checked()
        guard let hook=input["hook"] as? String,module.hooks.contains(hook) else{throw AppError.message("Unknown module hook")}
        var result=ModuleDecision()
        if let source=module.source{
            let engine=ScriptEngine()
            let requested=module.budget ?? InterpreterBudget()
            engine.budget=InterpreterBudget(timeoutMs:min(requested.timeoutMs ?? 1000,hostBudget.timeoutMs ?? 5000),
                memoryMiB:min(requested.memoryMiB ?? 16,hostBudget.memoryMiB ?? 32),maxCommands:1)
            let wrapper="(function(){\n"+source+"\nconst h=api.state.hook; const f=typeof hooks==='object' && (hooks[h] || hooks.onEvent); return JSON.stringify(typeof f==='function' ? (f(api.state)||null):null);})()"
            let (text,commands)=try await engine.evaluateAsync(wrapper,snapshot:input)
            guard commands.isEmpty,text.utf8.count<=131072 else{throw AppError.message("Event hooks return decisions, never execute commands")}
            result=try JSONCodec.decoder().decode(ModuleDecision?.self,from:Data(text.utf8)) ?? ModuleDecision()
        }
        let state=input["snapshot"] as? [String:Any] ?? [:]
        let rules=(module.rules ?? []).filter{$0.matches(hook:hook,state:state)}.flatMap(\.actions)
        result.actions=(result.actions ?? [])+rules
        _ = try result.checked()
        try HotPatchPlatform.validateActions(result.actions ?? [])
        return result
    }
}

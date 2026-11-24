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
    private func policy(_ patch:RuntimePatch,settings:Settings) async throws -> RuntimePolicy {
        let defaults=RuntimePolicy.baseline(settings)
        let encoded=try JSONCodec.encoder().encode(defaults)
        guard let input=try JSONSerialization.jsonObject(with:encoded) as? [String:Any] else{throw AppError.message("Cannot encode native policy")}
        let configured=try await evaluate(patch,hook:"configure",input:input,as:RuntimePolicy?.self)
        return try (configured ?? defaults).checked()
    }
    func select(candidates:[Channel],currentID:String?) async throws -> String? {
        guard let patch=store.active,!loading else{return nil}
        let input:[String:Any]=["candidates":candidates.map{["id":$0.id,"name":$0.name,"favorite":$0.favorite] as [String:Any]},"currentID":currentID ?? ""]
        let id=try await evaluate(patch,hook:"select",input:input,as:String?.self)
        guard let id=id else{return nil}
        guard candidates.contains(where:{$0.id==id}) else{throw AppError.message("Module selected an ineligible channel")}
        return id
    }
    func decision(snapshot:[String:Any],state:[String:String],events:[[String:Any]],receipts:[String]) async throws -> ModuleDecision? {
        guard let patch=store.active,!loading else{return nil}
        let input:[String:Any]=["snapshot":snapshot,"state":state,"events":events,"receipts":receipts]
        let value=try await evaluate(patch,hook:"tick",input:input,as:ModuleDecision?.self)
        return try value?.checked()
    }
}

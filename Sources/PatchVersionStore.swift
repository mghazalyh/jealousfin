import Foundation

struct PatchVersionSummary:Codable,Identifiable{
    var id:String
    var parentID:String?
    var restoredFrom:String?
    var label:String?
    var knownGood:Bool
    var recordedAt:Date
    var reason:String
    var runtime:String
    var jellyfinAPI:String
    var ui:String
    init(_ a:PatchArchive){
        id=a.id;parentID=a.parentID;restoredFrom=a.restoredFrom;label=a.label
        knownGood=a.knownGood ?? false;recordedAt=a.recordedAt;reason=a.reason
        runtime=a.sources.runtime?.id ?? "native-baseline"
        jellyfinAPI=a.sources.jellyfinAPI?.id ?? "native-baseline"
        ui=a.sources.ui?.revision ?? "native-baseline"
    }
}
enum PatchVersionScope:String,CaseIterable,Codable{
    case all,runtime,jellyfinAPI,ui
    func merge(current:PatchSourceSet,target:PatchSourceSet)->PatchSourceSet{
        switch self{
        case .all:return target
        case .runtime:return PatchSourceSet(runtime:target.runtime,jellyfinAPI:current.jellyfinAPI,ui:current.ui)
        case .jellyfinAPI:return PatchSourceSet(runtime:current.runtime,jellyfinAPI:target.jellyfinAPI,ui:current.ui)
        case .ui:return PatchSourceSet(runtime:current.runtime,jellyfinAPI:current.jellyfinAPI,ui:target.ui)
        }
    }
}
struct PatchVersionComparison:Codable{
    var from:String
    var to:String
    var runtimeChanged:Bool
    var apiChanged:Bool
    var uiChanged:Bool
    init(from a:PatchArchive,to b:PatchArchive){
        from=a.id;to=b.id;runtimeChanged=a.sources.runtime != b.sources.runtime
        apiChanged=a.sources.jellyfinAPI != b.sources.jellyfinAPI
        uiChanged=a.sources.ui != b.sources.ui
    }
}
struct PatchRestoreSnapshot:Codable{
    var runtime:RuntimePatchStore
    var api:JellyfinAdapterStore
    var ui:DynamicUIStore
}
struct PatchRestoreJournal:Codable{
    var before:PatchRestoreSnapshot
    var targetID:String
    var scope:PatchVersionScope
    var committed=false
}
@MainActor enum PatchVersionStore{
    static func retain(_ record:PatchArchive,disk:Disk)throws{
        guard record.schemaVersion==1,DynamicUILayout.validID(record.id) else{throw AppError.message("Invalid version archive")}
        _ = try record.sources.checked()
        let name="patch-version-"+record.id+".json"
        if let existing=disk.load(PatchArchive.self,name){
            guard existing==record else{throw AppError.message("Immutable patch version collision")}
            return
        }
        guard disk.save(record,name) else{throw AppError.message("Cannot retain patch version")}
    }
    static func load(_ id:String,disk:Disk)->PatchArchive?{
        guard DynamicUILayout.validID(id) else{return nil}
        return disk.load(PatchArchive.self,"patch-version-"+id+".json")
    }
    static func history(disk:Disk)->[PatchVersionSummary]{
        disk.files(prefix:"patch-version-").compactMap{url in
            disk.load(PatchArchive.self,url.lastPathComponent).map(PatchVersionSummary.init)
        }.sorted{a,b in a.recordedAt==b.recordedAt ? a.id>b.id:a.recordedAt>b.recordedAt}
    }
    static func write(_ snapshot:PatchRestoreSnapshot,disk:Disk)throws{
        guard disk.save(snapshot.runtime,"runtime-patch.json"),
              disk.save(snapshot.api,"jellyfin-api.json"),
              disk.save(snapshot.ui,"dynamic-ui.json") else{throw AppError.message("Cannot write complete patch source set")}
    }
    static func recover(disk:Disk)throws->Bool{
        guard let journal=disk.load(PatchRestoreJournal.self,"patch-restore.json"),!journal.committed else{return false}
        try write(journal.before,disk:disk)
        var resolved=journal;resolved.committed=true
        guard disk.save(resolved,"patch-restore.json") else{throw AppError.message("Cannot finish interrupted restore recovery")}
        return true
    }
    static func commit(before:PatchRestoreSnapshot,after:PatchRestoreSnapshot,targetID:String,scope:PatchVersionScope,disk:Disk)throws{
        var journal=PatchRestoreJournal(before:before,targetID:targetID,scope:scope)
        guard disk.save(journal,"patch-restore.json") else{throw AppError.message("Cannot journal patch restore")}
        do{
            try write(after,disk:disk)
            journal.committed=true
            guard disk.save(journal,"patch-restore.json") else{throw AppError.message("Cannot finalize patch restore")}
        }catch{
            // The pending journal remains available for the next startup if recovery fails.
            _ = try recover(disk:disk)
            throw error
        }
    }
}

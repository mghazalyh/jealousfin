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
            guard try JSONCodec.encoder().encode(existing)==JSONCodec.encoder().encode(record) else{throw AppError.message("Immutable patch version collision")}
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
        guard FileManager.default.fileExists(atPath:disk.root.appendingPathComponent("patch-restore.json").path) else{return false}
        guard let journal=disk.load(PatchRestoreJournal.self,"patch-restore.json") else{throw AppError.message("Unreadable patch recovery journal")}
        guard !journal.committed else{return false}
        try write(journal.before,disk:disk)
        var resolved=journal;resolved.committed=true
        guard disk.save(resolved,"patch-restore.json") else{throw AppError.message("Cannot finish interrupted restore recovery")}
        return true
    }
    static func commit(before:PatchRestoreSnapshot,after:PatchRestoreSnapshot,targetID:String,scope:PatchVersionScope,disk:Disk)throws{
        if FileManager.default.fileExists(atPath:disk.root.appendingPathComponent("patch-restore.json").path){
            guard disk.load(PatchRestoreJournal.self,"patch-restore.json")?.committed==true else{throw AppError.message("An earlier restore requires recovery")}
        }
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

struct ApplicationSnapshot:Codable{
    var patches:PatchRestoreSnapshot
    var configuration:HotConfigurationStore
}
struct ApplicationTrialJournal:Codable{
    var id:String
    var before:ApplicationSnapshot
    var target:ApplicationRevision
    var expiresAt:Date
    var pending=true
    var outcome:String?
}
@MainActor enum ApplicationRevisionStore{
    static let journalName="application-trial.json"
    static func write(_ snapshot:ApplicationSnapshot,disk:Disk)throws{
        try PatchVersionStore.write(snapshot.patches,disk:disk)
        try disk.saveConfiguration(snapshot.configuration)
    }
    static func recover(disk:Disk)throws->ApplicationTrialJournal?{
        guard FileManager.default.fileExists(atPath:disk.root.appendingPathComponent(journalName).path) else{return nil}
        guard var trial=disk.load(ApplicationTrialJournal.self,journalName) else{throw AppError.message("Unreadable application trial journal")}
        guard trial.pending else{return trial}
        try write(trial.before,disk:disk)
        trial.pending=false;trial.outcome="recovered-on-launch"
        guard disk.save(trial,journalName) else{throw AppError.message("Cannot finalize application trial recovery")}
        return trial
    }
    static func retain(_ revision:ApplicationRevision,disk:Disk)throws{
        _ = try revision.checked()
        let name="application-revision-"+revision.id+".json"
        if FileManager.default.fileExists(atPath:disk.root.appendingPathComponent(name).path){
            guard disk.load(ApplicationRevision.self,name)==revision else{throw AppError.message("Immutable application revision collision")}
        }else if !disk.save(revision,name){throw AppError.message("Cannot retain application revision")}
    }
    static func begin(_ trial:ApplicationTrialJournal,after:ApplicationSnapshot,disk:Disk)throws{
        try retain(trial.target,disk:disk)
        guard disk.save(trial,journalName) else{throw AppError.message("Cannot journal application trial")}
        do{try write(after,disk:disk)}
        catch{_ = try recover(disk:disk);throw error}
    }
    // Native recovery preserves broken files for inspection; it does not erase
    // account settings, health history, receipts or archived source revisions.
    static func completeSafeResetIfNeeded(disk:Disk)throws{
        if disk.load(Bool.self,"native-safe-reset-complete.json")==true{return}
        try safeReset(disk:disk)
    }
    static func safeReset(disk:Disk)throws{
        let folder=disk.root.appendingPathComponent("native-recovery-"+UUID().uuidString,isDirectory:true)
        try FileManager.default.createDirectory(at:folder,withIntermediateDirectories:true)
        let names=["runtime-patch.json","jellyfin-api.json","dynamic-ui.json","hot-config.json","patch-restore.json","hot-config-trial.json","application-trial.json","hot-config-staged.json","device-campaign.json","decision-journal.json"]
        for name in names{
            let path=disk.root.appendingPathComponent(name)
            if FileManager.default.fileExists(atPath:path.path){try FileManager.default.copyItem(at:path,to:folder.appendingPathComponent(name))}
        }
        guard disk.save(false,"native-safe-reset-complete.json"),disk.save(true,"native-safe-mode.json") else{throw AppError.message("Cannot persist safe mode")}
        // A safe mode marker is written before touching any source file.
        for name in names{
            let path=disk.root.appendingPathComponent(name)
            if FileManager.default.fileExists(atPath:path.path){try FileManager.default.removeItem(at:path)}
        }
        try write(ApplicationSnapshot(patches:PatchRestoreSnapshot(runtime:RuntimePatchStore(),api:JellyfinAdapterStore(),ui:DynamicUIStore()),configuration:HotConfigurationStore()),disk:disk)
        guard disk.save(true,"native-safe-reset-complete.json") else{throw AppError.message("Cannot finalize safe mode")}
    }
}

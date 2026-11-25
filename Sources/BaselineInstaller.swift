import Foundation

@MainActor enum BaselineInstaller {
    static func install(_ baseline:ReleaseBaseline,disk:Disk)throws{
        let prior=disk.load(BaselineInstallState.self,"installed-baseline.json")
        if prior?.revision==baseline.revision && prior?.sources==baseline.sources{return}
        let runtime=disk.load(RuntimePatchStore.self,"runtime-patch.json")
        let api=disk.load(JellyfinAdapterStore.self,"jellyfin-api.json")
        let ui=disk.load(DynamicUIStore.self,"dynamic-ui.json")
        let oldRuntime=runtime?.active.map{PatchSource(id:$0.id,source:$0.source)}
        let oldAPI=api?.active.map{PatchSource(id:$0.id,source:$0.source,scope:$0.scope)}
        let selectedRuntime=BaselineMerge.select(local:oldRuntime,previous:prior?.sources.runtime,incoming:baseline.sources.runtime,hadLocal:runtime != nil,hadPrevious:prior != nil)
        let selectedAPI=BaselineMerge.select(local:oldAPI,previous:prior?.sources.jellyfinAPI,incoming:baseline.sources.jellyfinAPI,hadLocal:api != nil,hadPrevious:prior != nil)
        let selectedUI=BaselineMerge.select(local:ui?.active,previous:prior?.sources.ui,incoming:baseline.sources.ui,hadLocal:ui != nil,hadPrevious:prior != nil)
        // Keep interrupted activation journals untouched; their ordinary boot
        // recovery handles them. An upgrade must never erase a rollback marker.
        // Do not acknowledge migration until these journals have been resolved.
        if runtime?.activationPending==true || api?.pending==true{return}
        if runtime?.activationPending != true,selectedRuntime != oldRuntime || runtime==nil{
            let next=RuntimePatchStore(active:selectedRuntime.map{RuntimePatch(id:$0.id,source:$0.source)},previous:runtime?.active,activationPending:false)
            guard disk.save(next,"runtime-patch.json") else{throw AppError.message("Cannot install bundled runtime")}
        }
        if api?.pending != true,selectedAPI != oldAPI || api==nil{
            let next=JellyfinAdapterStore(active:selectedAPI.map{JellyfinAdapterRevision(id:$0.id,source:$0.source,scope:$0.scope)},previous:api?.active,pending:false)
            guard disk.save(next,"jellyfin-api.json") else{throw AppError.message("Cannot install bundled API")}
        }
        if selectedUI != ui?.active || ui==nil{
            let next=DynamicUIStore(active:selectedUI,previous:ui?.active,values:selectedUI?.retainedValues(ui?.values ?? [:]) ?? [:])
            guard disk.save(next,"dynamic-ui.json") else{throw AppError.message("Cannot install bundled UI")}
        }
        guard disk.save(BaselineInstallState(revision:baseline.revision,sources:baseline.sources),"installed-baseline.json") else{throw AppError.message("Cannot persist baseline migration")}
    }
}

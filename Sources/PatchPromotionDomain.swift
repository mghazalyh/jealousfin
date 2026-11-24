import Foundation

// Only executable sources and layout definitions, never device settings or form values.
struct PatchSource: Codable, Equatable {
    var id:String
    var source:String
    func checked()throws->Self{
        guard DynamicUILayout.validID(id),!source.isEmpty,source.utf8.count<=192*1024 else{
            throw AppError.message("Invalid patch source")
        }
        return self
    }
}
struct PatchSourceSet: Codable, Equatable {
    var runtime:PatchSource?
    var jellyfinAPI:PatchSource?
    var ui:DynamicUILayout?
    func checked()throws->Self{
        if let runtime=runtime{_ = try runtime.checked()}
        if let jellyfinAPI=jellyfinAPI{_ = try jellyfinAPI.checked()}
        if let ui=ui{_ = try ui.checked()}
        guard try JSONCodec.encoder().encode(self).count<=700*1024 else{throw AppError.message("Patch set too large")}
        return self
    }
}
struct PatchArchive:Codable,Equatable,Identifiable{
    var schemaVersion=1
    var id=UUID().uuidString
    var deviceID:String
    var recordedAt=Date()
    var reason:String
    var build:String
    var sources:PatchSourceSet
}
struct ReleaseBaseline:Codable,Equatable{
    var schemaVersion=1
    var revision:String
    var archiveID:String?
    var deviceID:String?
    var evidence:String?
    var sources:PatchSourceSet
    func checked()throws->Self{
        guard schemaVersion==1,DynamicUILayout.validID(revision),
              archiveID.map(DynamicUILayout.validID) ?? true,
              deviceID.map(DynamicUILayout.validID) ?? true,
              (evidence?.count ?? 0)<=4000 else{throw AppError.message("Invalid release baseline")}
        _ = try sources.checked()
        return self
    }
    static let empty=ReleaseBaseline(revision:"native-baseline",sources:PatchSourceSet())
}
struct BaselineInstallState:Codable{
    var revision:String
    var sources:PatchSourceSet
}
enum BaselineMerge{
    // An upgrade updates components still following the previous bundled release.
    // An independently installed local patch is retained rather than overwritten.
    static func select<T:Equatable>(local:T?,previous:T?,incoming:T?,hadLocal:Bool,hadPrevious:Bool)->T?{
        if !hadLocal{return incoming}
        if hadPrevious && local==previous{return incoming}
        if !hadPrevious && local==nil{return incoming}
        return local
    }
}

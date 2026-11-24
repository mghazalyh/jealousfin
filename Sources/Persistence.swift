import Foundation

@MainActor final class Disk {
    let root: URL
    private(set) var error: String?
    init() {
        root = FileManager.default.urls(for:.applicationSupportDirectory,in:.userDomainMask).first!.appendingPathComponent("Jealousfin",isDirectory:true)
        do { try FileManager.default.createDirectory(at:root,withIntermediateDirectories:true) } catch { self.error="Cannot create app storage" }
    }
    func load<T: Decodable>(_ type:T.Type,_ name:String) -> T? {
        let url=root.appendingPathComponent(name)
        guard FileManager.default.fileExists(atPath:url.path) else { return nil }
        do { return try JSONCodec.decoder().decode(type,from:Data(contentsOf:url)) }
        catch { self.error="Cannot read \(name)"; return nil }
    }
    @discardableResult func save<T:Encodable>(_ object:T,_ name:String) -> Bool {
        do { try JSONCodec.encoder().encode(object).write(to:root.appendingPathComponent(name),options:.atomic); return true }
        catch { self.error="Cannot write \(name)"; return false }
    }
    func files(prefix:String) -> [URL] {
        let files=(try? FileManager.default.contentsOfDirectory(at:root,includingPropertiesForKeys:nil)) ?? []
        return files.filter{$0.lastPathComponent.hasPrefix(prefix) && $0.pathExtension=="json"}.sorted{$0.lastPathComponent<$1.lastPathComponent}
    }
    func remove(_ url:URL) { do { try FileManager.default.removeItem(at:url) } catch { self.error="Cannot remove uploaded batch" } }
}
struct GitFile {
    var sha: String
    var data: Data
}
@MainActor final class TorrioDB {
    let repository = "https://api.github.com/repos/mghazalyh/torriodb/"
    var base:String {repository+"contents/jealousfin/"}
    var token = ""
    var configured:Bool { !token.isEmpty }
    func call(_ path:String, method:String="GET", body:[String:Any]?=nil,gitTree:Bool=false) async throws -> (Data,Int) {
        guard configured, let u=URL(string:(gitTree ? repository+"git/trees/" : base)+path) else { throw AppError.message("Configure TorrioDB token") }
        var req=URLRequest(url:u); req.httpMethod=method; req.timeoutInterval=20
        req.setValue("Bearer "+token,forHTTPHeaderField:"Authorization")
        req.setValue("application/vnd.github+json",forHTTPHeaderField:"Accept")
        req.setValue("2022-11-28",forHTTPHeaderField:"X-GitHub-Api-Version")
        if let body=body { req.httpBody=try JSONSerialization.data(withJSONObject:body); req.setValue("application/json",forHTTPHeaderField:"Content-Type") }
        let (d,r)=try await URLSession.shared.data(for:req)
        guard let h=r as? HTTPURLResponse else { throw AppError.message("Invalid GitHub response") }
        return (d,h.statusCode)
    }
    func read(_ path:String) async throws -> GitFile? {
        let (d,status)=try await call(path)
        if status==404 { return nil }
        guard status==200, let j=try JSONSerialization.jsonObject(with:d) as? [String:Any], let sha=j["sha"] as? String, let c=j["content"] as? String, let bytes=Data(base64Encoded:c,options:.ignoreUnknownCharacters) else { throw AppError.message("TorrioDB read HTTP \(status)") }
        return GitFile(sha:sha,data:bytes)
    }
    func put(_ path:String, data:Data, sha:String?=nil) async throws {
        var b:[String:Any]=["message":"jealousfin: synchronize \(path)","content":data.base64EncodedString(),"branch":"main"]
        if let sha=sha { b["sha"]=sha }
        let (_,status)=try await call(path,method:"PUT",body:b)
        guard status==200 || status==201 else { throw AppError.message("TorrioDB write HTTP \(status)") }
    }
    func immutable(_ path:String,data:Data) async throws {
        if let existing=try await read(path) {
            guard existing.data==data else { throw AppError.message("Immutable record collision") }; return
        }
        try await put(path,data:data)
    }
    func directory(_ path:String) async throws -> [String] {
        let (d,status)=try await call(path)
        if status==404 { return [] }
        guard status==200, let a=try JSONSerialization.jsonObject(with:d) as? [[String:Any]] else { throw AppError.message("TorrioDB listing HTTP \(status)") }
        var names=a.filter{$0["type"] as? String=="file"}.compactMap{$0["name"] as? String}
        // Contents API caps directory listings at 1000 entries. Resolve its tree
        // instead of silently losing later commands/receipts after long operation.
        if a.count>=1000 {
            let parent=(path as NSString).deletingLastPathComponent
            let name=(path as NSString).lastPathComponent
            let (pd,ps)=try await call(parent)
            guard ps==200,let children=try JSONSerialization.jsonObject(with:pd) as? [[String:Any]],
                  let sha=children.first(where:{$0["name"] as? String==name && $0["type"] as? String=="dir"})?["sha"] as? String,
                  sha.range(of:"^[0-9a-f]{40}$",options:.regularExpression) != nil else{throw AppError.message("Cannot resolve complete command directory")}
            let (td,ts)=try await call(sha,gitTree:true)
            guard ts==200,let tree=try JSONSerialization.jsonObject(with:td) as? [String:Any],tree["truncated"] as? Bool==false,
                  let entries=tree["tree"] as? [[String:Any]] else{throw AppError.message("Incomplete Git tree")}
            names=entries.filter{$0["type"] as? String=="blob"}.compactMap{$0["path"] as? String}
        }
        return names.filter{$0.hasSuffix(".json") && $0.range(of:"^[A-Za-z0-9_.-]+$",options:.regularExpression) != nil}
    }
    func merge(_ local:HealthLedger) async throws -> HealthLedger {
        let path="database/channel-health-v1.json"
        let file=try await read(path)
        var merged=local
        if let file=file { merged.merge(try JSONCodec.decoder().decode(HealthLedger.self,from:file.data)) }
        merged.events.sort { $0.at == $1.at ? $0.id < $1.id : $0.at < $1.at }
        let bytes=try JSONCodec.encoder().encode(merged)
        if file?.data != bytes { try await put(path,data:bytes,sha:file?.sha) }
        return merged
    }
}

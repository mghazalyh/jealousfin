import Foundation

@MainActor final class Disk {
    let root: URL
    private(set) var error: String?
    init(root override:URL? = nil) {
        root = override ?? FileManager.default.urls(for:.applicationSupportDirectory,in:.userDomainMask).first!.appendingPathComponent("Jealousfin",isDirectory:true)
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
private struct TorrioWriteFailure:LocalizedError {
    let status:Int
    var errorDescription:String?{"JealousDB write HTTP \(status)"}
}
private final class DatabaseNoRedirect:NSObject,URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping (URLRequest?)->Void){completionHandler(nil)}
}
@MainActor final class TorrioDB {
    private let transport:URLSession
    let repositoryAddress:String?
    private let repository:String?
    init(address:String,transport:URLSession = .shared){
        self.transport=transport
        self.repositoryAddress=GitHubRepositoryAddress.canonical(address)
        self.repository=GitHubRepositoryAddress.apiRoot(address)
    }
    private let writeAttempts=4
    private func conflictBackoff(_ attempt:Int) async throws {
        try await Task.sleep(nanoseconds:UInt64(250_000_000 * (1 << attempt)))
    }
    var base:String? {repository.map{$0+"contents/jealousfin/"}}
    var token = ""
    var configured:Bool { repository != nil && !token.isEmpty }
    func call(_ path:String, method:String="GET", body:[String:Any]?=nil,gitTree:Bool=false) async throws -> (Data,Int) {
        guard let repository=repository,let base=base else{throw AppError.message("Invalid GitHub repository address")}
        guard !token.isEmpty else{throw AppError.message("Configure GitHub repository token")}
        guard var components=URLComponents(string:(gitTree ? repository+"git/trees/" : base)+path),
              components.scheme=="https",components.host=="api.github.com",components.fragment==nil,
              components.query==nil else{throw AppError.message("Invalid database request path")}
        if method=="GET" && !gitTree{components.queryItems=[URLQueryItem(name:"ref",value:"main")]}
        guard let u=components.url else{throw AppError.message("Invalid database request URL")}
        var req=URLRequest(url:u); req.httpMethod=method; req.timeoutInterval=20
        req.setValue("Bearer "+token,forHTTPHeaderField:"Authorization")
        req.setValue("application/vnd.github+json",forHTTPHeaderField:"Accept")
        req.setValue("2022-11-28",forHTTPHeaderField:"X-GitHub-Api-Version")
        if let body=body { req.httpBody=try JSONSerialization.data(withJSONObject:body); req.setValue("application/json",forHTTPHeaderField:"Content-Type") }
        let (d,r)=try await transport.data(for:req,delegate:DatabaseNoRedirect())
        guard let h=r as? HTTPURLResponse else { throw AppError.message("Invalid GitHub response") }
        return (d,h.statusCode)
    }
    func read(_ path:String) async throws -> GitFile? {
        let (d,status)=try await call(path)
        if status==404 { return nil }
        guard status==200, let j=try JSONSerialization.jsonObject(with:d) as? [String:Any], let sha=j["sha"] as? String, let c=j["content"] as? String, let bytes=Data(base64Encoded:c,options:.ignoreUnknownCharacters) else { throw AppError.message("JealousDB read HTTP \(status)") }
        return GitFile(sha:sha,data:bytes)
    }
    private func writeOnce(_ path:String,data:Data,sha:String?) async throws {
        var b:[String:Any]=["message":"jealousfin: synchronize \(path)","content":data.base64EncodedString(),"branch":"main"]
        if let sha=sha {b["sha"]=sha}
        let (_,status)=try await call(path,method:"PUT",body:b)
        guard status==200 || status==201 else{throw TorrioWriteFailure(status:status)}
    }
    // Preserve compare-and-swap semantics for reviewed baseline promotion.
    // A branch-head conflict can be retried only while the target blob is unchanged.
    func put(_ path:String,data:Data,sha:String?=nil) async throws {
        for attempt in 0..<writeAttempts {
            do{try await writeOnce(path,data:data,sha:sha);return}
            catch let error as TorrioWriteFailure where error.status==409 {
                let current=try await read(path)
                if current?.data==data{return}
                guard current?.sha==sha,attempt+1<writeAttempts else{throw error}
                try await conflictBackoff(attempt)
            }
        }
    }
    // For device-owned mutable observations only, re-read the current blob
    // and replace it. Never use this operation for immutable data or promotion.
    func replaceDeviceSnapshot(_ path:String,data:Data) async throws {
        for attempt in 0..<writeAttempts {
            let current=try await read(path)
            if current?.data==data{return}
            do{try await writeOnce(path,data:data,sha:current?.sha);return}
            catch let error as TorrioWriteFailure where error.status==409 && attempt+1<writeAttempts {
                try await conflictBackoff(attempt)
            }
        }
    }
    func immutable(_ path:String,data:Data) async throws {
        for attempt in 0..<writeAttempts {
            if let current=try await read(path){
                guard current.data==data else{throw AppError.message("Immutable record collision")}
                return
            }
            do{try await writeOnce(path,data:data,sha:nil);return}
            catch let error as TorrioWriteFailure where error.status==409 {
                if let current=try await read(path){
                    guard current.data==data else{throw AppError.message("Immutable record collision")}
                    return
                }
                guard attempt+1<writeAttempts else{throw error}
                try await conflictBackoff(attempt)
            }
        }
    }
    func directory(_ path:String) async throws -> [String] {
        let (d,status)=try await call(path)
        if status==404 { return [] }
        guard status==200, let a=try JSONSerialization.jsonObject(with:d) as? [[String:Any]] else { throw AppError.message("JealousDB listing HTTP \(status)") }
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
        for attempt in 0..<writeAttempts {
            let file=try await read(path)
            var merged=local
            if let file=file{merged.merge(try JSONCodec.decoder().decode(HealthLedger.self,from:file.data))}
            merged.events.sort{$0.at == $1.at ? $0.id < $1.id : $0.at < $1.at}
            let bytes=try JSONCodec.encoder().encode(merged)
            if file?.data==bytes{return merged}
            do{try await writeOnce(path,data:bytes,sha:file?.sha);return merged}
            catch let error as TorrioWriteFailure where error.status==409 && attempt+1<writeAttempts {
                try await conflictBackoff(attempt)
            }
        }
        throw TorrioWriteFailure(status:409)
    }
}

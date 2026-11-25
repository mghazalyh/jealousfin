import Foundation

struct JellyfinAdapterSpec:Codable,Equatable{
    var usernameKey="Username"
    var passwordKey="Pw"
    var tokenPath="AccessToken"
    var userIDPath="User.Id"
    var authorizationHeader="Authorization"
    var logoPath="Items/{id}/Images/Primary"
    var timeoutSeconds=20
    func checked()throws->Self{
        let key="^[A-Za-z_][A-Za-z0-9_]{0,79}$"
        let path="^[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*){0,5}$"
        guard usernameKey != passwordKey,
              [usernameKey,passwordKey].allSatisfy({$0.range(of:key,options:.regularExpression) != nil}),
              [tokenPath,userIDPath].allSatisfy({$0.range(of:path,options:.regularExpression) != nil}),
              ["Authorization","X-Emby-Authorization","both"].contains(authorizationHeader),
              logoPath.contains("{id}"),JellyfinAdapterRules.validPath(logoPath.replacingOccurrences(of:"{id}",with:"fixture")),
              (5...90).contains(timeoutSeconds)
        else{throw AppError.message("Invalid Jellyfin adapter configuration")}
        return self
    }
}
struct JellyfinAdapterRevision:Codable,Equatable{
    var id:String
    var source:String
    var installedAt=Date()
    var scope:String?=nil
    var catalogueOnly:Bool{scope=="catalogue"}
}
struct DeferredAPIActivation:Codable{
    let candidate:JellyfinAdapterRevision
    let expectedRevision:String
    let mode:String
    func checked()throws->Self{
        guard ["when-idle","next-channel","next-cycle"].contains(mode),JellyfinAdapterRules.validRevisionID(candidate.id),
              JellyfinAdapterRules.validRevisionID(expectedRevision),candidate.id != expectedRevision,!candidate.source.isEmpty,
              candidate.source.utf8.count<=192*1024,candidate.scope==nil || candidate.catalogueOnly else{throw AppError.message("Invalid deferred adapter")}
        return self
    }
}
struct JellyfinAdapterStore:Codable{
    var active:JellyfinAdapterRevision?
    var previous:JellyfinAdapterRevision?
    var pending=false
}
enum JellyfinAdapterRules{
    // Keep the adapter policy independent of SwiftUI domain types so the
    // standalone HLS readiness test compiles with only the network domain.
    static func validRevisionID(_ value:String)->Bool{
        value.range(of:"^[A-Za-z0-9_-]{1,80}$",options:.regularExpression) != nil
    }
    static let operations=["authenticate","channels","programs","favorite","playback_info","session_start","session_progress","session_stop","close_stream","health","record","recordings","other"]
    static func operation(_ path:String)->String{
        if path=="Users/AuthenticateByName"{return "authenticate"}
        if path=="LiveTv/Channels"{return "channels"}
        if path=="LiveTv/Programs"{return "programs"}
        if path.contains("/FavoriteItems/"){return "favorite"}
        if path.hasSuffix("/PlaybackInfo"){return "playback_info"}
        if path=="Sessions/Playing"{return "session_start"}
        if path=="Sessions/Playing/Progress"{return "session_progress"}
        if path=="Sessions/Playing/Stopped"{return "session_stop"}
        if path=="LiveStreams/Close"{return "close_stream"}
        if path=="System/Info/Public"{return "health"}
        if path=="LiveTv/Timers"{return "record"}
        if path=="LiveTv/Recordings"{return "recordings"}
        return "other"
    }
    static func validPath(_ path:String)->Bool{
        let parts=path.split(separator:"/",omittingEmptySubsequences:false)
        return !path.isEmpty && path.utf8.count<=2048 && !parts.isEmpty &&
            parts.allSatisfy{!$0.isEmpty && $0 != "." && $0 != ".." && $0.range(of:"^[A-Za-z0-9_.~-]+$",options:.regularExpression) != nil}
    }
    static func string(at path:String,in object:[String:Any])->String?{
        var value:Any=object
        for key in path.split(separator:"."){
            guard let dictionary=value as? [String:Any],let next=dictionary[String(key)] else{return nil}
            value=next
        }
        return value as? String
    }
    static func sameOrigin(_ url:URL,_ base:URL)->Bool{
        func port(_ u:URL)->Int?{u.port ?? (u.scheme?.lowercased()=="https" ? 443 : u.scheme?.lowercased()=="http" ? 80:nil)}
        return ["http","https"].contains(url.scheme?.lowercased() ?? "") &&
            url.scheme?.lowercased()==base.scheme?.lowercased() &&
            url.host?.lowercased()==base.host?.lowercased() && url.host != nil &&
            port(url)==port(base) && url.user==nil && url.password==nil
    }
    static func request(_ candidate:[String:Any],authenticate:Bool)throws->(path:String,method:String,query:[String:String],body:[String:Any]?){
        guard let path=candidate["path"] as? String,validPath(path),
            let method=candidate["method"] as? String,["GET","POST","PUT","DELETE","PATCH"].contains(method),
            let query=candidate["query"] as? [String:String],query.count<=100,
            query.allSatisfy({$0.key.utf8.count<=128 && $0.value.utf8.count<=8192}),
            !authenticate || method=="POST" else{throw AppError.message("Invalid patched Jellyfin request")}
        let raw=candidate["body"]
        let body=raw as? [String:Any]
        guard raw==nil || raw is NSNull || body != nil else{throw AppError.message("Invalid patched request body")}
        if let body=body{
            guard JSONSerialization.isValidJSONObject(body),try JSONSerialization.data(withJSONObject:body).count<=256*1024 else{throw AppError.message("Patched request body too large")}
        }
        return(path,method,query,body)
    }
}

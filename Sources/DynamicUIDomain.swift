import Foundation

struct DynamicUIAction: Codable, Equatable {
    var command:String
    var arguments:[String:String]?
}
struct DynamicUIOption: Codable, Equatable, Identifiable {
    var value:String
    var title:String
    var id:String {value}
}
struct DynamicUINode: Codable, Equatable, Identifiable {
    var id:String
    var kind:String
    var title:String?
    var detail:String?
    var symbol:String?
    var style:String?
    var valueKey:String?
    var defaultValue:String?
    var minimum:Double?
    var maximum:Double?
    var step:Double?
    var options:[DynamicUIOption]?
    var action:DynamicUIAction?
    var targetPage:String?
    var children:[DynamicUINode]?
    var visibleWhen:String?
    var enabledWhen:String?
}
struct DynamicUISlot: Codable, Equatable, Identifiable {
    var location:String
    var nodes:[DynamicUINode]
    var id:String {location}
}
struct DynamicUIPage: Codable, Equatable, Identifiable {
    var id:String
    var title:String
    var symbol:String?
    var nodes:[DynamicUINode]
}
struct DynamicUILayout: Codable, Equatable {
    var schemaVersion:Int
    var revision:String
    var slots:[DynamicUISlot]
    var pages:[DynamicUIPage]
    static let locations=["guide.top","guide.bottom","player.top","player.controls","player.bottom","health.top","health.bottom","insights.top","insights.bottom","more.top","more.bottom","settings.top","settings.bottom","telemetry.top","telemetry.bottom","runtime.top","runtime.bottom","tests.top","tests.bottom","logs.top","logs.bottom","epg.top","epg.bottom"]
    static let kinds=["text","metric","button","input","toggle","picker","slider","progress","card","vstack","hstack","divider","link","spacer"]
    static let commands:Set<String>=["get_status","get_telemetry","get_insights","get_logs","get_version","get_capabilities","list_channels","get_channel_health","list_banned","list_hiatus","open_channel","open_server_surf","play","pause","resume","stop","next_channel","previous_channel","start_surf","stop_surf","pause_surf","resume_surf","set_rotation_minutes","favorite_current","favorite_previous","sync_now","reconnect","start_device_tests","pause_device_tests","resume_device_tests","stop_device_tests","get_device_tests","get_runtime_module","script"]
    static func validID(_ value:String)->Bool {
        value.range(of:"^[A-Za-z0-9_-]{1,80}$",options:.regularExpression) != nil
    }
    static func validBinding(_ value:String)->Bool {
        value.range(of:"^(state|form)\\.[A-Za-z0-9_-]+(\\.[A-Za-z0-9_-]+){0,5}$",options:.regularExpression) != nil
    }
    func checked() throws -> DynamicUILayout {
        guard schemaVersion==1,Self.validID(revision),slots.count<=Self.locations.count,pages.count<=12,
            Set(slots.map(\.location)).count==slots.count,Set(pages.map(\.id)).count==pages.count,
            slots.allSatisfy({Self.locations.contains($0.location)}),
            pages.allSatisfy({Self.validID($0.id) && !$0.title.isEmpty && $0.title.count<=100})
        else{throw AppError.message("Invalid UI layout header, slots or pages")}
        var seen=Set<String>();var fields=Set<String>();var count=0
        let pageIDs=Set(pages.map(\.id))
        func walk(_ nodes:[DynamicUINode],depth:Int)throws{
            guard depth<=7,nodes.count<=60 else{throw AppError.message("UI tree exceeds supported depth or width")}
            for n in nodes{
                count+=1
                guard count<=250,Self.validID(n.id),seen.insert(n.id).inserted,Self.kinds.contains(n.kind),
                    (n.title?.count ?? 0)<=512,(n.detail?.count ?? 0)<=2048,(n.symbol?.count ?? 0)<=100,
                    ["normal","headline","caption","prominent","destructive","muted"].contains(n.style ?? "normal"),
                    (n.defaultValue?.utf8.count ?? 0)<=4096
                else{throw AppError.message("Invalid or duplicate UI node \(n.id)")}
                for binding in [n.valueKey,n.visibleWhen,n.enabledWhen].compactMap({$0}){
                    guard Self.validBinding(binding) else{throw AppError.message("Invalid UI binding \(binding)")}
                }
                let editable=["input","toggle","picker","slider"].contains(n.kind)
                if editable {
                    guard let key=n.valueKey,key.hasPrefix("form."),key.split(separator:".").count==2 else{throw AppError.message("Editable controls require a form key")}
                    fields.insert(key)
                    guard fields.count<=64 else{throw AppError.message("Too many form fields")}
                }
                if n.kind=="picker" {
                    let values=n.options ?? []
                    guard !values.isEmpty,values.count<=40,Set(values.map(\.value)).count==values.count,
                        values.allSatisfy({$0.value.utf8.count<=512 && !$0.title.isEmpty && $0.title.count<=100}),
                        n.defaultValue.map({v in values.contains(where:{$0.value==v})}) ?? true
                    else{throw AppError.message("Invalid picker options")}
                }
                if n.kind=="slider" || n.kind=="progress"{
                    let lo=n.minimum ?? 0,hi=n.maximum ?? 100,step=n.step ?? 1
                    guard lo.isFinite,hi.isFinite,step.isFinite,hi>lo,hi-lo<=1e9,step>0,step<=hi-lo,
                        abs(lo)<=1e9,abs(hi)<=1e9 else{throw AppError.message("Invalid numeric range")}
                }
                if n.kind=="link" {
                    guard let target=n.targetPage,pageIDs.contains(target) else{throw AppError.message("Unknown custom page")}
                }
                if let action=n.action{
                    guard n.kind=="button",Self.commands.contains(action.command),(action.arguments?.count ?? 0)<=32,
                          (action.arguments ?? [:]).allSatisfy({$0.key.count<=128 && $0.value.utf8.count<=65536})
                    else{throw AppError.message("Invalid UI action")}
                }
                if n.kind=="button",n.action==nil {throw AppError.message("Button requires an action")}
                if let children=n.children,!children.isEmpty{
                    guard ["card","vstack","hstack"].contains(n.kind) else{throw AppError.message("Only containers may have children")}
                    try walk(children,depth:depth+1)
                }
            }
        }
        for slot in slots{try walk(slot.nodes,depth:0)}
        for page in pages{try walk(page.nodes,depth:0)}
        return self
    }
    func node(_ id:String)->DynamicUINode?{
        func find(_ nodes:[DynamicUINode])->DynamicUINode?{
            for n in nodes{
                if n.id==id{return n}
                if let child=find(n.children ?? []){return child}
            }
            return nil
        }
        for s in slots{if let n=find(s.nodes){return n}}
        for p in pages{if let n=find(p.nodes){return n}}
        return nil
    }
    func formDefaults()->[String:String]{
        var result:[String:String]=[:]
        func visit(_ nodes:[DynamicUINode]){
            for n in nodes{
                if ["input","toggle","picker","slider"].contains(n.kind),let key=n.valueKey {
                    result[String(key.dropFirst(5))]=n.defaultValue ?? (n.kind=="toggle" ? "false":n.kind=="picker" ? n.options?.first?.value ?? "":n.kind=="slider" ? String(n.minimum ?? 0):"")
                }
                visit(n.children ?? [])
            }
        }
        slots.forEach{visit($0.nodes)};pages.forEach{visit($0.nodes)}
        return result
    }
}
struct DynamicUIStore: Codable {
    var active:DynamicUILayout?
    var previous:DynamicUILayout?
    var values:[String:String] = [:]
}
enum DynamicUIBinding {
    static func resolve(_ key:String,values:[String:String],state:[String:Any])->String?{
        guard DynamicUILayout.validBinding(key) else{return nil}
        let parts=key.split(separator:".").map(String.init)
        if parts.first=="form" {return values[parts.dropFirst().joined(separator:".")]}
        var item:Any=state
        for component in parts.dropFirst(){
            guard let map=item as? [String:Any],let child=map[component] else{return nil}
            item=child
        }
        if let s=item as? String{return s}
        if let n=item as? NSNumber{return n.stringValue}
        return nil
    }
    static func arguments(_ args:[String:String],values:[String:String],state:[String:Any])throws->[String:String]{
        var result:[String:String]=[:]
        for (key,value) in args{
            if value.hasPrefix("$form.") || value.hasPrefix("$state.") {
                guard let found=resolve(String(value.dropFirst()),values:values,state:state) else{throw AppError.message("Missing UI argument binding \(value)")}
                result[key]=found
            }else{result[key]=value}
        }
        return result
    }
    static func truth(_ value:String?)->Bool{
        guard let value=value?.lowercased() else{return false}
        return ["true","1","yes","on"].contains(value)
    }
}

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
    var html:String?
    var height:Double?
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
// Versioned with the layout, so rollback restores both content and appearance.
struct DynamicPresentation: Codable, Equatable {
    var videoGravity:String? = nil
    var fullscreenOpacity:Double? = nil
    var fullscreenMaxHeight:Double? = nil
    var fullscreenInitiallyVisible:Bool? = nil
    var nativePlaybackControls:Bool? = nil
    var pictureInPictureAllowed:Bool? = nil
    var automaticPictureInPicture:Bool? = nil
    var fullscreenWidth:Double?=nil
    var fullscreenPadding:Double?=nil
    var fullscreenSpacing:Double?=nil
    var fullscreenCornerRadius:Double?=nil
    var fullscreenButtonHeight:Double?=nil
    var fullscreenTextScale:Double?=nil
    var fullscreenAutoHideSeconds:Int?=nil
    var fullscreenTapToReveal:Bool?=nil
    var fullscreenFitContent:Bool?=nil
    var fullscreenPortraitMaxHeight:Double?=nil
    var fullscreenControlOrder:[String]?=nil
    var fullscreenControlTheme:String?=nil
    // The range is hot-configurable; absolute rendering bounds remain native.
    var videoBrightnessMinimum:Int?=nil
    var videoBrightnessMaximum:Int?=nil
    var resolvedVideoBrightnessRange:ClosedRange<Int>{
        let low=min(0,max(-25,videoBrightnessMinimum ?? -20))
        let high=max(0,min(20,videoBrightnessMaximum ?? 10))
        return low...high
    }
    func clampedVideoBrightness(_ value:Int)->Int{
        min(resolvedVideoBrightnessRange.upperBound,max(resolvedVideoBrightnessRange.lowerBound,value))
    }
    static func videoBrightnessOpacity(_ value:Int)->Double{
        Double(abs(min(20,max(-25,value))))*0.035
    }
    var resolvedFullscreenControlTheme:String{fullscreenControlTheme ?? "night"}
    var fullscreenIconOnly:Bool?=nil
    var fullscreenIconSize:Double?=nil
    static let legacyControlOrder=["rotationToggle","playPause","next","stop","favorite"]
    static let defaultControlOrder=["rotationToggle","previous","playPause","next","stop","favorite"]
    var resolvedFullscreenControlOrder:[String]{
        var order=fullscreenControlOrder ?? Self.defaultControlOrder
        if !order.contains("previous"){order.insert("previous",at:order.firstIndex(of:"playPause") ?? order.startIndex)}
        return order
    }
    func checked()throws->Self{
        guard ["fit","fill","stretch"].contains(videoGravity ?? "fit"),
              ["night","standard"].contains(resolvedFullscreenControlTheme),
              (-25 ... -1).contains(videoBrightnessMinimum ?? -20),
              (1 ... 20).contains(videoBrightnessMaximum ?? 10),
              (0.15...1).contains(fullscreenOpacity ?? 0.65),
              (0.20...0.80).contains(fullscreenMaxHeight ?? 0.25),
              (0.35...1).contains(fullscreenWidth ?? 1),
              (0...32).contains(fullscreenPadding ?? 8),(0...24).contains(fullscreenSpacing ?? 6),
              (0...32).contains(fullscreenCornerRadius ?? 14),(44...80).contains(fullscreenButtonHeight ?? 44),
              (0.8...1.4).contains(fullscreenTextScale ?? 1),(0...600).contains(fullscreenAutoHideSeconds ?? 0),
              (0.20...0.80).contains(fullscreenPortraitMaxHeight ?? 0.25),
              (18...36).contains(fullscreenIconSize ?? 29),
              fullscreenControlOrder.map({($0.count==6 && Set($0)==Set(Self.defaultControlOrder)) || ($0.count==5 && Set($0)==Set(Self.legacyControlOrder))}) ?? true
        else{throw AppError.message("Invalid presentation options")}
        return self
    }
}
// The state is derived from the last confirmed native catalogue value, not
// optimistically inverted by a tap. It is also available to web and runtime UI.
struct FullscreenFavoriteState:Equatable{
    let favorite:Bool?
    let pending:Bool
    var title:String{
        if pending{return "Saving…"}
        guard let favorite=favorite else{return "Favorite unavailable"}
        return favorite ? "Unfavorite":"Favorite"
    }
    var symbol:String{favorite==true ? "heart.fill":"heart"}
    var enabled:Bool{favorite != nil && !pending}
    var selected:Bool{favorite==true}
}
struct DynamicTab:Codable,Equatable,Identifiable{
    let id:Int
    let title:String
    let symbol:String
    var visible:Bool=true
    static let defaults=[DynamicTab(id:0,title:"Guide",symbol:"tv"),DynamicTab(id:1,title:"Player",symbol:"play.rectangle"),DynamicTab(id:2,title:"Health",symbol:"waveform.path.ecg"),DynamicTab(id:3,title:"Insights",symbol:"lightbulb"),DynamicTab(id:4,title:"More",symbol:"ellipsis.circle")]
    static func checked(_ tabs:[Self])throws{
        guard tabs.count==5,Set(tabs.map(\.id))==Set(0...4),
              tabs.allSatisfy({!$0.title.isEmpty && $0.title.utf8.count<=60 && !$0.symbol.isEmpty && $0.symbol.utf8.count<=100}),
              tabs.first(where:{$0.id==4})?.visible==true else{throw AppError.message("Tab layout must retain the native recovery tab")}
    }
}
struct DynamicUILayout: Codable, Equatable {
    var schemaVersion:Int
    var revision:String
    var slots:[DynamicUISlot]
    var pages:[DynamicUIPage]
    var presentation:DynamicPresentation? = nil
    var tabs:[DynamicTab]?=nil
    // Parent replacements retain ABI compatibility and take precedence over their
    // native child slots. Empty slots still fall back to the native implementation.
    static let fullscreenComponentLocations=["showControls","hideControls","exit","programmeTitle","countdown","rotationToggle","previous","playPause","next","stop","favorite","category","surfToggle","brightness","favoriteError","sharedHint"].map{"player.fullscreen."+$0}
    static let replacementLocations=["guide.body","health.body","insights.body","more.body","player.transport","player.surf","player.history","player.fullscreen.header","player.fullscreen.heading","player.fullscreen.transport","player.fullscreen.surf","player.fullscreen.footer"]+fullscreenComponentLocations
    static let locations=replacementLocations+["guide.top","guide.bottom","player.top","player.controls","player.bottom","player.fullscreen.top","player.fullscreen.controls","player.fullscreen.bottom","health.top","health.bottom","insights.top","insights.bottom","more.top","more.bottom","settings.top","settings.bottom","telemetry.top","telemetry.bottom","runtime.top","runtime.bottom","tests.top","tests.bottom","logs.top","logs.bottom","epg.top","epg.bottom"]
    static let kinds=["segmented","menu","disclosure","scroll","searchList","table","barChart","banner","liveBrightness","text","metric","button","input","toggle","picker","slider","progress","card","vstack","hstack","divider","link","spacer","web"]
    static let commands:Set<String>=["query_native_data","get_effective_configuration","get_capability_registry","list_event_modules","get_media_tracks","select_media_track","seek_live","seek_relative","reload_current_stream","get_hot_configuration","get_native_capabilities","get_preferences","set_preferences","set_player_audio","set_video_brightness","navigate","refresh_catalogue","probe_active_hls","set_fullscreen","set_fullscreen_controls","get_player_snapshot","get_status","get_telemetry","get_insights","get_logs","get_version","get_capabilities","list_channels","get_channel_health","list_banned","list_hiatus","open_channel","open_server_surf","play","pause","resume","stop","next_channel","previous_channel","start_surf","stop_surf","pause_surf","resume_surf","set_rotation_minutes","favorite_current","favorite_previous","sync_now","reconnect","start_device_tests","pause_device_tests","resume_device_tests","stop_device_tests","get_device_tests","get_runtime_module","script"]
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
        _ = try presentation?.checked()
        if let tabs=tabs{try DynamicTab.checked(tabs)}
        var seen=Set<String>();var fields=Set<String>();var count=0
        let pageIDs=Set(pages.map(\.id))
        func walk(_ nodes:[DynamicUINode],depth:Int)throws{
            guard depth<=7,nodes.count<=60 else{throw AppError.message("UI tree exceeds supported depth or width")}
            for n in nodes{
                count+=1
                guard count<=250,Self.validID(n.id),seen.insert(n.id).inserted,Self.kinds.contains(n.kind),
                    (n.title?.count ?? 0)<=512,(n.detail?.count ?? 0)<=2048,(n.symbol?.count ?? 0)<=100,
                    ["normal","headline","caption","programme","prominent","destructive","muted"].contains(n.style ?? "normal"),
                    (n.defaultValue?.utf8.count ?? 0)<=4096
                else{throw AppError.message("Invalid or duplicate UI node \(n.id)")}
                for binding in [n.valueKey,n.visibleWhen,n.enabledWhen].compactMap({$0}){
                    guard Self.validBinding(binding) else{throw AppError.message("Invalid UI binding \(binding)")}
                }
                let editable=["input","toggle","picker","slider","segmented","menu"].contains(n.kind)
                if editable {
                    guard let key=n.valueKey,key.hasPrefix("form."),key.split(separator:".").count==2 else{throw AppError.message("Editable controls require a form key")}
                    guard fields.insert(key).inserted else{throw AppError.message("Duplicate editable form field")}
                    guard fields.count<=64 else{throw AppError.message("Too many form fields")}
                }
                if ["picker","segmented","menu"].contains(n.kind) {
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
                if n.kind=="liveBrightness"{
                    guard n.action==nil,n.valueKey==nil,n.children==nil,n.minimum==nil,n.maximum==nil,n.step==nil,
                          !fields.contains("native.brightness") else{throw AppError.message("Only one fixed native brightness control is allowed")}
                    fields.insert("native.brightness")
                }
                if n.kind=="web" {
                    guard let html=n.html,!html.isEmpty,html.utf8.count<=65536,
                        let height=n.height,height.isFinite,(80...1200).contains(height)
                    else{throw AppError.message("Invalid custom HTML component")}
                }else if n.html != nil || n.height != nil{throw AppError.message("HTML and height belong to web components")}
                if editable,let initial=n.defaultValue {try Self.validateValue(initial,for:n)}
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
                    guard ["card","vstack","hstack","disclosure","scroll","searchList","table","barChart"].contains(n.kind) else{throw AppError.message("Only containers may have children")}
                    try walk(children,depth:depth+1)
                }
            }
        }
        func containsBrightness(_ nodes:[DynamicUINode])->Bool{nodes.contains{$0.kind=="liveBrightness" || containsBrightness($0.children ?? [])}}
        for slot in slots{
            guard !containsBrightness(slot.nodes) || ["player.fullscreen.surf","player.fullscreen.brightness"].contains(slot.location) else{throw AppError.message("Native brightness belongs in a fullscreen brightness or Surf replacement")}
            try walk(slot.nodes,depth:0)
        }
        for page in pages{guard !containsBrightness(page.nodes) else{throw AppError.message("Brightness is fullscreen-only")};try walk(page.nodes,depth:0)}
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
    static func validateValue(_ value:String,for node:DynamicUINode)throws{
        guard value.utf8.count<=4096 else{throw AppError.message("Form value too large")}
        switch node.kind {
        case "toggle":
            guard ["true","false"].contains(value) else{throw AppError.message("Toggle value must be true or false")}
        case "picker","segmented","menu":
            guard (node.options ?? []).contains(where:{$0.value==value}) else{throw AppError.message("Unknown picker option")}
        case "slider":
            guard let number=Double(value),number.isFinite,number>=(node.minimum ?? 0),number<=(node.maximum ?? 100) else{throw AppError.message("Slider value outside supported range")}
        case "input":break
        default:throw AppError.message("Node is not an editable field")
        }
    }
    func formNode(_ field:String)->DynamicUINode?{
        func find(_ nodes:[DynamicUINode])->DynamicUINode?{
            for n in nodes {
                if n.valueKey=="form."+field,["input","toggle","picker","slider","segmented","menu"].contains(n.kind){return n}
                if let child=find(n.children ?? []){return child}
            }
            return nil
        }
        for s in slots{if let n=find(s.nodes){return n}}
        for p in pages{if let n=find(p.nodes){return n}}
        return nil
    }
    func retainedValues(_ old:[String:String])->[String:String]{
        var values=formDefaults()
        for (key,value) in old{
            if let node=formNode(key),(try? Self.validateValue(value,for:node)) != nil {values[key]=value}
        }
        return values
    }
    func formDefaults()->[String:String]{
        var result:[String:String]=[:]
        func visit(_ nodes:[DynamicUINode]){
            for n in nodes{
                if ["input","toggle","picker","slider","segmented","menu"].contains(n.kind),let key=n.valueKey {
                    result[String(key.dropFirst(5))]=n.defaultValue ?? (n.kind=="toggle" ? "false":["picker","segmented","menu"].contains(n.kind) ? n.options?.first?.value ?? "":n.kind=="slider" ? String(n.minimum ?? 0):"")
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

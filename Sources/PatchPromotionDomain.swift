import Foundation

// Only executable sources and layout definitions, never device settings or form values.
struct PatchSource: Codable, Equatable {
    var id:String
    var source:String
    var scope:String?=nil
    func checked()throws->Self{
        guard DynamicUILayout.validID(id),!source.isEmpty,source.utf8.count<=192*1024,scope==nil || scope=="catalogue" else{
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
        if let runtime=runtime{_ = try runtime.checked();guard runtime.scope==nil else{throw AppError.message("Runtime source cannot declare API scope")}}
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
    var parentID:String?
    var restoredFrom:String?
    var label:String?
    var knownGood:Bool?
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

// A complete application update is declarative source, never native code.
struct ApplicationRevision:Codable,Equatable{
    var schemaVersion=1
    var id:String
    var sources:PatchSourceSet
    var configuration:HotConfiguration?
    var runtimeABI:Int=2
    var adapterABI:Int=1
    var uiABI:Int=1
    var configurationABI:Int=1
    var acceptance:DeviceTestPlan?
    func checked()throws->Self{
        guard schemaVersion==1,DynamicUILayout.validID(id),id != "native-baseline",
              (1...2).contains(runtimeABI),adapterABI==1,uiABI==1,configurationABI==1 else{throw AppError.message("Unsupported application revision or native ABI")}
        _ = try sources.checked();_ = try configuration?.checked()
        _ = try acceptance?.checked()
        guard acceptance?.repeatForever != true else{throw AppError.message("Application acceptance must be finite")}
        guard try JSONCodec.encoder().encode(self).count<=950*1024 else{throw AppError.message("Application revision too large")}
        return self
    }
    // Use the same strict field validation and partial-runtime normalization as
    // standalone hot configuration. Codable alone silently discards unknown keys.
    static func decode(_ data:Data)throws->Self{
        guard data.count<=950*1024 else{throw AppError.message("Manifest too large")}
        guard var root=try JSONSerialization.jsonObject(with:data) as? [String:Any] else{
            throw AppError.message("Expected application manifest object")
        }
        if let raw=root["configuration"], !(raw is NSNull){
            guard let object=raw as? [String:Any] else{throw AppError.message("Expected configuration object")}
            let configuration=try HotConfiguration.decode(JSONSerialization.data(withJSONObject:object))
            root["configuration"]=try JSONSerialization.jsonObject(with:JSONCodec.encoder().encode(configuration))
        }
        return try JSONCodec.decoder().decode(Self.self,from:JSONSerialization.data(withJSONObject:root)).checked()
    }
    var identity:[String:String]{
        ["runtime":sources.runtime?.id ?? "native-baseline","api":sources.jellyfinAPI?.id ?? "native-baseline","ui":sources.ui?.revision ?? "native-baseline","configuration":configuration?.revision ?? "native-baseline"]
    }
}

enum ApplicationCapabilitySchema{
    static let commands:[String:[String:String]]=[
        "get_application_revision":[:],
        "get_capability_registry":[:],"query_native_data":["query":"NativeDataQuery JSON"],
        "get_effective_configuration":[:],"list_event_modules":[:],
        "validate_event_module":["module":"PlatformModule JSON"],
        "install_event_module":["module":"PlatformModule JSON","expectedRevision":"none or active revision","preserveState":"true|false"],
        "rollback_event_module":["id":"module ID","expectedRevision":"active revision"],
        "remove_event_module":["id":"module ID","expectedRevision":"active revision"],
        "stage_api_activation":["id":"API revision","source":"JavaScript source","scope":"catalogue or omitted","expectedRevision":"active API revision","mode":"when-idle|next-channel|next-cycle"],
        "get_staged_api_activation":[:],"cancel_staged_api_activation":["id":"staged revision"],
        "install_jellyfin_api":["id":"API revision","source":"JavaScript source","scope":"catalogue or omitted","activation":"immediate|reconnect"],
        "validate_patch_bundle":["manifest":"ApplicationRevision JSON"],
        "begin_application_trial":["manifest":"ApplicationRevision JSON","expected":"identity JSON from get_application_revision","seconds":"30...3600"],
        "restore_application_revision":["id":"archived application revision","expected":"identity JSON","seconds":"30...3600"],
        "finish_application_trial":["id":"trial ID","outcome":"accept|rollback","approval":"explicit when accepting"],
        "enter_safe_mode":["approval":"explicit"],
        "exit_safe_mode":["approval":"explicit"],
        "install_hot_configuration":["configuration":"HotConfiguration JSON","expectedRevision":"active config revision"],
        "stage_hot_configuration":["configuration":"HotConfiguration JSON","expectedRevision":"active config revision"],
        "reload_hot_configuration":["expectedRevision":"active config revision","stagedRevision":"optional staged revision"],
        "begin_configuration_trial":["stagedRevision":"staged config","expectedRevision":"active config revision","seconds":"30...3600"],
        "finish_configuration_trial":["id":"trial ID","expectedRevision":"active config","outcome":"accept|rollback","approval":"explicit when accepting"],
        "reload_current_stream":[:],
        "record_channel_failure":["id":"known channel ID","reason":"failure reason, max 512 bytes","approval":"explicit"],
        "seek_live":["offsetSeconds":"0...60"],
        "select_media_track":["kind":"audio|subtitle","index":"available option index|auto|off"],
        "set_fullscreen":["visible":"true|false"],
        "set_fullscreen_controls":["visible":"true|false"],
        "set_video_brightness":["level":"integer within configured presentation range; default -20...10, absolute bounds -25...20; local video-plane adjustment"]
    ]
    static let configuration:[String:[String:String]]=[
        "root":["schemaVersion":"1","revision":"unique ID, 1...80 characters","runtime":"RuntimePolicy; optional","playbackRules":"ordered rules, max 64","operations":"OperationalConfiguration; optional","interpreter":"InterpreterBudget; optional","presentation":"DynamicPresentation; optional","gateway":"GatewayConfiguration; optional"],
        "runtime":["startupTimeout":"5...120","retries":"1...5","qualityMbps":"1...50","stallSeconds":"5...300","rotationSeconds":"5...86400","validationSeconds":"3...60, less than startupTimeout","minimumFrames":"3...120","forwardBufferSeconds":"0...120","automaticallyWaits":"boolean","recoveryMode":"reopen|advance_surf|pause","failureRetrySeconds":"0...600","interAttemptDelaySeconds":"0...30","terminalAdvanceDelaySeconds":"0...60","adaptiveStartupExtraSeconds":"0...60","adaptiveChannelStartup":"boolean; learns bounded deadline from confirmed attempts","healthPenalty":"{baseDays:1...30,banAfter:2...20}; gateway policy takes precedence","strictChannelHiatus":"boolean; default true"],
        "interpreter":["timeoutMs":"100...10000","memoryMiB":"8...64","maxCommands":"1...100"],
        "gateway":["lanServerPort":"1...65535, default 8096","lanGatewayPort":"1...65535, default 8097","routePrefix":"validated absolute path, default /api/surf","ticketField":"JSON key, default url","timeoutSeconds":"5...90, default 20"],
        "operations":["systemSampleSeconds":"5...300","playbackSampleSeconds":"5...120","heartbeatSeconds":"15...600","syncSeconds":"15...600","catalogueRefreshSeconds":"60...3600","gatewayPolicyPollSeconds":"2...120","reconnectSeconds":"15...600","eventRetentionCount":"500...20000","eventRetentionMiB":"4...128","resourceWarningMiB":"64...2048","memoryGrowthWarningMiB":"16...1024","suspendAutomationOnPressure":"boolean","playerEventDiagnostics":"boolean","playerEventSampleSeconds":"1...60","surfTransport":"auto|device"],
        "presentation":["videoGravity":"fit|fill|stretch","fullscreenOpacity":"0.15...1","fullscreenMaxHeight":"0.20...0.80","fullscreenInitiallyVisible":"boolean","nativePlaybackControls":"boolean","pictureInPictureAllowed":"boolean","automaticPictureInPicture":"boolean","fullscreenWidth":"0.35...1","fullscreenPadding":"0...32","fullscreenSpacing":"0...24","fullscreenCornerRadius":"0...32","fullscreenButtonHeight":"44...80","fullscreenTextScale":"0.8...1.4","fullscreenAutoHideSeconds":"0...600; 0 disables","fullscreenTapToReveal":"boolean","fullscreenFitContent":"boolean","fullscreenPortraitMaxHeight":"0.20...0.80","fullscreenControlOrder":"permutation of rotationToggle, playPause, next, stop, favorite","fullscreenControlTheme":"night|standard; default night","videoBrightnessMinimum":"integer -25...-1; default -20","videoBrightnessMaximum":"integer 1...20; default 10"]
    ]
}

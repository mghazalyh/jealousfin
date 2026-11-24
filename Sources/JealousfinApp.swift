import SwiftUI
import AVKit

private let accent = Color(red:0.64,green:0.45,blue:0.98)
private let navy = Color(red:0.045,green:0.051,blue:0.09)
private let panel = Color(red:0.095,green:0.103,blue:0.16)
private let subdued = Color(red:0.62,green:0.64,blue:0.73)

@main struct JealousfinApp:App {
    @StateObject private var model=AppModel()
    var body:some Scene {
        WindowGroup {
            RootView().environmentObject(model).preferredColorScheme(.dark).tint(accent)
                .task{model.start()}
        }
    }
}
struct RootView:View {
    @EnvironmentObject var model:AppModel
    var body:some View{
        TabView(selection:$model.tab){
            NavigationView{GuideView()}.navigationViewStyle(.stack).tabItem{Label("Guide",systemImage:"tv")}.tag(0)
            NavigationView{PlaybackView()}.navigationViewStyle(.stack).tabItem{Label("Player",systemImage:"play.rectangle")}.tag(1)
            NavigationView{HealthView()}.navigationViewStyle(.stack).tabItem{Label("Health",systemImage:"waveform.path.ecg")}.tag(2)
            NavigationView{InsightsView()}.navigationViewStyle(.stack).tabItem{Label("Insights",systemImage:"lightbulb")}.tag(3)
            NavigationView{EngineeringView()}.navigationViewStyle(.stack).tabItem{Label("More",systemImage:"ellipsis.circle")}.tag(4)
        }
        .accentColor(accent)
        .overlay{if model.dynamicPage==nil{DecisionOverlay().environmentObject(model)}}
        .sheet(item:$model.dynamicPage){page in DynamicUIPageView(page:page).environmentObject(model)}
    }
}
struct Panel<Content:View>:View {
    let content:Content
    init(@ViewBuilder content:()->Content){self.content=content()}
    var body:some View{content.padding(14).frame(maxWidth:.infinity,alignment:.leading).background(panel).cornerRadius(14)}
}
struct SectionLabel:View {
    var title:String
    var body:some View{Text(title.uppercased()).font(.caption.weight(.semibold)).foregroundColor(subdued).tracking(1)}
}
struct Screen<Content:View>:View{
    let content:Content
    init(@ViewBuilder content:()->Content){self.content=content()}
    var body:some View{ScrollView{LazyVStack(alignment:.leading,spacing:14){content}.padding(16)}.background(navy.ignoresSafeArea())}
}
struct GuideView:View{
    @EnvironmentObject var model:AppModel
    @State private var search=""
    @State private var filter="All"
    var visible:[Channel]{
        let list = model.channels.filter{model.health[$0.id]?.eligible(at:Date()) ?? true}
        return list.filter{c in
            (filter=="All" || (filter=="Favorites" && c.favorite) || (filter=="Surf" && c.isSurf)) &&
            (search.isEmpty || c.name.localizedCaseInsensitiveContains(search) || (c.program?.title.localizedCaseInsensitiveContains(search) ?? false))
        }
    }
    var body:some View{
        Screen{
            DynamicUISlotView(location:"guide.top")
            HStack{
                VStack(alignment:.leading,spacing:4){Text("LIVE TV").font(.caption.bold()).foregroundColor(accent);Text("Jealousfin").font(.largeTitle.bold())}
                Spacer()
                Circle().fill(model.jelly.connected ? Color.green:Color.orange).frame(width:8,height:8)
                Text(model.jelly.connected ? "Connected":"Offline").font(.caption).foregroundColor(subdued)
            }
            Panel{
                VStack(alignment:.leading,spacing:10){
                    HStack{Label("Surf: All Channels",systemImage:"shuffle").font(.headline);Spacer();Text("\(model.settings.rotationMinutes) min").font(.caption).foregroundColor(accent)}
                    Text("Discover something new. Automatically rotate through eligible channels.").font(.subheadline).foregroundColor(subdued)
                    Button{model.startSurf()}label:{Label("Start Surf",systemImage:"play.fill").frame(maxWidth:.infinity)}
                        .buttonStyle(.borderedProminent)
                }
            }
            HStack{
                ForEach(["All","Favorites","Surf"],id:\.self){f in
                    Button{filter=f}label:{Text(f).font(.subheadline.weight(.semibold)).padding(.vertical,8).frame(maxWidth:.infinity).background(filter==f ? accent.opacity(0.28):panel).cornerRadius(10)}
                }
            }.buttonStyle(.plain)
            HStack{Image(systemName:"magnifyingglass").foregroundColor(subdued);TextField("Search channels",text:$search)}.padding(12).background(panel).cornerRadius(12)
            HStack{SectionLabel(title:"\(visible.count) channels");Spacer();Button{model.refreshFromUI()}label:{Image(systemName:"arrow.clockwise")}}
            if visible.isEmpty{
                Panel{VStack(alignment:.leading,spacing:8){Text(model.channels.isEmpty ? "Connect to Jellyfin":"No matching channels").font(.headline);Text(model.channels.isEmpty ? "Enter your server and account in Settings to load the actual Live TV catalogue.":"Try another filter.").foregroundColor(subdued).font(.subheadline);if model.channels.isEmpty{NavigationLink("Open Settings",destination:SettingsView())}}}
            }
            ForEach(visible){c in ChannelRow(channel:c)}
            DynamicUISlotView(location:"guide.bottom")
        }.navigationBarHidden(true)
    }
}
struct ChannelRow:View{
    @EnvironmentObject var model:AppModel
    var channel:Channel
    var body:some View{
        Panel{
            HStack(spacing:12){
                Button{model.open(channel)}label:{
                    HStack(spacing:12){
                        AsyncImage(url:model.jelly.logo(channel)){image in image.resizable().scaledToFit()}placeholder:{Image(systemName:channel.isSurf ? "shuffle":"tv").font(.title2).foregroundColor(accent)}
                            .frame(width:58,height:46).background(navy).cornerRadius(8)
                        VStack(alignment:.leading,spacing:5){
                            Text(channel.name).font(.subheadline.weight(.semibold)).foregroundColor(.white)
                            Text(channel.program?.title ?? "Programme information unavailable").font(.caption).foregroundColor(subdued).lineLimit(2)
                            if let p=channel.program?.progress {ProgressView(value:p).tint(accent)}
                        }.frame(maxWidth:.infinity,alignment:.leading)
                    }
                }.buttonStyle(.plain)
                if channel.isSurf {
                    Button{model.openServerSurf(channel)}label:{Image(systemName:"antenna.radiowaves.left.and.right").foregroundColor(subdued).padding(6)}.buttonStyle(.plain).accessibilityLabel("Play server-managed Surf")
                }
                Button{model.favoriteFromUI(channel)}label:{Image(systemName:channel.favorite ? "heart.fill":"heart").foregroundColor(channel.favorite ? accent:subdued).padding(6)}.buttonStyle(.plain)
            }
        }
    }
}
struct PlaybackView:View{
    @EnvironmentObject var model:AppModel
    @State private var category="All Channels"
    var body:some View{
        Screen{
            DynamicUISlotView(location:"player.top")
            HStack{Text("Now playing").font(.largeTitle.bold());Spacer();if model.playing{Label("LIVE",systemImage:"circle.fill").font(.caption.bold()).foregroundColor(.red)}}
            PlayerSurface(player:model.player,onReady:model.presentationReady).aspectRatio(16/9,contentMode:.fit).background(.black).cornerRadius(14)
            Panel{
                VStack(alignment:.leading,spacing:8){
                    Text(model.current?.name ?? "Choose a channel").font(.title2.bold())
                    Text(model.current?.program?.title ?? model.status).font(.subheadline).foregroundColor(subdued)
                    if let p=model.current?.program?.progress {ProgressView(value:p).tint(accent)}
                    HStack{
                        Button{if model.playing{model.pause()}else{model.resume()}}label:{Image(systemName:model.playing ? "pause.fill":"play.fill").frame(maxWidth:.infinity)}
                        Button{model.next()}label:{Image(systemName:"forward.end.fill").frame(maxWidth:.infinity)}
                        Button{model.stop()}label:{Image(systemName:"stop.fill").frame(maxWidth:.infinity)}
                        if let c=model.current,!c.isSurf{Button{model.favoriteFromUI(c)}label:{Image(systemName:c.favorite ? "heart.fill":"heart").frame(maxWidth:.infinity)}}
                    }.buttonStyle(.bordered)
                }
            }
            DynamicUISlotView(location:"player.controls")
            SectionLabel(title:"Surf controls")
            Panel{
                VStack(alignment:.leading,spacing:12){
                    HStack{Label(model.surfing ? "Surf active":"Surf stopped",systemImage:"shuffle");Spacer();Text(String(format:"%02d:%02d",model.remaining/60,model.remaining%60)).font(.title2.monospacedDigit()).foregroundColor(accent)}
                    Picker("Category",selection:$category){ForEach(["All Channels","Favorites","Sports","Movies & Series"],id:\.self){Text($0)}}
                    Stepper("Rotate every \(model.settings.rotationMinutes) min",value:$model.settings.rotationMinutes,in:1...1440).onChange(of:model.settings.rotationMinutes){_ in model.saveSettings()}
                    Toggle("Random order",isOn:$model.settings.random).onChange(of:model.settings.random){_ in model.saveSettings()}
                    Toggle("Validate while watching",isOn:$model.settings.validation).onChange(of:model.settings.validation){_ in model.saveSettings()}
                    HStack{
                        Button(model.surfing ? "Stop Surf":"Start Surf"){if model.surfing{model.stopSurf()}else{model.startSurf(category:category)}}.buttonStyle(.borderedProminent)
                        if model.surfing{Button(model.surfPaused ? "Resume rotation":"Pause rotation"){model.toggleSurfPause()}.buttonStyle(.bordered)}
                    }
                }
            }
            SectionLabel(title:"Recently played")
            ForEach(model.previous){c in ChannelRow(channel:c)}
            DynamicUISlotView(location:"player.bottom")
        }.navigationBarHidden(true)
    }
}
struct HealthView:View{
    @EnvironmentObject var model:AppModel
    @State private var filter="Hiatus"
    @State private var approval:Channel?
    var body:some View{
        Screen{
            DynamicUISlotView(location:"health.top")
            Text("Channel Health").font(.largeTitle.bold())
            Text("Validation through real playback").foregroundColor(subdued)
            HStack{
                stat("Eligible",model.eligibleChannels.count,.green)
                stat("Hiatus",model.hiatus.count,.orange)
                stat("Banned",model.banned.count,.red)
            }
            Panel{
                VStack(alignment:.leading,spacing:8){
                    Text("Validation policy").font(.headline)
                    Text("3 → 6 → 9 → 12 → 15 → 18 → 21 days").font(.subheadline.monospaced()).foregroundColor(accent)
                    Text("After the final hiatus, another confirmed failure permanently excludes the channel until explicit reinstatement.").font(.caption).foregroundColor(subdued)
                    Text("\(model.validatedCount) channels with recorded observations").font(.caption)
                }
            }
            Picker("Filter",selection:$filter){ForEach(["Hiatus","Banned","Observed"],id:\.self){Text($0)}}.pickerStyle(.segmented)
            let list=filter=="Hiatus" ? model.hiatus : filter=="Banned" ? model.banned:model.channels.filter{model.health[$0.id] != nil}
            if list.isEmpty{Panel{Text("No \(filter.lowercased()) records.").foregroundColor(subdued)}}
            ForEach(list){c in
                Panel{
                    VStack(alignment:.leading,spacing:8){
                        Text(c.name).font(.headline)
                        if let h=model.health[c.id]{
                            Text("\(h.failures) consecutive failures · \(h.successes) successes").font(.caption).foregroundColor(subdued)
                            if let until=h.hiatusUntil{Text("Eligible \(until.formatted())").font(.caption).foregroundColor(.orange)}
                            if let reason=h.reason{Text(reason).font(.caption).foregroundColor(subdued)}
                            if h.banned{Button("Approve reinstatement"){approval=c}.buttonStyle(.bordered)}
                        }
                    }
                }
            }
            DynamicUISlotView(location:"health.bottom")
        }.navigationBarHidden(true)
        .confirmationDialog("Explicitly reinstate \(approval?.name ?? "channel")?",isPresented:Binding(get:{approval != nil},set:{if !$0{approval=nil}}),titleVisibility:.visible){
            Button("Approve reinstatement"){if let c=approval{model.approve(c)};approval=nil}
            Button("Cancel",role:.cancel){approval=nil}
        }
    }
    private func stat(_ label:String,_ n:Int,_ color:Color)->some View{
        Panel{VStack(alignment:.leading,spacing:5){Text(label).font(.caption).foregroundColor(subdued);Text("\(n)").font(.title.bold()).foregroundColor(color)}}
    }
}
struct InsightsView:View{
    @EnvironmentObject var model:AppModel
    var body:some View{
        Screen{
            DynamicUISlotView(location:"insights.top")
            Text("Insights").font(.largeTitle.bold())
            Text("What needs your attention").foregroundColor(subdued)
            DecisionHistoryView()
            if model.insights.isEmpty{Panel{Label("No active issues detected",systemImage:"checkmark.circle").foregroundColor(.green);Text("This reflects current observations, not a complete system health certification.").font(.caption).foregroundColor(subdued)}}
            ForEach(model.insights){i in
                Panel{
                    VStack(alignment:.leading,spacing:8){
                        Text(i.priority.uppercased()).font(.caption.bold()).foregroundColor(i.priority=="Emergency" ? .red:i.priority=="High" ? .orange:accent)
                        Text(i.title).font(.headline)
                        Text(i.explanation).font(.subheadline).foregroundColor(subdued)
                        Label(i.action,systemImage:"arrow.turn.down.right").font(.caption).foregroundColor(accent)
                    }
                }
            }
            DynamicUISlotView(location:"insights.bottom")
        }.navigationBarHidden(true)
    }
}
struct EngineeringView:View{
    @EnvironmentObject var model:AppModel
    var body:some View{
        Screen{
            DynamicUISlotView(location:"more.top")
            Text("Engineering").font(.largeTitle.bold())
            Panel{
                VStack(alignment:.leading,spacing:12){
                    Label("TorrioDB",systemImage:"externaldrive").font(.headline)
                    Text(model.syncStatus).font(.subheadline).foregroundColor(subdued)
                    Text("Device: \(model.deviceID)").font(.caption.monospaced()).textSelection(.enabled)
                    Button("Synchronize now"){model.requestSync()}.buttonStyle(.borderedProminent)
                    Text("LAN port 8765 · \(model.lanStatus)").font(.caption).foregroundColor(subdued)
                }
            }
            Panel{
                VStack(alignment:.leading,spacing:16){
                    NavigationLink(destination:SettingsView()){Label("Settings",systemImage:"gear")}
                    NavigationLink(destination:RuntimeView()){Label("Remote & Runtime",systemImage:"terminal")}
                    NavigationLink(destination:DeviceTestsView()){Label("Device tests & hot patches",systemImage:"checklist")}
                    NavigationLink(destination:TelemetryView()){Label("Telemetry",systemImage:"waveform.path")}
                    NavigationLink(destination:LogsView()){Label("Structured logs",systemImage:"doc.text")}
                    NavigationLink(destination:EPGView()){Label("Programme guide & DVR",systemImage:"calendar")}
                }.font(.headline)
            }
            if let pages=model.dynamicUI.active?.pages,!pages.isEmpty{
                SectionLabel(title:"Custom pages")
                Panel{VStack(alignment:.leading,spacing:12){
                    ForEach(pages){page in Button{model.openDynamicPageFromView(page.id)}label:{
                        HStack{Label(page.title,systemImage:page.symbol ?? "square.grid.2x2");Spacer();Image(systemName:"chevron.right").font(.caption)}
                    }.buttonStyle(.plain)}
                }}
            }
            Button("Restore native interface"){
                Task{_ = await model.execute(Command(id:UUID().uuidString,action:"reset_ui_layout",arguments:nil,expiresAt:nil))}
            }.font(.caption).foregroundColor(.secondary)
            .disabled(model.dynamicUI.active==nil)
            DynamicUISlotView(location:"more.bottom")
        }.navigationBarHidden(true)
    }
}
struct SettingsView:View{
    @EnvironmentObject var model:AppModel
    var body:some View{
        Screen{
            DynamicUISlotView(location:"settings.top")
            SectionLabel(title:"Jellyfin")
            Panel{VStack(alignment:.leading,spacing:8){
                Text("API adapter").font(.headline)
                Text(model.jellyfinAPIRevision).font(.caption.monospaced()).textSelection(.enabled)
                if let fault=model.jellyfinAPIFault{Text(fault).font(.caption).foregroundColor(.orange)}
                Button("Restore native Jellyfin API"){
                    Task{_ = await model.execute(Command(id:UUID().uuidString,action:"reset_jellyfin_api",arguments:nil,expiresAt:nil))}
                }.buttonStyle(.bordered)
            }}
            Panel{VStack(alignment:.leading,spacing:12){
                TextField("Server URL",text:$model.settings.server).keyboardType(.URL).autocapitalization(.none).disableAutocorrection(true)
                Divider()
                TextField("Username",text:$model.settings.username).autocapitalization(.none).disableAutocorrection(true)
                Divider()
                SecureField("Password",text:$model.settings.password)
                Toggle("Automatically connect",isOn:$model.settings.autoLogin)
                Button("Connect & refresh"){Task{await model.connect()}}.buttonStyle(.borderedProminent)
                Text(model.status).font(.caption).foregroundColor(subdued)
            }}
            SectionLabel(title:"TorrioDB")
            Panel{VStack(alignment:.leading,spacing:10){SecureField("GitHub repository access token",text:$model.settings.githubToken);Text("Private repository: torriodb/jealousfin").font(.caption).foregroundColor(subdued);Text(model.syncStatus).font(.caption)}}
            SectionLabel(title:"Playback and operation")
            Panel{VStack(alignment:.leading,spacing:12){
                Stepper("Quality: \(model.settings.qualityMbps) Mbps",value:$model.settings.qualityMbps,in:1...50)
                Stepper("Attempts: \(model.settings.retries)",value:$model.settings.retries,in:1...5)
                Stepper("Startup timeout: \(model.settings.startupTimeout)s",value:$model.settings.startupTimeout,in:5...120,step:5)
                Toggle("Unattended recovery",isOn:$model.settings.unattended)
                Toggle("LAN command interface",isOn:$model.settings.lanEnabled)
            }}
            Button("Save settings"){model.saveSettings();model.requestSync()}.buttonStyle(.borderedProminent)
            Text("Background execution depends on iOS and the installed device.").font(.caption).foregroundColor(subdued)
            DynamicUISlotView(location:"settings.bottom")
        }.navigationTitle("Settings").navigationBarTitleDisplayMode(.inline)
    }
}
struct RuntimeView:View{
    @EnvironmentObject var model:AppModel
    @State private var source="console.log(api.state.status);\napi.command('get_status', {});"
    @State private var busy=false
    var body:some View{
        Screen{
            DynamicUISlotView(location:"runtime.top")
            Panel{VStack(alignment:.leading,spacing:10){Text("JavaScript runtime").font(.headline);Text("Scripts can inspect state and invoke registered application commands without reinstalling.").font(.caption).foregroundColor(subdued)
                TextEditor(text:$source).font(.system(.caption,design:.monospaced)).frame(minHeight:180)
                Button(busy ? "Running…":"Run script"){busy=true;Task{do{model.scriptOutput=try await model.runScript(source)}catch{model.scriptOutput=error.localizedDescription};busy=false}}.buttonStyle(.borderedProminent).disabled(busy)
                Text(model.scriptOutput).font(.system(.caption,design:.monospaced)).textSelection(.enabled)
            }}
            Panel{VStack(alignment:.leading,spacing:8){Text("Registered commands").font(.headline);ForEach(AppModel.actions,id:\.self){Text($0).font(.system(.caption,design:.monospaced))}}}
            DynamicUISlotView(location:"runtime.bottom")
        }.navigationTitle("Remote & Runtime").navigationBarTitleDisplayMode(.inline)
    }
}
struct TelemetryView:View{
    @EnvironmentObject var model:AppModel
    var body:some View{
        Screen{
            DynamicUISlotView(location:"telemetry.top")
            Panel{VStack(alignment:.leading,spacing:8){
                Label(model.telemetryActive ? "Collecting automatically":"Starting collector",systemImage:"waveform.path.ecg").foregroundColor(model.telemetryActive ? .green:subdued)
                Text(model.syncStatus).font(.caption).foregroundColor(subdued)
                if let last=model.telemetryLastSample{Text("Last system sample: "+last.formatted()).font(.caption).foregroundColor(subdued)}
            }}
            SectionLabel(title:"System and application")
            ForEach(model.systemMetrics.keys.sorted(),id:\.self){key in Panel{HStack{Text(key).font(.caption);Spacer();Text(model.systemMetrics[key] ?? "").font(.caption).foregroundColor(accent)}}}
            Text("Playback measurements").font(.title2.bold())
            if model.metrics.isEmpty{Panel{Text("No active playback sample; system telemetry remains active.").foregroundColor(subdued)}}
            ForEach(model.metrics.keys.sorted(),id:\.self){key in Panel{HStack{Text(key).font(.caption);Spacer();Text(model.metrics[key] ?? "").monospacedDigit().foregroundColor(accent)}}}
            Text("AVPlayer-reported measurements; actual physical A/V synchronization is not measured by these values.").font(.caption).foregroundColor(subdued)
            DynamicUISlotView(location:"telemetry.bottom")
        }.navigationTitle("Telemetry").navigationBarTitleDisplayMode(.inline)
    }
}
struct LogsView:View{
    @EnvironmentObject var model:AppModel
    var body:some View{
        Screen{
            DynamicUISlotView(location:"logs.top")
            ForEach(Array(model.events.reversed())){e in
                Panel{VStack(alignment:.leading,spacing:5){
                    HStack{Text(e.severity).font(.caption.bold()).foregroundColor(e.severity=="ERROR" ? .red:accent);Spacer();Text(e.timestamp.formatted(date:.omitted,time:.standard)).font(.caption2).foregroundColor(subdued)}
                    Text(e.component+" · "+e.code).font(.subheadline.weight(.medium))
                    ForEach(e.fields.keys.sorted(),id:\.self){key in Text(key+": "+(e.fields[key] ?? "")).font(.caption2.monospaced()).foregroundColor(subdued)}
                    if let trace=e.trace{Text(trace).font(.caption2).foregroundColor(subdued)}
                }}
            }
            DynamicUISlotView(location:"logs.bottom")
        }.navigationTitle("Logs").navigationBarTitleDisplayMode(.inline)
    }
}
struct EPGView:View{
    @EnvironmentObject var model:AppModel
    @State private var selected:Program?
    var body:some View{
        Screen{
            DynamicUISlotView(location:"epg.top")
            SectionLabel(title:"Programme guide")
            ForEach(model.programs){p in Panel{VStack(alignment:.leading,spacing:6){
                Text(p.title).font(.headline)
                if let c=model.channels.first(where:{$0.id==p.channelID}){Text(c.name).font(.caption).foregroundColor(subdued)}
                if let a=p.start,let b=p.end{Text(a.formatted(date:.omitted,time:.shortened)+" – "+b.formatted(date:.omitted,time:.shortened)).font(.caption).foregroundColor(subdued)}
                Button("Schedule recording"){selected=p}.font(.caption)
            }}}
            if model.programs.isEmpty{Text("No programme information returned by Jellyfin.").font(.caption).foregroundColor(subdued)}
            SectionLabel(title:"Recordings")
            Button("Refresh recordings"){model.loadRecordings()}
            ForEach(Array(model.recordings.enumerated()),id:\.offset){item in Panel{Text(item.element)}}
            DynamicUISlotView(location:"epg.bottom")
        }.navigationTitle("Guide & DVR").navigationBarTitleDisplayMode(.inline)
        .confirmationDialog("Schedule recording?",isPresented:Binding(get:{selected != nil},set:{if !$0{selected=nil}})){
            Button("Schedule"){if let p=selected{model.schedule(p)};selected=nil}
            Button("Cancel",role:.cancel){selected=nil}
        }
    }
}


struct DeviceTestsView:View{
    @EnvironmentObject var model:AppModel
    @State private var issue=""
    var body:some View{
        Screen{
            DynamicUISlotView(location:"tests.top")
            Text("On-device engineering").font(.title2.bold())
            Panel{VStack(alignment:.leading,spacing:8){
                Text("Active runtime module").font(.headline)
                Text(model.activePatchID).font(.system(.caption,design:.monospaced)).foregroundColor(accent)
                if let fault=model.patchFault{Text(fault).foregroundColor(.orange).font(.caption)}
                Text("Runtime modules, diagnostics and campaign plans can be updated from TorrioDB.").font(.caption).foregroundColor(subdued)
            }}
            Panel{VStack(alignment:.leading,spacing:10){
                Text("Device test campaign").font(.headline)
                if let cp=model.campaign{
                    Text(cp.plan.id+" · "+cp.state).font(.subheadline)
                    Text("Cycle \(cp.cycle+1) · Step \(cp.stepIndex+1) / \(cp.plan.steps.count)").font(.caption)
                    if let r=cp.lastResult{
                        Text(r.kind+" · "+r.status).font(.subheadline.bold()).foregroundColor(r.status=="passed" ? .green:r.status=="failed" ? .red:.orange)
                        ForEach(r.evidence.keys.sorted(),id:\.self){key in
                            Text(key+": "+(r.evidence[key] ?? "")).font(.caption).foregroundColor(subdued)
                        }
                    }
                    HStack{
                        if cp.canResume{Button("Pause"){change("paused")}}
                        if cp.state=="paused"{Button("Resume"){change("queued")}}
                        if ["queued","running","paused"].contains(cp.state){Button("Stop",role:.destructive){change("stopped")}}
                    }
                }else{Text("No campaign submitted.").font(.caption).foregroundColor(subdued)}
                if !issue.isEmpty{Text(issue).font(.caption).foregroundColor(.orange)}
            }}
            DynamicUISlotView(location:"tests.bottom")
        }.navigationTitle("Device testing").navigationBarTitleDisplayMode(.inline)
    }
    private func change(_ state:String){
        do{try model.setCampaignState(state);issue=""}
        catch{issue=error.localizedDescription}
    }
}

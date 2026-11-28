import SwiftUI
import AVKit

// One AVPlayer and one active presentation. No stream reopening on presentation
// changes. Native buttons remain available if a dynamic UI revision is rejected.
struct FullscreenSurfPlayer:View {
    @EnvironmentObject var model:AppModel
    @State private var contentHeight:CGFloat=0
    @State private var lastInteraction=Date()
    private var padding:CGFloat{CGFloat(presentation.fullscreenPadding ?? 8)}
    private var spacing:CGFloat{CGFloat(presentation.fullscreenSpacing ?? 6)}
    private var corner:CGFloat{CGFloat(presentation.fullscreenCornerRadius ?? 14)}
    private var presentation:DynamicPresentation{model.presentationConfiguration}
    private var palette:FullscreenControlPalette{FullscreenControlPalette(theme:presentation.resolvedFullscreenControlTheme)}
    var body:some View {
        GeometryReader{geometry in
            ZStack {
                Color.black.ignoresSafeArea()
                PlayerSurface(player:model.player,onReady:{if model.fullscreenPresented{model.presentationReady($0)}},showsControls:presentation.nativePlaybackControls ?? false,gravity:presentation.videoGravity ?? "fit",allowsPiP:presentation.pictureInPictureAllowed ?? true,automaticPiP:presentation.automaticPictureInPicture ?? false,brightness:model.videoBrightness)
                    .id(model.presentationEpoch)
                    .ignoresSafeArea()
                    .accessibilityLabel("Fullscreen video")
                    .overlay{
                        if !model.fullscreenControlsVisible && presentation.fullscreenTapToReveal != false{
                            Color.clear.contentShape(Rectangle()).onTapGesture{lastInteraction=Date();model.fullscreenControlsVisible=true}
                        }
                    }
                if model.fullscreenControlsVisible {
                    VStack(spacing:0){
                        header
                        Spacer(minLength:8)
                        let portrait=geometry.size.height>geometry.size.width
                        let fraction=portrait ? (presentation.fullscreenPortraitMaxHeight ?? presentation.fullscreenMaxHeight ?? 0.25):(presentation.fullscreenMaxHeight ?? 0.25)
                        footer(maxHeight:max(100,geometry.size.height*CGFloat(fraction)))
                    }
                    .frame(maxWidth:geometry.size.width*CGFloat(presentation.fullscreenWidth ?? 1))
                    .padding(.horizontal,padding).padding(.vertical,padding)
                    .simultaneousGesture(TapGesture().onEnded{lastInteraction=Date()})
                    .simultaneousGesture(DragGesture(minimumDistance:1).onChanged{_ in lastInteraction=Date()})
                }else{
                    VStack{
                        HStack{
                            Spacer()
                            PatchableRegion("player.fullscreen.showControls"){
                                Button{model.fullscreenControlsVisible=true}label:{
                                    Label("Controls",systemImage:"slider.horizontal.3")
                                        .font(.subheadline.bold()).padding(12)
                                        .background(.black.opacity(0.8)).cornerRadius(12)
                                }.accessibilityIdentifier("player.fullscreen.showControls")
                            }
                            closeButton
                        }
                        Spacer()
                    }.padding(12)
                }
            }
        }.preferredColorScheme(.dark)
            .onChange(of:model.fullscreenControlsVisible){visible in if visible{lastInteraction=Date()}}
            .task(id:presentation.fullscreenAutoHideSeconds){
                guard let seconds=presentation.fullscreenAutoHideSeconds,seconds>0 else{return}
                while !Task.isCancelled{
                    try? await Task.sleep(nanoseconds:1_000_000_000)
                    guard !Task.isCancelled else{return}
                    if model.fullscreenControlsVisible,Date().timeIntervalSince(lastInteraction)>=Double(seconds){model.fullscreenControlsVisible=false}
                }
            }
            .overlay{DecisionOverlay().environmentObject(model)}
    }
    private var header:some View{
        VStack(alignment:.leading,spacing:spacing){
            HStack(spacing:spacing){
                PatchableRegion("player.fullscreen.header"){
                VStack(alignment:.leading,spacing:3){
                    Text(model.current?.name ?? "Choose a channel").font(.headline).lineLimit(1)
                }
                }
                Spacer(minLength:4)
                PatchableRegion("player.fullscreen.hideControls"){
                    Button{model.fullscreenControlsVisible=false}label:{
                        Image(systemName:"eye.slash").frame(width:44,height:44)
                    }.accessibilityLabel("Hide Surf controls")
                        .accessibilityIdentifier("player.fullscreen.hideControls")
                }
                closeButton
            }
            DynamicUISlotView(location:"player.fullscreen.top")
        }.padding(padding).background(Color.black.opacity(presentation.fullscreenOpacity ?? 0.65)).cornerRadius(corner)
    }
    private var closeButton:some View{
        PatchableRegion("player.fullscreen.exit"){
            Button{model.fullscreenPresented=false}label:{
                Image(systemName:"arrow.down.right.and.arrow.up.left")
                    .font(.headline).frame(width:44,height:44)
                    .background(Color.black.opacity(0.8)).cornerRadius(10)
            }.accessibilityLabel("Exit fullscreen")
                .accessibilityIdentifier("player.fullscreen.exit")
        }
    }
    private func footer(maxHeight:CGFloat)->some View{
        ScrollView(showsIndicators:false){
            VStack(alignment:.leading,spacing:spacing){
                PatchableRegion("player.fullscreen.heading"){
                    HStack(spacing:8){
                        PatchableRegion("player.fullscreen.programmeTitle"){
                            Text(model.currentProgrammeTitle.flatMap{ $0.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty ? nil : $0 } ?? (model.current == nil ? "Choose a channel" : "Programme unavailable"))
                                .font(.system(size:22,weight:.semibold)).lineLimit(3)
                                .frame(maxWidth:.infinity,alignment:.leading)
                                .accessibilityIdentifier("player.fullscreen.programmeTitle")
                        }
                        Spacer(minLength:4)
                        PatchableRegion("player.fullscreen.countdown"){
                            Text(model.usingSharedSurf ? "Server rotation" : String(format:"%02d:%02d",model.remaining/60,model.remaining%60))
                                .font(.subheadline.monospacedDigit())
                                .fixedSize(horizontal:true,vertical:false)
                                .accessibilityLabel("Rotation countdown")
                                .accessibilityValue(model.surfPaused ? "Paused, "+String(model.remaining)+" seconds remaining":String(model.remaining)+" seconds remaining")
                                .accessibilityIdentifier("player.fullscreen.countdown")
                        }
                    }.foregroundColor(palette.secondary)
                }
                PatchableRegion("player.fullscreen.transport"){
                HStack(spacing:spacing){
                    ForEach(presentation.resolvedFullscreenControlOrder,id:\.self){id in
                        PatchableRegion("player.fullscreen."+id){
                            transportControl(id)
                        }
                    }
                }
                }
                PatchableRegion("player.fullscreen.surf"){
                HStack(spacing:8){
                    PatchableRegion("player.fullscreen.category"){
                        sourceSelector
                    }
                    PatchableRegion("player.fullscreen.surfToggle"){
                    Button{
                        if model.surfing || model.surfStartPending{model.stopSurf()}else{model.startSurfFromUI(category:model.settings.surfCategory)}
                    }label:{Text(model.surfing ? "Stop Surf":(model.surfStartPending ? "Cancel Surf start":"Start Surf")).font(.subheadline).frame(minHeight:44).padding(.horizontal,10)}
                        .buttonStyle(FullscreenControlButtonStyle(palette:palette))
                        .disabled(model.sourceTransitionBusy)
                        .accessibilityIdentifier("player.fullscreen.surfToggle")
                    }
                }
                PatchableRegion("player.fullscreen.brightness"){brightnessControl}
                PatchableRegion("player.fullscreen.favoriteError"){
                    if model.currentFavoriteFailed{Text("Favorite update failed. Try again.").font(.caption2).foregroundColor(palette.secondary).accessibilityIdentifier("player.fullscreen.favoriteError")}
                }
                PatchableRegion("player.fullscreen.sharedHint"){
                    if model.usingSharedSurf{
                        Text("Server manages source rotation. Reconnect refreshes the live stream.")
                            .font(.caption).foregroundColor(.secondary)
                    }
                }
                }
                PatchableRegion("player.fullscreen.footer"){
                    DynamicUISlotView(location:"player.fullscreen.controls")
                    DynamicUISlotView(location:"player.fullscreen.bottom")
                }
            }.padding(padding)
                .background(GeometryReader{proxy in Color.clear.preference(key:FullscreenContentHeight.self,value:proxy.size.height)})
        }
        .onPreferenceChange(FullscreenContentHeight.self){height in if height>0,abs(height-contentHeight)>1{contentHeight=height}}
        .frame(height:presentation.fullscreenFitContent != false && contentHeight>0 ? min(maxHeight,contentHeight):maxHeight)
        .background(Color.black.opacity(presentation.fullscreenOpacity ?? 0.65)).cornerRadius(corner)
    }
    @ViewBuilder private func transportControl(_ id:String)->some View{
        switch id{
        case "rotationToggle":
            if model.surfing && !model.usingSharedSurf{control(model.surfPaused ? "Resume rotation":"Pause rotation",model.surfPaused ? "play.circle":"pause.circle",id,role:model.surfPaused ? .rotationPaused : .rotation){model.toggleSurfPause()}}
        case "previous":
            control("Previous channel","backward.end.fill",id){model.previousFromUI()}
                .disabled(!model.canGoPrevious)
        case "playPause":
            control(model.playing ? "Pause":"Play",model.playing ? "pause.fill":"play.fill",id){if model.playing{model.pause()}else{model.resume()}}
                .disabled(model.current==nil)
        case "next":
            if model.surfing || model.currentSharedTransport{control(model.usingSharedSurf ? "Reconnect":"Next",model.usingSharedSurf ? "arrow.clockwise":"forward.end.fill",id){model.next()}}
        case "stop":
            control("Stop","stop.fill",id){model.stop()}
        case "favorite":
            if let c=model.current,!c.isSurf,!model.currentSharedTransport{
                let state=model.currentFavoriteState
                control(state.title,state.symbol,id,role:state.selected ? .favorite:.normal){model.favoriteFromUI(c)}
                    .disabled(!state.enabled)
                    .accessibilityValue(state.selected ? "In Favorites":"Not in Favorites")
                    .accessibilityAddTraits(state.selected ? .isSelected:[])
            }
        default:EmptyView()
        }
    }
    // American/Malay are typed aliases over the existing same-origin gateway
    // category protocol. This region stays replaceable as
    // player.fullscreen.category; a dynamic replacement suppresses this native
    // selector rather than rendering both.
    private var sourceSelector:some View{
        VStack(alignment:.leading,spacing:4){
            HStack(spacing:6){
                ForEach(SurfSourceIdentity.selectable,id:\.rawValue){source in
                    sourceButton(source)
                }
            }
            if model.sourceTransitionBusy{
                HStack(spacing:6){
                    ProgressView().scaleEffect(0.7)
                    Text("Switching source…").font(.caption2)
                }.foregroundColor(palette.secondary)
                    .accessibilityIdentifier("player.fullscreen.source.busy")
            }else if let error=model.sourceTransitionError{
                Label(error,systemImage:"exclamationmark.triangle.fill")
                    .font(.caption2).foregroundColor(.orange).lineLimit(2)
                    .accessibilityIdentifier("player.fullscreen.source.error")
            }else{
                Text("\(model.surfSourceChannelCount) channels • \(model.activeSurfSource.label) selected")
                    .font(.caption2).foregroundColor(palette.secondary).lineLimit(1)
                    .accessibilityIdentifier("player.fullscreen.source.count")
            }
        }
    }
    @ViewBuilder private func sourceButton(_ source:SurfSourceIdentity)->some View{
        let selected=model.activeSurfSource==source
        Button{
            Task{await model.transitionSurfSource(to:source)}
        }label:{
            VStack(alignment:.leading,spacing:2){
                HStack(spacing:4){
                    Image(systemName:selected ? "checkmark.circle.fill":"circle")
                    Text(source.label).font(.subheadline.bold()).lineLimit(1)
                }
                Text(source.categoryAlias).font(.caption2).lineLimit(1)
            }.frame(maxWidth:.infinity,minHeight:44,alignment:.leading)
                .padding(.horizontal,8)
        }
        .buttonStyle(FullscreenControlButtonStyle(palette:palette,role:selected ? .rotation:.normal))
        .disabled(model.sourceTransitionBusy)
        .accessibilityLabel(source.label+" source")
        .accessibilityValue(selected ? "Selected":"Not selected")
        .accessibilityHint(source.categoryAlias+" catalogue")
        .accessibilityIdentifier("player.fullscreen.source."+source.rawValue)
    }
    // Local video-plane luminance overlay; no transcoding or stream reopening.
    private var brightnessControl:some View{
        HStack(spacing:8){
            Label("Brightness",systemImage:"sun.max").font(.caption)
            Text(String(model.videoBrightnessRange.lowerBound)).font(.caption2)
            Slider(value:Binding(get:{Double(model.videoBrightness)},set:{model.setVideoBrightness(Int($0));lastInteraction=Date()}),in:Double(model.videoBrightnessRange.lowerBound)...Double(model.videoBrightnessRange.upperBound),step:1)
                .tint(palette.slider)
                .accessibilityLabel("Video brightness")
            Text("+"+String(model.videoBrightnessRange.upperBound)).font(.caption2)
            Text(model.videoBrightness > 0 ? "+"+String(model.videoBrightness):String(model.videoBrightness))
                .font(.caption.monospacedDigit()).frame(minWidth:25)
            Button{model.setVideoBrightness(0)}label:{Text("Reset").font(.caption).padding(.horizontal,9).frame(minHeight:32)}
                .buttonStyle(FullscreenControlButtonStyle(palette:palette))
                .accessibilityLabel("Reset brightness")
        }.foregroundColor(palette.text).accessibilityIdentifier("player.fullscreen.brightness")
    }
    private func control(_ title:String,_ image:String,_ id:String,role:FullscreenControlRole = .normal,action:@escaping()->Void)->some View{
        Button(action:action){
            VStack(spacing:4){
                Image(systemName:image).font(.system(size:CGFloat(presentation.fullscreenIconSize ?? 29)*CGFloat(presentation.fullscreenTextScale ?? 1),weight:.semibold))
                if presentation.fullscreenIconOnly==false{
                    Text(title).font(.system(size:11*CGFloat(presentation.fullscreenTextScale ?? 1))).lineLimit(1).minimumScaleFactor(0.7)
                }
            }.frame(maxWidth:.infinity,minHeight:CGFloat(presentation.fullscreenButtonHeight ?? 48))
        }.buttonStyle(FullscreenControlButtonStyle(palette:palette,role:role))
            .accessibilityLabel(title)
            .accessibilityIdentifier("player.fullscreen."+id)
    }
}

private struct FullscreenContentHeight:PreferenceKey{
    static var defaultValue:CGFloat=0
    static func reduce(value:inout CGFloat,nextValue:()->CGFloat){value=max(value,nextValue())}
}

// Native colors mirror the physically accepted night hot patch. The standard
// variant is opt-in and remains subdued; neither changes video brightness.
enum FullscreenControlRole{case normal,rotation,rotationPaused,favorite}
struct FullscreenControlPalette{
    let theme:String
    private var night:Bool{theme != "standard"}
    private func rgb(_ value:UInt32)->Color{
        Color(red:Double((value >> 16) & 255)/255,green:Double((value >> 8) & 255)/255,blue:Double(value & 255)/255)
    }
    var text:Color{rgb(night ? 0xb9bec7:0xeceff4)}
    var secondary:Color{rgb(night ? 0x9da6b2:0xaeb6c2)}
    var button:Color{rgb(night ? 0x171a20:0x282d36).opacity(0.91)}
    var border:Color{Color.white.opacity(night ? 0.09:0.18)}
    var slider:Color{rgb(night ? 0x8495ab:0x8ea4c3)}
    func foreground(_ role:FullscreenControlRole)->Color{
        switch role{
        case .normal:return text
        case .rotation:return rgb(night ? 0xb5c1d0:0xd5e3f6)
        case .rotationPaused,.favorite:return rgb(night ? 0xc4aab6:0xe0c3ce)
        }
    }
    func background(_ role:FullscreenControlRole)->Color{
        switch role{
        case .normal:return button
        case .rotation:return rgb(night ? 0x26313f:0x34465b).opacity(0.91)
        case .rotationPaused,.favorite:return rgb(night ? 0x352a31:0x493641).opacity(0.91)
        }
    }
    func outline(_ role:FullscreenControlRole)->Color{
        switch role{
        case .normal:return border
        case .rotation:return rgb(0x8193ad).opacity(night ? 0.20:0.35)
        case .rotationPaused,.favorite:return rgb(0x8b6e79).opacity(night ? 0.24:0.40)
        }
    }
}
struct FullscreenControlButtonStyle:ButtonStyle{
    @Environment(\.isEnabled) private var enabled
    let palette:FullscreenControlPalette
    var role:FullscreenControlRole = .normal
    func makeBody(configuration:Configuration)->some View{
        configuration.label
            .foregroundColor(palette.foreground(role))
            .background(palette.background(role))
            .cornerRadius(9)
            .overlay(RoundedRectangle(cornerRadius:9).strokeBorder(palette.outline(role),lineWidth:1))
            .opacity(enabled ? (configuration.isPressed ? 0.75:1):0.45)
            .contentShape(Rectangle())
    }
}

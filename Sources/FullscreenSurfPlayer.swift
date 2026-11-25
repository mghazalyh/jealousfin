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
    var body:some View {
        GeometryReader{geometry in
            ZStack {
                Color.black.ignoresSafeArea()
                PlayerSurface(player:model.player,onReady:{if model.fullscreenPresented{model.presentationReady($0)}},showsControls:presentation.nativePlaybackControls ?? false,gravity:presentation.videoGravity ?? "fit",allowsPiP:presentation.pictureInPictureAllowed ?? true,automaticPiP:presentation.automaticPictureInPicture ?? false,brightness:model.videoBrightness)
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
                            Button{model.fullscreenControlsVisible=true}label:{
                                Label("Controls",systemImage:"slider.horizontal.3")
                                    .font(.subheadline.bold()).padding(12)
                                    .background(.black.opacity(0.8)).cornerRadius(12)
                            }.accessibilityIdentifier("player.fullscreen.showControls")
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
                    Text(model.current?.program?.title ?? model.status).font(.caption).lineLimit(1)
                }
                }
                Spacer(minLength:4)
                Button{model.fullscreenControlsVisible=false}label:{
                    Image(systemName:"eye.slash").frame(width:44,height:44)
                }.accessibilityLabel("Hide Surf controls")
                    .accessibilityIdentifier("player.fullscreen.hideControls")
                closeButton
            }
            DynamicUISlotView(location:"player.fullscreen.top")
        }.padding(padding).background(Color.black.opacity(presentation.fullscreenOpacity ?? 0.65)).cornerRadius(corner)
    }
    private var closeButton:some View{
        Button{model.fullscreenPresented=false}label:{
            Image(systemName:"arrow.down.right.and.arrow.up.left")
                .font(.headline).frame(width:44,height:44)
                .background(Color.black.opacity(0.8)).cornerRadius(10)
        }.accessibilityLabel("Exit fullscreen")
            .accessibilityIdentifier("player.fullscreen.exit")
    }
    private func footer(maxHeight:CGFloat)->some View{
        ScrollView(showsIndicators:false){
            VStack(alignment:.leading,spacing:spacing){
                HStack{
                    Label(model.surfing ? "Surf active":"Surf stopped",systemImage:"shuffle").font(.headline)
                    Spacer()
                    Text(model.usingSharedSurf ? "Server rotation" : String(format:"%02d:%02d",model.remaining/60,model.remaining%60))
                        .font(.subheadline.monospacedDigit())
                }
                PatchableRegion("player.fullscreen.transport"){
                HStack(spacing:spacing){
                    ForEach(presentation.fullscreenControlOrder ?? DynamicPresentation.defaultControlOrder,id:\.self){id in
                        transportControl(id)
                    }
                }
                }
                PatchableRegion("player.fullscreen.surf"){
                HStack(spacing:8){
                    Menu{
                        ForEach(["All Channels","Favorites","Sports","Movies & Series"],id:\.self){category in
                            Button{
                                if model.surfing{model.startSurf(category:category)}
                                else{model.settings.surfCategory=category;model.saveSettings()}
                            }label:{
                                if model.settings.surfCategory==category{Label(category,systemImage:"checkmark")}
                                else{Text(category)}
                            }
                        }
                    }label:{Label(model.settings.surfCategory,systemImage:"list.bullet").lineLimit(1)}
                    .accessibilityIdentifier("player.fullscreen.category")
                    Spacer(minLength:0)
                    Button(model.surfing ? "Stop Surf":"Start Surf"){
                        if model.surfing{model.stopSurf()}else{model.startSurf(category:model.settings.surfCategory)}
                    }.buttonStyle(.borderedProminent)
                        .accessibilityIdentifier("player.fullscreen.surfToggle")
                }
                brightnessControl
                if model.usingSharedSurf{
                    Text("Server manages source rotation. Reconnect refreshes the live stream.")
                        .font(.caption).foregroundColor(.secondary)
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
            if model.surfing && !model.usingSharedSurf{control(model.surfPaused ? "Resume rotation":"Pause rotation",model.surfPaused ? "play.circle":"pause.circle",id){model.toggleSurfPause()}}
        case "playPause":
            control(model.playing ? "Pause":"Play",model.playing ? "pause.fill":"play.fill",id){if model.playing{model.pause()}else{model.resume()}}
        case "next":
            if model.surfing || model.currentSharedTransport{control(model.usingSharedSurf ? "Reconnect":"Next",model.usingSharedSurf ? "arrow.clockwise":"forward.end.fill",id){model.next()}}
        case "stop":
            control("Stop","stop.fill",id){model.stop()}
        case "favorite":
            if let c=model.current,!c.isSurf{control("Favorite",c.favorite ? "heart.fill":"heart",id){model.favoriteFromUI(c)}}
        default:EmptyView()
        }
    }
    // Local video-plane luminance overlay; no transcoding or stream reopening.
    private var brightnessControl:some View{
        HStack(spacing:8){
            Label("Brightness",systemImage:"sun.max").font(.caption)
            Text("−10").font(.caption2)
            Slider(value:Binding(get:{Double(model.videoBrightness)},set:{model.setVideoBrightness(Int($0));lastInteraction=Date()}),in:-10...10,step:1)
                .accessibilityLabel("Video brightness")
            Text("+10").font(.caption2)
            Text(model.videoBrightness > 0 ? "+"+String(model.videoBrightness):String(model.videoBrightness))
                .font(.caption.monospacedDigit()).frame(minWidth:25)
            Button("Reset"){model.setVideoBrightness(0)}.buttonStyle(.bordered)
                .accessibilityLabel("Reset brightness")
        }.accessibilityIdentifier("player.fullscreen.brightness")
    }
    private func control(_ title:String,_ image:String,_ id:String,action:@escaping()->Void)->some View{
        Button(action:action){
            VStack(spacing:4){
                Image(systemName:image).font(.system(size:17*CGFloat(presentation.fullscreenTextScale ?? 1),weight:.semibold))
                Text(title).font(.system(size:11*CGFloat(presentation.fullscreenTextScale ?? 1))).lineLimit(1).minimumScaleFactor(0.7)
            }.frame(maxWidth:.infinity,minHeight:CGFloat(presentation.fullscreenButtonHeight ?? 44))
        }.buttonStyle(.bordered)
            .accessibilityLabel(title)
            .accessibilityIdentifier("player.fullscreen."+id)
    }
}

private struct FullscreenContentHeight:PreferenceKey{
    static var defaultValue:CGFloat=0
    static func reduce(value:inout CGFloat,nextValue:()->CGFloat){value=max(value,nextValue())}
}

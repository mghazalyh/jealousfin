import SwiftUI
import AVKit

// One AVPlayer and one active presentation. No stream reopening on presentation
// changes. Native buttons remain available if a dynamic UI revision is rejected.
struct FullscreenSurfPlayer:View {
    @EnvironmentObject var model:AppModel
    var body:some View {
        GeometryReader{geometry in
            ZStack {
                Color.black.ignoresSafeArea()
                PlayerSurface(player:model.player,onReady:{if model.fullscreenPresented{model.presentationReady($0)}},showsControls:false)
                    .ignoresSafeArea()
                    .accessibilityLabel("Fullscreen video")
                if model.fullscreenControlsVisible {
                    VStack(spacing:0){
                        header
                        Spacer(minLength:8)
                        footer(maxHeight:max(100,geometry.size.height*0.48))
                    }.padding(.horizontal,12).padding(.vertical,8)
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
            .overlay{DecisionOverlay().environmentObject(model)}
    }
    private var header:some View{
        VStack(alignment:.leading,spacing:8){
            HStack(spacing:10){
                VStack(alignment:.leading,spacing:3){
                    Text(model.current?.name ?? "Choose a channel").font(.headline).lineLimit(1)
                    Text(model.current?.program?.title ?? model.status).font(.caption).lineLimit(1)
                }
                Spacer(minLength:4)
                Button{model.fullscreenControlsVisible=false}label:{
                    Image(systemName:"eye.slash").frame(width:44,height:44)
                }.accessibilityLabel("Hide Surf controls")
                    .accessibilityIdentifier("player.fullscreen.hideControls")
                closeButton
            }
            DynamicUISlotView(location:"player.fullscreen.top")
        }.padding(10).background(Color.black.opacity(0.78)).cornerRadius(14)
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
            VStack(alignment:.leading,spacing:10){
                HStack{
                    Label(model.surfing ? "Surf active":"Surf stopped",systemImage:"shuffle").font(.headline)
                    Spacer()
                    Text(model.usingSharedSurf ? "Server rotation" : String(format:"%02d:%02d",model.remaining/60,model.remaining%60))
                        .font(.subheadline.monospacedDigit())
                }
                HStack(spacing:8){
                    control(model.playing ? "Pause":"Play",model.playing ? "pause.fill":"play.fill","playPause"){
                        if model.playing{model.pause()}else{model.resume()}
                    }
                    if model.surfing{
                        control(model.usingSharedSurf ? "Reconnect":"Next",model.usingSharedSurf ? "arrow.clockwise":"forward.end.fill","next"){model.next()}
                    }
                    control("Stop","stop.fill","stop"){model.stop()}
                    if let c=model.current,!c.isSurf{
                        control("Favorite",c.favorite ? "heart.fill":"heart","favorite"){model.favoriteFromUI(c)}
                    }
                }
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
                if model.surfing && !model.usingSharedSurf{
                    Button(model.surfPaused ? "Resume rotation":"Pause rotation"){model.toggleSurfPause()}
                        .buttonStyle(.bordered)
                        .accessibilityIdentifier("player.fullscreen.rotationToggle")
                }
                if model.usingSharedSurf{
                    Text("Server manages source rotation. Reconnect refreshes the live stream.")
                        .font(.caption).foregroundColor(.secondary)
                }
                DynamicUISlotView(location:"player.fullscreen.controls")
                DynamicUISlotView(location:"player.fullscreen.bottom")
            }.padding(12)
        }.frame(maxHeight:maxHeight).background(Color.black.opacity(0.82)).cornerRadius(14)
    }
    private func control(_ title:String,_ image:String,_ id:String,action:@escaping()->Void)->some View{
        Button(action:action){
            VStack(spacing:4){
                Image(systemName:image).font(.headline)
                Text(title).font(.caption2).lineLimit(1).minimumScaleFactor(0.7)
            }.frame(maxWidth:.infinity,minHeight:44)
        }.buttonStyle(.bordered)
            .accessibilityLabel(title)
            .accessibilityIdentifier("player.fullscreen."+id)
    }
}

import SwiftUI
import AVKit
import AVFoundation

struct PlayerSurface: UIViewControllerRepresentable {
    var player: AVPlayer
    var onReady: (Bool)->Void
    var showsControls:Bool = true
    var gravity:String = "fit"
    private var videoGravity:AVLayerVideoGravity {
        switch gravity{case "fill":return .resizeAspectFill;case "stretch":return .resize;default:return .resizeAspect}
    }
    func makeCoordinator()->Coordinator { Coordinator(onReady) }
    final class Coordinator {
        var callback:(Bool)->Void
        var observation:NSKeyValueObservation?
        init(_ callback:@escaping(Bool)->Void){self.callback=callback}
    }
    func makeUIViewController(context:Context)->AVPlayerViewController {
        let c=AVPlayerViewController()
        c.player=player
        c.showsPlaybackControls=showsControls
        c.videoGravity=videoGravity
        c.allowsPictureInPicturePlayback=true
        context.coordinator.observation=c.observe(\.isReadyForDisplay,options:[.initial,.new]){ [weak coordinator=context.coordinator] vc,_ in
            let ready=vc.isReadyForDisplay
            DispatchQueue.main.async { coordinator?.callback(ready) }
        }
        return c
    }
    func updateUIViewController(_ uiViewController:AVPlayerViewController,context:Context) {
        context.coordinator.callback=onReady
        if uiViewController.player !== player {uiViewController.player=player}
        uiViewController.showsPlaybackControls=showsControls
        uiViewController.videoGravity=videoGravity
    }
    static func dismantleUIViewController(_ uiViewController:AVPlayerViewController,coordinator:Coordinator){
        coordinator.observation?.invalidate()
        coordinator.observation=nil
        // Do not pause the shared AVPlayer when switching surfaces.
        uiViewController.player=nil
    }
}

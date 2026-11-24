import SwiftUI
import AVKit
import AVFoundation

struct PlayerSurface: UIViewControllerRepresentable {
    var player: AVPlayer
    var onReady: (Bool)->Void
    func makeCoordinator()->Coordinator { Coordinator(onReady) }
    final class Coordinator {
        let callback:(Bool)->Void
        var observation:NSKeyValueObservation?
        init(_ callback:@escaping(Bool)->Void){self.callback=callback}
    }
    func makeUIViewController(context:Context)->AVPlayerViewController {
        let c=AVPlayerViewController()
        c.player=player
        c.showsPlaybackControls=true
        c.allowsPictureInPicturePlayback=true
        context.coordinator.observation=c.observe(\.isReadyForDisplay,options:[.initial,.new]){ [weak coordinator=context.coordinator] vc,_ in
            let ready=vc.isReadyForDisplay
            DispatchQueue.main.async { coordinator?.callback(ready) }
        }
        return c
    }
    func updateUIViewController(_ uiViewController:AVPlayerViewController,context:Context) { if uiViewController.player !== player { uiViewController.player=player } }
}

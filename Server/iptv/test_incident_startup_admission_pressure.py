import importlib
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

class StartupAdmissionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env={'JEALOUSFIN_STATE_DIR':temp.name,'JEALOUSFIN_USERNAME':'fixture',
             'JEALOUSFIN_PASSWORD':'fixture','JEALOUSFIN_TOKEN':'fixture-token',
             'JEALOUSFIN_SERVER_ID':'fixture-server','JEALOUSFIN_BIND':'127.0.0.1'}
        with patch.dict(os.environ,env):
            import gateway
            self.g=importlib.reload(gateway)
        self.handler=Mock()
        self.handler.headers={'Content-Length':'0'}
        self.handler.client_address=('192.168.1.10',1000)
        self.handler.authorized.return_value=True
        self.cid='a'*32
        self.config={'deviceIP':'192.168.1.10','backendPort':18797}

    def route(self,path,profile='american',malay=False):
        g=self.g
        actions=[]
        selected=Mock(side_effect=profile if isinstance(profile,Exception) else None,return_value=profile)
        with patch.object(g.prototype_selector,'load_selector',return_value=self.config), \
             patch.object(g.prototype_selector,'source_ids',return_value={self.cid} if malay else set()), \
             patch.object(g,'selected_profile_admitted',selected), \
             patch.object(g,'apply_device_spanish_request',side_effect=lambda *a:actions.append('marker')), \
             patch.object(g,'enqueue_playback_marker',side_effect=lambda *a:actions.append('async-marker')), \
             patch.object(g,'overlay_enqueue',side_effect=lambda *a:actions.append('overlay')):
            try:
                result=g.Handler.route_prototype(self.handler,path,{})
            except RuntimeError:
                result='rejected'
        return result,actions

    def test_failed_catalogue_retains_original_marker_boundary(self):
        self.assertEqual(self.route('/LiveTv/Channels',RuntimeError('unavailable')),('rejected',['marker','overlay']))

    def test_failed_playback_never_contacts_optional_ui(self):
        self.assertEqual(self.route('/Items/'+self.cid+'/PlaybackInfo',RuntimeError('unavailable')),('rejected',[]))

    def test_successful_english_catalogue_preserves_marker_before_overlay(self):
        self.assertEqual(self.route('/LiveTv/Channels'),(False,['marker','overlay']))

    def test_successful_english_playback_keeps_async_marker(self):
        self.assertEqual(self.route('/Items/'+self.cid+'/PlaybackInfo'),(False,['async-marker','overlay']))

    def test_malay_catalogue_retains_original_callbacks(self):
        self.assertEqual(self.route('/LiveTv/Channels','malay'),(True,['marker','overlay']))
        self.handler.proxy_malay.assert_called_once()

    def test_admitted_malay_playback_preserves_callbacks_and_proxy(self):
        self.assertEqual(self.route('/Items/'+self.cid+'/PlaybackInfo','malay',True),(True,['async-marker','overlay']))
        self.handler.proxy_malay.assert_called_once()

    def test_profile_mismatch_never_contacts_optional_ui(self):
        self.assertEqual(self.route('/Items/'+self.cid+'/PlaybackInfo','malay'),(True,[]))
        self.handler.reply.assert_called_once_with(409,{'JealousfinPolicyError':'excluded'})

if __name__=='__main__':unittest.main()

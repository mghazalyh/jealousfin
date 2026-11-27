import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import episode_audio as audio
import test_episode_audio as fixtures

class UnknownProgrammeTests(unittest.TestCase):
    def context(self, **kwargs):
        return dict(channelID=fixtures.CHANNEL, seriesTitle="", channelName="Example channel", **kwargs)

    def test_discovery_requires_audio_and_scopes_unknown_cache(self):
        calls=[]
        def provider(payload):
            calls.append(payload)
            return [fixtures.OFFICIAL_URL], [fixtures.REPORT_URL]
        discovery=audio.EpisodeSourceDiscovery(provider=provider)
        self.assertEqual(discovery.resolve(self.context()), ([], [], ""))
        clues=audio.extract_clues(fixtures.NXT_TEXT)
        official, reports, _=discovery.resolve(self.context(), clues)
        self.assertTrue(official and reports)
        self.assertEqual(calls[0]["seriesTitle"], "")
        self.assertEqual(calls[0]["channelName"], "Example channel")
        other=dict(self.context(), channelID="c"*32)
        discovery.resolve(other, clues)
        self.assertEqual(len(calls), 2)

    def test_unknown_requires_two_independent_matching_domains(self):
        req=fixtures.nxt_request()
        args=(req, [fixtures.nxt_official()])
        def promote(reports):
            return audio.promote_episode(*args, reports, fixtures.current(), fixtures.NOW, programme=self.context())
        reports=fixtures.nxt_reports()
        self.assertTrue(promote(reports).accepted)
        self.assertFalse(promote(reports[:1]).accepted)
        self.assertFalse(promote([reports[0], reports[0]]).accepted)
        self.assertFalse(promote([reports[0], audio.dataclasses.replace(reports[1],series_title="Unrelated")]).accepted)

    def test_no_epg_publication_must_have_playback_session(self):
        req=fixtures.nxt_request()
        promoted=audio.promote_episode(req,[fixtures.nxt_official()],fixtures.nxt_reports(),fixtures.current(),fixtures.NOW,programme=self.context())
        self.assertTrue(promoted.accepted)
        store=audio.PublicationStore(clock=lambda:fixtures.NOW)
        self.assertFalse(store.publish(promoted,self.context(),fixtures.NOW))
        self.assertTrue(store.publish(promoted,self.context(publicationSessionHash="test-session"),fixtures.NOW))
        self.assertEqual(store.get(fixtures.CHANNEL,fixtures.NOW)["audioSeriesTitle"],"WWE NXT")

class NoEPGGatewayTests(fixtures.GatewayAudioIntegrationTests):
    def test_new_row_only_for_same_active_session(self):
        now=time.time()
        req=fixtures.nxt_request(now)
        context={"channelID":fixtures.CHANNEL,"seriesTitle":"","publicationSessionHash":"s1"}
        promoted=audio.promote_episode(req,[fixtures.nxt_official()],fixtures.nxt_reports(),fixtures.current(now),now,programme=context)
        self.assertTrue(promoted.accepted)
        self.assertTrue(self.g.episode_publisher.publish(promoted,context,now))
        with patch.object(self.g.playback_activity,"views",return_value={fixtures.CHANNEL:SimpleNamespace(session_hash="s1")}):
            rows=self.g.merge_episode_publication({},now)
            row=rows[fixtures.CHANNEL]
            self.assertEqual(row["title"],"WWE NXT")
            self.assertNotIn("audioPlaybackSessionHash",row)
            item=self.g.programme_item(fixtures.CHANNEL,row)
            self.assertNotIn("StartDate",item)
            self.assertNotIn("EndDate",item)
            self.assertIn("WWE NXT",item["Name"])
            self.assertEqual(self.g.merge_episode_publication({fixtures.CHANNEL:{"title":"Different Show"}},now)[fixtures.CHANNEL]["title"],"Different Show")
        with patch.object(self.g.playback_activity,"views",return_value={fixtures.CHANNEL:SimpleNamespace(session_hash="s2")}):
            self.assertEqual(self.g.merge_episode_publication({},now),{})
        with patch.object(self.g.playback_activity,"views",return_value={}):
            self.assertEqual(self.g.merge_episode_publication({},now),{})

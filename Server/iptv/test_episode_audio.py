import unittest

import episode_audio as audio


class EpisodeAudioTests(unittest.TestCase):
    def setUp(self):
        self.sample = audio.AudioSessionSample("a" * 32, "session-1", "b" * 32, 8.0)
        self.candidates = [
            audio.Candidate("Silent Service", 4, 16, "JAG", 1998),
            audio.Candidate("King of the Greenie Board", 4, 17, "JAG", 1998),
        ]

    def test_contract_is_off_and_deferred(self):
        status = audio.status()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["active"])
        self.assertFalse(status["liveIntegration"])
        with self.assertRaises(audio.DeferredIntegration):
            audio.LocalWhisperAdapter().identify(self.sample, ["silent", "service"], self.candidates)

    def test_pure_ranking_requires_distinctive_evidence(self):
        match = audio.identify_local(self.sample, ["silent", "service"], self.candidates)
        self.assertIsNotNone(match)
        self.assertEqual((match.candidate.season_number, match.candidate.episode_number), (4, 16))
        evidence = match.evidence()
        self.assertNotIn("matchedTerms", evidence)
        self.assertNotIn("transcript", evidence)
        self.assertEqual(evidence["episodeTitle"], "Silent Service")
        self.assertIsNone(audio.identify_local(self.sample, ["the", "you", "this"], self.candidates))
        self.assertIsNone(audio.identify_local(self.sample, ["silent", "greenie"], self.candidates))

    def test_unsafe_correlation_and_sample_are_rejected(self):
        with self.assertRaises(ValueError):
            audio.identify_local(audio.AudioSessionSample("bad", "session-1", "b" * 32, 3.0), ["silent"], self.candidates)
        with self.assertRaises(ValueError):
            audio.identify_local(audio.AudioSessionSample("a" * 32, "session-1", "b" * 32, 0), ["silent"], self.candidates)
        with self.assertRaises(ValueError):
            audio.digest_sample(b"")
        self.assertEqual(len(audio.digest_sample(b"fixture")), 32)

    def test_candidate_bounds(self):
        self.assertFalse(audio.Candidate("", 0, 1).checked())
        self.assertFalse(audio.Candidate("Title", 1, 1, release_year=1800).checked())
        self.assertIsNone(audio.rank_episode_evidence("silent", [audio.Candidate("x", 1, 1)] * 33))


if __name__ == "__main__":
    unittest.main()

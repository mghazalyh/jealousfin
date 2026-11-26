import importlib
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import episode_audio as audio
import language_audio
import language_visits

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


NOW = 1_800_000_000.0
CHANNEL = "a" * 32


def binding(now=NOW, **overrides):
    values = dict(channel_id=CHANNEL, launch_id="launch-1", stream_generation="generation-1",
                  observed_at=now - 10, expires_at=now + 30, visit_id="visit-1")
    values.update(overrides)
    return audio.SessionBinding(**values)


def nxt_official(**overrides):
    values = dict(series_title="WWE NXT", season_number=20, episode_number=24,
                  episode_title="June 16, 2026", original_air_date="2026-06-16",
                  broadcaster_code="S20E24", series_premiere_year=2010,
                  source_url="https://www.cwtv.com/shows/wwe-nxt/episodes")
    values.update(overrides)
    return audio.OfficialEpisode(**values)


def nxt_reports():
    return [
        audio.IndependentReport(
            "https://www.f4wonline.com/news/wwe-nxt-results-june-16",
            "2026-06-17", ("Zaria", "first-ever women's North American Champion"),
            "2026-06-16", "WWE NXT", 20, 24, "S20E24"),
        audio.IndependentReport(
            "https://www.pwinsider.com/article/wwe-nxt-june-16",
            "2026-06-17", ("Zaria", "women's North American Championship"),
            "2026-06-16", "WWE NXT", 20, 25, "S20E25"),
    ]


NXT_TEXT = "Zaria becomes the first-ever women's North American Champion after the match."


def nxt_request(now=NOW, **overrides):
    values = dict(clues=audio.extract_clues(NXT_TEXT), binding=binding(now),
                  channel_name="CW Presents WWE NXT", language="en", language_outcome="allowed",
                  language_probability=0.97, speech_seconds=21.0, sample_seconds=32.0,
                  sample_digest="b" * 32)
    values.update(overrides)
    return audio.ClueRequest(**values)


def current(now=NOW, **overrides):
    values = {"channelID": CHANNEL, "launchID": "launch-1", "streamGeneration": "generation-1",
              "visitID": "visit-1", "observedAt": now}
    values.update(overrides)
    return values


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
        self.assertTrue(status["handoff"]["defaultOff"])
        self.assertFalse(status["handoff"]["searchEnabled"])
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


class ClueExtractionTests(unittest.TestCase):
    def test_clues_are_minimal_and_distinctive(self):
        clues = audio.extract_clues(NXT_TEXT)
        self.assertIn("zaria", clues)
        self.assertIn("womens north american champion", clues)
        self.assertLessEqual(len(clues), audio.MAX_CLUES)
        self.assertNotIn("the", clues)
        self.assertNotIn("welcome", clues)
        self.assertNotIn("after", clues)
        self.assertFalse(any(len(clue) > 80 for clue in clues))

    def test_generic_dialogue_and_promos_have_no_clues(self):
        self.assertEqual(audio.extract_clues("The champion is here tonight"), ())
        self.assertEqual(audio.extract_clues("Watch the replay promo clip tonight"), ())
        self.assertEqual(audio.extract_clues(""), ())
        self.assertEqual(audio.extract_clues(None), ())

    def test_clue_overlap_is_token_based_and_bounded(self):
        clues = audio.extract_clues(NXT_TEXT)
        self.assertEqual(len(audio.clue_matches(clues, ("Zaria", "women's North American Championship"))), 2)
        self.assertEqual(audio.clue_matches(clues, ("champion", "tonight")), ())


class SessionBindingTests(unittest.TestCase):
    def test_exact_binding_and_expiry(self):
        item = binding()
        self.assertTrue(item.checked(NOW))
        self.assertTrue(item.matches(current(), NOW))
        # A stream reload inside the same physical visit is still that visit.
        self.assertTrue(item.matches(current(streamGeneration="generation-2"), NOW))
        # A different visit, a missing visit or a different launch is stale.
        self.assertFalse(item.matches(current(visitID="visit-2"), NOW))
        self.assertFalse(item.matches(current(visitID=""), NOW))
        self.assertFalse(item.matches(current(launchID="launch-2"), NOW))
        self.assertFalse(item.checked(NOW + 31))
        self.assertFalse(item.checked(NOW - 11))

    def test_wrong_profile_or_malformed_binding_is_rejected(self):
        self.assertFalse(binding(profile="malay").checked(NOW))
        self.assertFalse(binding(channel_id="bad").checked(NOW))
        self.assertFalse(binding(launch_id="").checked(NOW))
        self.assertFalse(binding(expires_at=NOW + audio.MAX_SESSION_TTL + 10).checked(NOW))

    def test_make_binding_requires_exact_probe_and_visit(self):
        job = {"cid": CHANNEL, "launchID": "launch-1", "streamGeneration": "generation-1",
               "visitID": "visit-1", "observedAt": NOW - 5, "expiresAt": NOW + 50}
        self.assertIsNotNone(audio.make_binding(job, current(), NOW))
        # Same visit, new stream generation: accepted as a reload.
        self.assertIsNotNone(audio.make_binding(job, current(streamGeneration="other"), NOW))
        # Different or missing visit identity fails closed.
        self.assertIsNone(audio.make_binding(job, current(visitID="visit-2"), NOW))
        self.assertIsNone(audio.make_binding(job, current(visitID=""), NOW))
        self.assertIsNone(audio.make_binding(dict(job, launchID=""), current(), NOW))
        self.assertIsNone(audio.make_binding(dict(job, observedAt=NOW - 60), current(), NOW))
        # Without a visit the legacy exact-generation rule still applies.
        legacy = dict(job, visitID="")
        self.assertIsNotNone(audio.make_binding(legacy, dict(current(), visitID=""), NOW))
        self.assertIsNone(audio.make_binding(legacy, dict(current(streamGeneration="other"), visitID=""), NOW))

    def test_session_digest_is_visit_scoped_across_reloads(self):
        item = binding()
        self.assertEqual(item.digest(), binding(stream_generation="generation-2").digest())
        self.assertNotEqual(item.digest(), binding(visit_id="visit-2").digest())
        legacy = binding(visit_id="")
        self.assertNotEqual(legacy.digest(), binding(visit_id="", stream_generation="generation-2").digest())


class BuildRequestTests(unittest.TestCase):
    def setUp(self):
        self.job = {"cid": CHANNEL, "name": "CW Presents WWE NXT", "deviceVerified": True,
                    "changed": False, "launchID": "launch-1", "streamGeneration": "generation-1",
                    "visitID": "visit-1", "observedAt": NOW - 10, "expiresAt": NOW + 30}
        self.decision = {"outcome": "allowed", "language": "en", "confidence": 0.97,
                         "speechSeconds": 21.0, "sampleSeconds": 32.0}

    def test_transient_transcript_becomes_bounded_clues_only(self):
        request = audio.build_request(NXT_TEXT, self.job, self.decision, binding(), NOW)
        self.assertIsNotNone(request)
        payload = request.research_payload()
        self.assertEqual(set(payload), {"clues", "channelName", "language", "sampleSeconds", "speechSeconds"})
        self.assertIn("zaria", payload["clues"])
        self.assertEqual(payload["channelName"], "CW Presents WWE NXT")
        self.assertNotIn("Zaria becomes", json.dumps(payload))
        self.assertFalse(hasattr(request, "text"))
        self.assertEqual(request.binding.digest(), binding().digest())

    def test_language_and_session_gates_are_preserved(self):
        self.assertIsNone(audio.build_request(NXT_TEXT, self.job, dict(self.decision, outcome="excluded"), binding(), NOW))
        self.assertIsNone(audio.build_request(NXT_TEXT, self.job, dict(self.decision, language="es"), binding(), NOW))
        self.assertIsNone(audio.build_request(NXT_TEXT, dict(self.job, changed=True), self.decision, binding(), NOW))
        self.assertIsNone(audio.build_request(NXT_TEXT, dict(self.job, deviceVerified=False), self.decision, binding(), NOW))
        self.assertIsNone(audio.build_request(NXT_TEXT, self.job, self.decision, binding(profile="malay"), NOW))
        self.assertIsNone(audio.build_request("The champion is here tonight", self.job, self.decision, binding(), NOW))
        self.assertIsNone(audio.build_request(NXT_TEXT, self.job, self.decision, binding(), NOW + 31))


class CorroborationTests(unittest.TestCase):
    def test_nxt_proof_uses_official_code_and_keeps_original_air_date(self):
        verification = audio.corroborate_episode(audio.extract_clues(NXT_TEXT), [nxt_official()],
                                                 nxt_reports(), now=NOW)
        self.assertTrue(verification.accepted)
        identity = verification.identity
        self.assertEqual(identity.series_title, "WWE NXT")
        self.assertEqual((identity.season_number, identity.episode_number), (20, 24))
        self.assertEqual(identity.broadcaster_code, "S20E24")
        self.assertEqual(identity.original_air_date, "2026-06-16")
        self.assertEqual(identity.episode_year, 2026)
        self.assertEqual(identity.series_premiere_year, 2010)
        self.assertTrue(verification.numbering_conflict)
        self.assertEqual(verification.corroborating_sources, 2)
        self.assertEqual(verification.verification, "official-and-independent-report")

    def test_official_archive_is_required_for_promotion(self):
        self.assertEqual(audio.corroborate_episode(audio.extract_clues(NXT_TEXT), [], nxt_reports(), now=NOW).reason,
                         "missing-official-archive")
        unapproved = nxt_official(source_url="https://example.com/nxt")
        self.assertEqual(audio.corroborate_episode(audio.extract_clues(NXT_TEXT), [unapproved], nxt_reports(), now=NOW).reason,
                         "missing-official-archive")
        self.assertEqual(audio.corroborate_episode(audio.extract_clues(NXT_TEXT), [nxt_official()], nxt_reports()[:1], now=NOW).accepted,
                         True)

    def test_series_premiere_year_is_never_used_as_episode_year(self):
        identity = audio.corroborate_episode(audio.extract_clues(NXT_TEXT), [nxt_official(series_premiere_year=2010)],
                                             nxt_reports(), now=NOW).identity
        self.assertEqual(identity.episode_year, 2026)
        self.assertEqual(identity.series_premiere_year, 2010)
        self.assertFalse(nxt_official(original_air_date="").checked())
        missing_date = audio.corroborate_episode(audio.extract_clues(NXT_TEXT),
                                                 [nxt_official(original_air_date="")], nxt_reports(), now=NOW)
        self.assertFalse(missing_date.accepted)
        self.assertIsNone(missing_date.identity)

    def test_ambiguous_and_conflicting_evidence_is_rejected(self):
        two_official = [nxt_official(), nxt_official(episode_number=25, broadcaster_code="S20E25")]
        self.assertEqual(audio.corroborate_episode(audio.extract_clues(NXT_TEXT), two_official,
                                                   nxt_reports(), now=NOW).reason, "ambiguous-episode")
        conflicting = nxt_reports()
        conflicting[1] = audio.IndependentReport(
            "https://www.pwinsider.com/other", "2026-06-10", ("Zaria", "women's North American Championship"),
            "2026-06-09", "WWE NXT")
        self.assertEqual(audio.corroborate_episode(audio.extract_clues(NXT_TEXT), [nxt_official()],
                                                   conflicting, now=NOW).reason, "conflicting-air-date")
        mismatched = audio.corroborate_episode(audio.extract_clues(NXT_TEXT),
                                               [nxt_official(original_air_date="2026-06-17")],
                                               nxt_reports(), now=NOW)
        self.assertEqual(mismatched.reason, "missing-official-air-date-match")

    def test_ads_reused_promos_generic_and_wrong_series_are_rejected(self):
        clues = audio.extract_clues(NXT_TEXT)
        bad = nxt_reports()[0]
        self.assertEqual(audio.corroborate_episode(clues, [nxt_official()],
                                                   [audio.IndependentReport(**dict(bad.__dict__, advertisement=True))],
                                                   now=NOW).reason, "advertisement")
        self.assertEqual(audio.corroborate_episode(clues, [nxt_official()],
                                                   [audio.IndependentReport(**dict(bad.__dict__, reused_promo=True))],
                                                   now=NOW).reason, "reused-promo")
        generic = audio.IndependentReport("https://www.f4wonline.com/g", "2026-06-17",
                                          ("champion", "tonight"), "2026-06-16", "WWE NXT")
        self.assertEqual(audio.corroborate_episode(clues, [nxt_official()], [generic], now=NOW).reason,
                         "generic-reference")
        wrong = audio.IndependentReport(**dict(bad.__dict__, series_title="Another Show"))
        self.assertEqual(audio.corroborate_episode(clues, [nxt_official()], [wrong], now=NOW).reason,
                         "series-mismatch")
        # One wrong-series report must not discard a valid corroborating report.
        self.assertTrue(audio.corroborate_episode(clues, [nxt_official()], [wrong, nxt_reports()[0]], now=NOW).accepted)
        self.assertEqual(audio.corroborate_episode(("champion",), [nxt_official()], nxt_reports(), now=NOW).reason,
                         "insufficient-clues")


class PromotionTests(unittest.TestCase):
    def test_promotion_requires_exact_current_session_and_expiry(self):
        request = nxt_request()
        promoted = audio.promote_episode(request, [nxt_official()], nxt_reports(), current(), NOW)
        self.assertTrue(promoted.accepted)
        self.assertEqual(promoted.reason, "")
        self.assertIsNotNone(promoted.episode)
        evidence = promoted.episode.evidence()
        self.assertEqual(evidence["officialCode"], "S20E24")
        self.assertEqual(evidence["originalAirDate"], "2026-06-16")
        self.assertEqual(evidence["episodeYear"], 2026)
        self.assertEqual(evidence["seriesPremiereYear"], 2010)
        self.assertNotIn("clues", evidence)
        self.assertNotIn("transcript", evidence)
        self.assertNotIn("launchID", evidence)
        self.assertNotIn("streamGeneration", evidence)
        self.assertIsNone(evidence["scheduledAiring"])

        self.assertEqual(audio.promote_episode(nxt_request(), [nxt_official()],
                                               nxt_reports(), current(NOW + 31), NOW + 31).reason, "session-expired")
        # Same physical visit, new stream generation: a reload, not a stale
        # session, and it must keep the same visit-scoped digest.
        reloaded = audio.promote_episode(request, [nxt_official()], nxt_reports(),
                                         current(streamGeneration="other"), NOW)
        self.assertTrue(reloaded.accepted)
        self.assertEqual(reloaded.episode.evidence()["sessionDigest"], evidence["sessionDigest"])
        # A job from a visit that has ended is stale.
        self.assertEqual(audio.promote_episode(request, [nxt_official()], nxt_reports(),
                                               current(visitID="visit-2"), NOW).reason, "session-mismatch")
        self.assertEqual(audio.promote_episode(request, [nxt_official()], nxt_reports(),
                                               current(visitID=""), NOW).reason, "session-mismatch")
        self.assertEqual(audio.promote_episode(request, [nxt_official()], nxt_reports(),
                                               current(launchID="other"), NOW).reason, "session-mismatch")

    def test_scheduled_airing_is_separate_from_content_identity(self):
        events = [{"station": "CW", "title": "WWE NXT", "start": NOW - 100, "end": NOW + 100,
                   "source": "public-schedule"}]
        promoted = audio.promote_episode(nxt_request(), [nxt_official()], nxt_reports(),
                                         current(), NOW, scheduled_events=events)
        self.assertTrue(promoted.accepted)
        self.assertEqual(promoted.episode.identity.episode_number, 24)
        self.assertEqual(promoted.episode.scheduled_airing["station"], "CW")
        self.assertEqual(promoted.episode.scheduled_airing["source"], "public-schedule")

        ambiguous = events + [{"station": "CW", "title": "WWE NXT", "start": NOW - 50,
                               "end": NOW + 50, "source": "public-schedule"}]
        promoted = audio.promote_episode(nxt_request(), [nxt_official()], nxt_reports(),
                                         current(), NOW, scheduled_events=ambiguous)
        self.assertTrue(promoted.accepted)
        self.assertEqual(promoted.episode.identity.episode_number, 24)
        self.assertIsNone(promoted.episode.scheduled_airing)

    def test_schedule_alone_cannot_create_identity(self):
        events = [{"station": "CW", "title": "WWE NXT", "start": NOW - 100, "end": NOW + 100,
                   "source": "public-schedule"}]
        promoted = audio.promote_episode(nxt_request(), [], [], current(), NOW, scheduled_events=events)
        self.assertFalse(promoted.accepted)
        self.assertEqual(promoted.reason, "missing-official-archive")
        self.assertIsNone(promoted.episode)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.budget = audio.EpisodeBudget(clock=lambda: self.now[0])
        self.view = {"channel_id": CHANNEL, "session_hash": "session-1", "active_seconds": 150}

    def eligible(self, view=None, **overrides):
        values = dict(has_episode_identity=False, profile="american", language="en",
                      language_outcome="allowed", now=self.now[0])
        values.update(overrides)
        return self.budget.eligible(view or self.view, **values)

    def test_active_viewing_priority_and_identity_trigger(self):
        self.assertEqual(self.eligible(), (True, ""))
        self.assertEqual(self.eligible(view=dict(self.view, active_seconds=30)), (False, "inactive"))
        self.assertEqual(self.eligible(has_episode_identity=True), (False, "identity-present"))
        self.assertEqual(self.eligible(profile="malay"), (False, "profile"))
        self.assertEqual(self.eligible(language="es", language_outcome="excluded"), (False, "language-gated"))
        self.assertEqual(self.eligible(view={"channel_id": CHANNEL}), (False, "session-unverified"))
        # A catalogue title alone does not suppress the trigger.
        self.assertEqual(self.eligible(has_episode_identity=False), (True, ""))

    def test_cooldown_dedup_and_attempt_limit(self):
        self.assertTrue(self.budget.begin(self.view))
        self.assertEqual(self.eligible(), (False, "duplicate"))
        self.now[0] += self.budget.cooldown_seconds + 1
        self.assertEqual(self.eligible(), (True, ""))
        self.assertTrue(self.budget.begin(self.view))
        self.now[0] += self.budget.cooldown_seconds + 1
        self.assertEqual(self.eligible(), (False, "attempt-limit"))
        self.budget.finish(self.view, "no-match")
        diagnostics = self.budget.diagnostics()
        self.assertEqual(diagnostics["maxAttempts"], audio.MAX_ATTEMPTS)
        self.assertNotIn("session-1", json.dumps(diagnostics))

    def test_daily_budget_is_bounded(self):
        budget = audio.EpisodeBudget(daily_budget=1, clock=lambda: self.now[0])
        self.assertTrue(budget.begin(self.view))
        other = {"channel_id": "b" * 32, "session_hash": "session-2", "active_seconds": 150}
        self.assertEqual(budget.eligible(other, has_episode_identity=False, profile="american",
                                         language="en", language_outcome="allowed", now=self.now[0]),
                         (False, "budget"))

    def test_cross_session_search_dedup_and_identity_helper(self):
        self.assertFalse(audio.programme_has_episode_identity({"title": "WWE NXT"}))
        self.assertTrue(audio.programme_has_episode_identity(
            {"title": "WWE NXT", "seasonNumber": 20, "episodeNumber": 24}))
        self.assertFalse(audio.programme_has_episode_identity(
            {"seasonNumber": "20", "episodeNumber": 24}))
        dedup = "clue-fingerprint"
        self.assertTrue(self.budget.eligible(self.view, has_episode_identity=False,
                                             profile="american", language="en",
                                             language_outcome="allowed", now=self.now[0],
                                             dedup_key=dedup)[0])
        self.assertTrue(self.budget.begin(self.view, dedup_key=dedup))
        other_session = dict(self.view, session_hash="session-2")
        self.assertEqual(self.budget.eligible(other_session, has_episode_identity=False,
                                              profile="american", language="en",
                                              language_outcome="allowed", now=self.now[0],
                                              dedup_key=dedup), (False, "duplicate-search"))
        self.now[0] += self.budget.dedup_seconds + 1
        self.assertTrue(self.budget.eligible(other_session, has_episode_identity=False,
                                             profile="american", language="en",
                                             language_outcome="allowed", now=self.now[0],
                                             dedup_key=dedup)[0])


class ObserverTests(unittest.TestCase):
    def test_observer_is_default_off_and_research_receives_only_clues(self):
        calls = []
        observer = audio.EpisodeObserver(lambda: current(), lambda payload: calls.append(payload) or ([], []))
        wall = time.time()
        job = {"cid": CHANNEL, "name": "CW Presents WWE NXT", "deviceVerified": True,
               "changed": False, "launchID": "launch-1", "streamGeneration": "generation-1",
               "visitID": "visit-1", "observedAt": wall - 5, "expiresAt": wall + 50,
               "activeSeconds": 150, "hasEpisodeIdentity": False}
        decision = {"outcome": "allowed", "language": "en", "confidence": 0.97,
                    "speechSeconds": 21.0, "sampleSeconds": 32.0}
        self.assertIsNone(observer(job, decision, NXT_TEXT))
        self.assertEqual(calls, [])

    def test_enabled_observer_promotes_with_nxt_fixture_and_minimal_payload(self):
        calls = []
        wall = time.time()
        job = {"cid": CHANNEL, "name": "CW Presents WWE NXT", "deviceVerified": True,
               "changed": False, "launchID": "launch-1", "streamGeneration": "generation-1",
               "visitID": "visit-1", "observedAt": wall - 5, "expiresAt": wall + 50,
               "activeSeconds": 150, "hasEpisodeIdentity": False}
        decision = {"outcome": "allowed", "language": "en", "confidence": 0.97,
                    "speechSeconds": 21.0, "sampleSeconds": 32.0}

        def research(payload):
            calls.append(payload)
            return [nxt_official()], nxt_reports()

        budget = audio.EpisodeBudget(clock=lambda: wall)
        observer = audio.EpisodeObserver(lambda: current(now=wall), research, enabled=True, budget=budget)
        promotion = observer(job, dict(decision), NXT_TEXT)
        self.assertTrue(promotion.accepted)
        self.assertEqual(promotion.episode.identity.broadcaster_code, "S20E24")
        self.assertEqual(len(calls), 1)
        payload = calls[0]
        self.assertIn("zaria", payload["clues"])
        self.assertEqual(set(payload), {"clues", "channelName", "language", "sampleSeconds", "speechSeconds"})
        self.assertNotIn("Zaria becomes", json.dumps(payload))
        self.assertNotIn("transcript", payload)

        budget2 = audio.EpisodeBudget(clock=lambda: wall)
        observer2 = audio.EpisodeObserver(lambda: current(now=wall), research, enabled=True, budget=budget2)
        self.assertIsNone(observer2(dict(job, hasEpisodeIdentity=True), dict(decision), NXT_TEXT))
        self.assertEqual(len(calls), 1)


class LanguageHookTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.manager = object.__new__(language_audio.Manager)
        self.manager.on_speech = lambda job, decision, text: self.seen.append((job, decision, text))
        self.manager.log = lambda message: None
        self.job = {"cid": CHANNEL, "provider": "nltv:us:pluto-6a30478d0be0264f4a4e227c",
                    "name": "CW Presents WWE NXT", "deviceVerified": True, "changed": False,
                    "launchID": "launch-1", "streamGeneration": "generation-1",
                    "visitID": "visit-1"}
        self.decision = {"outcome": "allowed", "language": "en"}

    def test_allowed_verified_transcript_is_handed_off_without_pcm(self):
        self.manager._notify_speech(self.job, self.decision, ("en", NXT_TEXT))
        self.assertEqual(len(self.seen), 1)
        job, decision, text = self.seen[0]
        self.assertNotIn("pcm", job)
        self.assertEqual(job["launchID"], "launch-1")
        self.assertEqual(decision["language"], "en")
        self.assertEqual(text, NXT_TEXT)

    def test_handoff_is_fresh_preserves_exact_visit_and_drops_sample_clock(self):
        before = time.time()
        self.manager._notify_speech(dict(self.job, observedAt=0, expiresAt=0),
                                    self.decision, ("en", NXT_TEXT))
        view, _, _ = self.seen[0]
        self.assertEqual(view["visitID"], "visit-1")
        self.assertEqual(view["streamGeneration"], "generation-1")
        self.assertGreaterEqual(view["observedAt"], before)
        self.assertIsNone(view["expiresAt"])
        self.assertNotIn("pcm", view)

    def test_language_gate_and_unverified_samples_are_not_handed_off(self):
        self.manager._notify_speech(self.job, {"outcome": "excluded", "language": "es"}, ("es", "texto"))
        self.manager._notify_speech(self.job, {"outcome": "unauthorized", "language": "ja"}, ("ja", "text"))
        self.manager._notify_speech(dict(self.job, changed=True), self.decision, ("en", NXT_TEXT))
        self.manager._notify_speech(dict(self.job, deviceVerified=False), self.decision, ("en", NXT_TEXT))
        self.manager._notify_speech(self.job, self.decision, None)
        self.assertEqual(self.seen, [])

    def test_synchronous_promotion_return_is_used_and_never_logs_text(self):
        logs = []
        self.manager.log = logs.append
        self.manager.on_speech = lambda job, decision, text: audio.Promotion(True, "", None)
        self.manager._notify_speech(self.job, self.decision, ("en", NXT_TEXT))
        self.assertTrue(any("EPISODE_IDENTITY_PROMOTED" == message for message in logs))
        self.assertFalse(any(NXT_TEXT in message for message in logs))
        logs.clear()
        self.manager.on_speech = lambda job, decision, text: audio.Promotion(False, "session-changed", None)
        self.manager._notify_speech(self.job, self.decision, ("en", NXT_TEXT))
        self.assertTrue(any("EPISODE_IDENTITY_SKIPPED reason=session-changed" == message
                            for message in logs))
        self.assertFalse(any(NXT_TEXT in message for message in logs))


OFFICIAL_PAGE = (
    '<script type="application/ld+json">{"@type":"TVEpisode","name":"June 16, 2026",'
    '"datePublished":"2026-06-16","episodeNumber":24,'
    '"partOfSeason":{"seasonNumber":20},"partOfSeries":{"name":"WWE NXT"}}</script>'
)
REPORT_PAGE = (
    '<script type="application/ld+json">{"@type":"NewsArticle",'
    '"headline":"WWE NXT results: Zaria wins first-ever women\'s North American Championship",'
    '"description":"Zaria became the first-ever women\'s North American Champion.",'
    '"datePublished":"2026-06-17"}</script>'
)
OFFICIAL_URL = "https://www.cwtv.com/shows/wwe-nxt/episodes"
REPORT_URL = "https://www.f4wonline.com/news/wwe-nxt-results-june-16"


def context_row(**overrides):
    values = dict(channelID=CHANNEL, seriesTitle="WWE NXT", seasonNumber=None,
                  episodeNumber=None, episodeTitle="", originalAirDate="")
    values.update(overrides)
    return values


class ProgrammeContextTests(unittest.TestCase):
    def test_context_is_bounded_public_metadata_only(self):
        context = audio.programme_context(
            {"title": "WWE NXT", "episodeTitle": "June 16, 2026", "seasonNumber": 20,
             "episodeNumber": 24, "airDate": "2026-06-16"}, CHANNEL)
        self.assertEqual(context["seriesTitle"], "WWE NXT")
        self.assertEqual(context["seasonNumber"], 20)
        self.assertEqual(context["episodeNumber"], 24)
        self.assertEqual(context["originalAirDate"], "2026-06-16")
        self.assertTrue(context["verified"])
        self.assertIsNone(audio.programme_context({"title": ""}, CHANNEL))
        self.assertIsNone(audio.programme_context({"title": "Show"}, "bad-channel"))

    def test_programme_matching_is_strict_but_optional(self):
        identity = audio.corroborate_episode(audio.extract_clues(NXT_TEXT), [nxt_official()],
                                             nxt_reports(), now=NOW).identity
        self.assertTrue(audio.programme_matches(identity, None))
        self.assertTrue(audio.programme_matches(identity, context_row()))
        self.assertFalse(audio.programme_matches(identity, context_row(seriesTitle="Another Show")))
        self.assertFalse(audio.programme_matches(identity, context_row(seasonNumber=19)))
        self.assertFalse(audio.programme_matches(identity, context_row(episodeNumber=25)))
        self.assertFalse(audio.programme_matches(identity, context_row(originalAirDate="2026-06-17")))
        self.assertTrue(audio.programme_matches(identity, context_row(seasonNumber=20, episodeNumber=24,
                                                                     originalAirDate="2026-06-16")))


class PublicEvidenceResearchTests(unittest.TestCase):
    def adapter(self, pages, registry=None):
        return audio.PublicEvidenceResearch(pages.get, registry or {
            "wwenxt": {"official": [OFFICIAL_URL], "reports": [REPORT_URL]}})

    def test_fixture_pages_become_typed_official_and_report_evidence(self):
        adapter = self.adapter({OFFICIAL_URL: OFFICIAL_PAGE, REPORT_URL: REPORT_PAGE})
        official, reports = adapter({"programme": context_row(originalAirDate="2026-06-16")})
        self.assertEqual(len(official), 1)
        self.assertEqual(official[0].code(), "S20E24")
        self.assertEqual(official[0].episode_year(), 2026)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].original_air_date, "2026-06-16")
        verification = audio.corroborate_episode(audio.extract_clues(NXT_TEXT), official, reports, now=NOW)
        self.assertTrue(verification.accepted)
        self.assertEqual(verification.identity.broadcaster_code, "S20E24")

    def test_unreviewed_hosts_and_empty_registry_stay_inconclusive(self):
        pages = {OFFICIAL_URL: OFFICIAL_PAGE, REPORT_URL: REPORT_PAGE}
        adapter = self.adapter(pages, {"wwenxt": {"official": ["https://example.com/nxt"],
                                                  "reports": ["https://example.com/report"]}})
        self.assertEqual(adapter({"programme": context_row()}), ([], []))
        self.assertEqual(audio.PublicEvidenceResearch(pages.get)({"programme": context_row()}), ([], []))
        self.assertEqual(audio.PublicEvidenceResearch(pages.get)(
            {"programme": context_row(seriesTitle="Unknown Show")}), ([], []))

    def test_oversized_and_failed_pages_do_not_raise(self):
        def fetch(url):
            raise OSError("network down")
        adapter = audio.PublicEvidenceResearch(fetch, {"wwenxt": {"official": [OFFICIAL_URL],
                                                                  "reports": [REPORT_URL]}})
        self.assertEqual(adapter({"programme": context_row()}), ([], []))


class IntegrationObserverTests(unittest.TestCase):
    def observer(self, programme=None, activity=None, research=None, use_budget=True):
        wall = time.time()
        pages = {OFFICIAL_URL: OFFICIAL_PAGE, REPORT_URL: REPORT_PAGE}
        if research is None:
            research = audio.PublicEvidenceResearch(pages.get, {
                "wwenxt": {"official": [OFFICIAL_URL], "reports": [REPORT_URL]}})
        observer = audio.EpisodeObserver(
            lambda: current(now=wall), research, enabled=True,
            budget=audio.EpisodeBudget(clock=lambda: wall) if use_budget else None,
            programme=programme, activity=activity)
        job = {"cid": CHANNEL, "name": "CW Presents WWE NXT", "deviceVerified": True,
               "changed": False, "launchID": "launch-1", "streamGeneration": "generation-1",
               "visitID": "visit-1", "observedAt": wall - 5, "expiresAt": wall + 50,
               "sampleDigest": "b" * 32}
        decision = {"outcome": "allowed", "language": "en", "confidence": 0.97,
                    "speechSeconds": 21.0, "sampleSeconds": 32.0}
        return observer, job, decision

    def test_programme_and_visit_matching_promote_only_once(self):
        observer, job, decision = self.observer(
            programme=lambda cid: context_row(originalAirDate="2026-06-16"),
            activity=lambda cid: {"channel_id": cid, "active_seconds": 150, "hasEpisodeIdentity": False},
            use_budget=False)
        promotion = observer(job, decision, NXT_TEXT)
        self.assertTrue(promotion.accepted)
        self.assertEqual(promotion.episode.identity.broadcaster_code, "S20E24")
        self.assertEqual(observer(job, decision, NXT_TEXT).reason, "duplicate-publication")
        # A same-visit reload keeps the exact visit ID and cannot republish.
        self.assertEqual(observer(dict(job, streamGeneration="generation-2"), decision, NXT_TEXT).reason,
                         "duplicate-publication")
        self.assertIsNone(observer(dict(job, visitID="visit-2"), decision, NXT_TEXT))
        self.assertIsNone(observer(dict(job, visitID=""), decision, NXT_TEXT))

    def test_programme_mismatch_and_inactive_or_known_identity_are_rejected(self):
        observer, job, decision = self.observer(
            programme=lambda cid: context_row(seriesTitle="Another Show"),
            research=lambda payload: ([nxt_official()], nxt_reports()),
            activity=lambda cid: {"channel_id": cid, "active_seconds": 150, "hasEpisodeIdentity": False})
        self.assertEqual(observer(job, decision, NXT_TEXT).reason, "programme-mismatch")
        inactive, job2, decision2 = self.observer(
            programme=lambda cid: context_row(),
            activity=lambda cid: {"channel_id": cid, "active_seconds": 30, "hasEpisodeIdentity": False})
        self.assertIsNone(inactive(job2, decision2, NXT_TEXT))
        known, job3, decision3 = self.observer(
            programme=lambda cid: context_row(seasonNumber=20, episodeNumber=24),
            activity=lambda cid: {"channel_id": cid, "active_seconds": 150, "hasEpisodeIdentity": True})
        self.assertIsNone(known(job3, decision3, NXT_TEXT))

    def test_activity_session_must_match_current_channel(self):
        observer, job, decision = self.observer(
            programme=lambda cid: context_row(),
            activity=lambda cid: {"channel_id": "b" * 32, "active_seconds": 500,
                                  "hasEpisodeIdentity": False})
        self.assertIsNone(observer(job, decision, NXT_TEXT))


class VisitProvenanceTests(unittest.TestCase):
    """Audio callback provenance against the durable physical-visit ledger."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.ledger = language_visits.Ledger(self.root)
        self.pages = {OFFICIAL_URL: OFFICIAL_PAGE, REPORT_URL: REPORT_PAGE}
        self.research = audio.PublicEvidenceResearch(
            self.pages.get, {"wwenxt": {"official": [OFFICIAL_URL], "reports": [REPORT_URL]}})

    def job(self, visit_id, generation="generation-1", wall=None):
        wall = time.time() if wall is None else wall
        return {"cid": CHANNEL, "name": "CW Presents WWE NXT", "deviceVerified": True,
                "changed": False, "launchID": "launch-1", "streamGeneration": generation,
                "visitID": visit_id, "observedAt": wall - 5, "expiresAt": wall + 50,
                "sampleDigest": "b" * 32}

    def probe(self, visit_id, generation):
        return {"channelID": CHANNEL, "launchID": "launch-1",
                "streamGeneration": generation, "visitID": visit_id,
                "surfPaused": False, "surfing": True}

    def decision(self):
        return {"outcome": "allowed", "language": "en", "confidence": 0.97,
                "speechSeconds": 21.0, "sampleSeconds": 32.0}

    def observer(self, probe):
        return audio.EpisodeObserver(
            lambda: probe, self.research, enabled=True, budget=None,
            programme=lambda cid: context_row(originalAirDate="2026-06-16"),
            activity=lambda cid: {"channel_id": cid, "active_seconds": 150,
                                  "hasEpisodeIdentity": False})

    def test_same_launch_reload_is_one_visit_while_stale_visit_is_rejected(self):
        first = self.ledger.observe(CHANNEL, "launch-1", "generation-1", now=1000)
        reload = self.ledger.observe(CHANNEL, "launch-1", "generation-2", now=1010)
        self.assertTrue(language_visits.is_verified(first["visitID"]))
        self.assertEqual(first["visitID"], reload["visitID"])
        observer = self.observer(self.probe(reload["visitID"], "generation-2"))
        # The job was observed under generation-1; the probe shows a reload
        # under generation-2. The durable visit ID is unchanged, so this is the
        # same physical session and promotes.
        promoted = observer(self.job(first["visitID"], "generation-1"), self.decision(), NXT_TEXT)
        self.assertTrue(promoted.accepted)
        # A second callback in the same visit cannot republish.
        self.assertEqual(observer(self.job(reload["visitID"], "generation-2"),
                                  self.decision(), NXT_TEXT).reason, "duplicate-publication")
        # Departure and return creates a new physical visit; the old job is now
        # stale and must not publish against the new session.
        self.ledger.observe("b" * 32, "launch-1", "generation-9", now=1020)
        returned = self.ledger.observe(CHANNEL, "launch-1", "generation-3", now=1030)
        self.assertNotEqual(returned["visitID"], first["visitID"])
        later = self.observer(self.probe(returned["visitID"], "generation-3"))
        self.assertIsNone(later(self.job(first["visitID"], "generation-1"), self.decision(), NXT_TEXT))
        self.assertTrue(later(self.job(returned["visitID"], "generation-3"),
                              self.decision(), NXT_TEXT).accepted)

    def test_recovery_visit_promotes_with_exact_id_then_stale_recovery_is_rejected(self):
        # Simulate a lost durable ledger while evidence/marker survives.
        (self.root / "language-exclusions.json").write_text("{}")
        (self.root / language_visits.FILE_NAME).unlink(missing_ok=True)
        ledger = language_visits.Ledger(self.root)
        recovered = ledger.observe(CHANNEL, "launch-1", "generation-1", now=2000)
        self.assertTrue(recovered["verified"])
        self.assertTrue(language_visits.is_recovery(recovered["visitID"]))
        observer = self.observer(self.probe(recovered["visitID"], "generation-1"))
        self.assertTrue(observer(self.job(recovered["visitID"], "generation-1"),
                                 self.decision(), NXT_TEXT).accepted)
        # A directly observed departure/return restores a trusted identity and
        # makes the old recovery job stale.
        ledger.observe("b" * 32, "launch-1", "generation-9", now=2010)
        returned = ledger.observe(CHANNEL, "launch-1", "generation-10", now=2020)
        self.assertTrue(language_visits.is_verified(returned["visitID"]))
        later = self.observer(self.probe(returned["visitID"], "generation-10"))
        self.assertIsNone(later(self.job(recovered["visitID"], "generation-1"),
                                self.decision(), NXT_TEXT))


class GatewayAudioIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = {"JEALOUSFIN_STATE_DIR": self.temp.name, "JEALOUSFIN_USERNAME": "fixture",
               "JEALOUSFIN_PASSWORD": "fixture", "JEALOUSFIN_TOKEN": "fixture-token",
               "JEALOUSFIN_SERVER_ID": "fixture-server", "JEALOUSFIN_BIND": "127.0.0.1"}
        with patch.dict(os.environ, env):
            import gateway
            self.g = importlib.reload(gateway)
        self.g.channels[CHANNEL] = {"Id": CHANNEL, "Name": "CW Presents WWE NXT",
                                    "Type": "TvChannel"}
        self.g.sources[CHANNEL] = "nltv:us:pluto-6a30478d0be0264f4a4e227c"
        self.server = pathlib.Path(self.temp.name)

    def write_registry(self, official_url=OFFICIAL_URL, report_url=REPORT_URL):
        (self.server / "episode-sources.json").write_text(json.dumps({
            "schemaVersion": 1,
            "series": {"WWE NXT": {"official": [official_url], "reports": [report_url]}}}))

    def snapshot(self, **overrides):
        values = {"channelID": CHANNEL, "launchID": "launch-1",
                  "streamGeneration": "generation-1", "surfPaused": False, "surfing": True}
        values.update(overrides)
        return values

    def seed_visit(self, launch="launch-1", generation="generation-1"):
        result = self.g.language_visit_identity().observe(CHANNEL, launch, generation)
        return result["visitID"]

    def job(self, visit_id, generation="generation-1", wall=None):
        wall = time.time() if wall is None else wall
        return {"cid": CHANNEL, "name": "CW Presents WWE NXT", "deviceVerified": True,
                "changed": False, "launchID": "launch-1", "streamGeneration": generation,
                "visitID": visit_id, "observedAt": wall - 5, "expiresAt": wall + 50,
                "sampleDigest": "b" * 32, "activeSeconds": 150, "hasEpisodeIdentity": False}

    def decision(self):
        return {"outcome": "allowed", "language": "en", "confidence": 0.97,
                "speechSeconds": 21.0, "sampleSeconds": 32.0}

    def test_observer_is_constructed_default_off(self):
        observer = self.g.episode_observer_instance()
        self.assertIsNotNone(observer)
        self.assertFalse(observer.enabled)
        status = self.g.episode_audio_status()
        self.assertTrue(status["gatewayWired"])
        self.assertFalse(status["enabled"])
        self.assertFalse(status["active"])
        self.assertEqual(status["handoff"]["matches"], ["channel", "programme", "visit"])

    def test_registry_rejects_unapproved_hosts_and_oversized_files(self):
        self.write_registry()
        self.assertEqual(self.g.load_episode_sources()["wwenxt"]["official"], [OFFICIAL_URL])
        self.write_registry(official_url="https://example.com/nxt")
        self.assertEqual(self.g.load_episode_sources(), {})
        (self.server / "episode-sources.json").write_text("x" * (self.g.EPISODE_SOURCE_LIMIT + 1))
        self.assertEqual(self.g.load_episode_sources(), {})

    def test_gateway_fixture_promotes_from_existing_callback_and_is_idempotent(self):
        self.write_registry()
        pages = {OFFICIAL_URL: OFFICIAL_PAGE, REPORT_URL: REPORT_PAGE}
        wall = time.time()
        visit_id = self.seed_visit()
        snapshot = self.snapshot()
        with patch.object(self.g, "episode_research_page", side_effect=lambda url: pages[url]), \
                patch.object(self.g, "device_language_probe", return_value=snapshot), \
                patch.object(self.g, "programme_snapshot",
                             return_value={CHANNEL: {"title": "WWE NXT", "airDate": "2026-06-16"}}), \
                patch.object(self.g, "episode_active_view",
                             return_value={"channel_id": CHANNEL, "active_seconds": 150,
                                           "hasEpisodeIdentity": False}):
            observer = self.g.episode_observer_instance()
            observer.enabled = True
            job = self.job(visit_id, wall=wall)
            decision = self.decision()
            promotion = observer(job, decision, NXT_TEXT)
            self.assertTrue(promotion.accepted)
            self.assertEqual(promotion.episode.identity.broadcaster_code, "S20E24")
            # A repeated callback in the same physical visit is not republished.
            self.assertIsNone(observer(job, decision, NXT_TEXT))

    def test_programme_mismatch_prevents_publication(self):
        self.write_registry()
        wall = time.time()
        visit_id = self.seed_visit()
        snapshot = self.snapshot()
        with patch.object(self.g, "device_language_probe", return_value=snapshot), \
                patch.object(self.g, "programme_snapshot",
                             return_value={CHANNEL: {"title": "Another Show"}}), \
                patch.object(self.g, "episode_active_view",
                             return_value={"channel_id": CHANNEL, "active_seconds": 150,
                                           "hasEpisodeIdentity": False}):
            observer = self.g.episode_observer_instance()
            observer.enabled = True
            observer.research = lambda payload: ([nxt_official()], nxt_reports())
            self.assertEqual(observer(self.job(visit_id, wall=wall), self.decision(), NXT_TEXT).reason,
                             "programme-mismatch")

    def test_probe_preserves_fresh_visit_and_fails_closed_on_pause_or_stop(self):
        visit_id = self.seed_visit()
        with patch.object(self.g, "device_language_probe", return_value=self.snapshot()):
            probe = self.g.episode_probe()
        self.assertEqual(probe["visitID"], visit_id)
        self.assertTrue(probe["visitVerified"])
        for state in ({"surfPaused": True, "surfing": True},
                      {"surfPaused": False, "surfing": False},
                      {"surfPaused": None, "surfing": None}):
            with patch.object(self.g, "device_language_probe",
                              return_value=self.snapshot(**state)):
                self.assertIsNone(self.g.episode_probe())

    def test_pause_blocks_audio_callback_without_research(self):
        self.write_registry()
        wall = time.time()
        visit_id = self.seed_visit()
        calls = []
        observer = self.g.episode_observer_instance()
        observer.enabled = True
        observer.research = lambda payload: calls.append(payload) or ([], [])
        paused = self.snapshot(surfPaused=True, surfing=True)
        with patch.object(self.g, "device_language_probe", return_value=paused), \
                patch.object(self.g, "programme_snapshot",
                             return_value={CHANNEL: {"title": "WWE NXT"}}), \
                patch.object(self.g, "episode_active_view",
                             return_value={"channel_id": CHANNEL, "active_seconds": 150,
                                           "hasEpisodeIdentity": False}):
            self.assertIsNone(observer(self.job(visit_id, wall=wall), self.decision(), NXT_TEXT))
        self.assertEqual(calls, [])

    def test_same_launch_reload_promotes_once_and_stale_visit_is_rejected(self):
        self.write_registry()
        wall = time.time()
        first = self.seed_visit(generation="generation-1")
        reloaded = self.g.language_visit_identity().observe(
            CHANNEL, "launch-1", "generation-2")
        self.assertEqual(reloaded["visitID"], first)
        observer = self.g.episode_observer_instance()
        observer.enabled = True
        observer.research = lambda payload: ([nxt_official()], nxt_reports())
        with patch.object(self.g, "device_language_probe",
                          return_value=self.snapshot(streamGeneration="generation-2")), \
                patch.object(self.g, "programme_snapshot",
                             return_value={CHANNEL: {"title": "WWE NXT", "airDate": "2026-06-16"}}), \
                patch.object(self.g, "episode_active_view",
                             return_value={"channel_id": CHANNEL, "active_seconds": 150,
                                           "hasEpisodeIdentity": False}):
            # Job carries the visit's original generation; the probe shows a
            # reload with the same visit ID. This is one session and promotes.
            promotion = observer(self.job(first, generation="generation-1", wall=wall),
                                 self.decision(), NXT_TEXT)
            self.assertTrue(promotion.accepted)
            # A stale job from before a departure/return no longer matches.
            self.g.language_visit_identity().observe("b" * 32, "launch-1", "generation-9")
            returned = self.g.language_visit_identity().observe(
                CHANNEL, "launch-1", "generation-3")
            with patch.object(self.g, "device_language_probe",
                              return_value=self.snapshot(streamGeneration="generation-3")):
                self.assertIsNone(observer(self.job(first, generation="generation-1", wall=wall),
                                           self.decision(), NXT_TEXT))
                fresh = self.g.episode_observer_instance()
                fresh.enabled = True
                fresh.research = lambda payload: ([nxt_official()], nxt_reports())
                # Clear the bounded cross-session clue dedup window so the new
                # physical visit can be exercised within this deterministic test.
                fresh.budget = audio.EpisodeBudget(
                    clock=lambda: time.time() + audio.DEDUP_SECONDS + 1)
                self.assertTrue(fresh(self.job(returned["visitID"], generation="generation-3", wall=wall),
                                      self.decision(), NXT_TEXT).accepted)

    def test_runtime_opt_in_toggles_observer_without_restart(self):
        class FakeObserver:
            enabled = False

            def submit(self, job, decision, text):
                return None

        fake = FakeObserver()
        with patch.object(self.g, "reload_gateway_config", return_value=False), \
                patch.object(self.g, "episode_observer_instance", return_value=fake):
            self.g.gateway_config = dict(self.g.gateway_config, episodeAudioEnabled=True)
            self.g.episode_speech_callback(self.job("visit-1"), self.decision(), NXT_TEXT)
            self.assertTrue(fake.enabled)
            self.g.gateway_config = dict(self.g.gateway_config, episodeAudioEnabled=False)
            self.g.episode_speech_callback(self.job("visit-1"), self.decision(), NXT_TEXT)
            self.assertFalse(fake.enabled)

    def test_status_exposes_configured_effective_and_publication(self):
        status = self.g.episode_audio_status()
        self.assertIn("configuredEnabled", status)
        self.assertIn("publication", status)
        self.assertIn("observer", status)

    def test_publication_merges_only_when_no_stronger_metadata(self):
        wall = time.time()
        request = self.job("visit-1", wall=wall)
        binding = audio.SessionBinding(CHANNEL, "launch-1", "generation-1",
                                       wall - 5, wall + 50, "visit-1")
        request = audio.ClueRequest(audio.extract_clues(NXT_TEXT), binding,
                                    "CW Presents WWE NXT", "en", "allowed", 0.97, 21.0, 55.0,
                                    "b" * 32)
        promoted = audio.promote_episode(request, [nxt_official()], nxt_reports(),
                                         current(now=wall), wall,
                                         programme=context_row(originalAirDate="2026-06-16"))
        self.assertTrue(promoted.accepted)
        self.g.episode_publisher.entries.clear()
        self.assertTrue(self.g.episode_publisher.publish(
            promoted, context_row(originalAirDate="2026-06-16"), wall))
        weak = {CHANNEL: {"title": "WWE NXT", "source": "provider-catalogue"}}
        merged = self.g.merge_episode_publication(dict(weak), wall)
        self.assertEqual(merged[CHANNEL]["episodeSource"], "local-audio-episode-identity")
        self.assertEqual(merged[CHANNEL]["seasonNumber"], 20)
        strong = {CHANNEL: {"title": "WWE NXT", "source": "provider-catalogue",
                            "episodeTitle": "Guide Title", "seasonNumber": 20,
                            "episodeNumber": 24}}
        untouched = self.g.merge_episode_publication(dict(strong), wall)
        self.assertEqual(untouched[CHANNEL]["episodeTitle"], "Guide Title")
        self.assertNotIn("episodeSource", untouched[CHANNEL])


# ---------------------------------------------------------------------------
# Real public-source shape regressions (bounded fixtures from the 2025-11-04
# WWE NXT archive/results pages and two independent reports). No full page or
# transcript is stored.
# ---------------------------------------------------------------------------

WWE_ARCHIVE_URL = "https://www.wwe.com/shows/wwenxt/2025-11-04"
WWE_RESULTS_URL = "https://www.wwe.com/shows/wwenxt/2025-11-04/results"
WI_URL = ("https://www.wrestlinginc.com/2016429/wwe-nxt-november-4-six-woman-"
          "tag-team-match-jevon-evans-saquon-shugars-more/")
PWI_URL = ("https://pwinsider.com/article/201974/114-wwe-nxt-report-ricky-and-trick-"
           "talk-ethan-and-chelsea-celebrate-grande-americano-in-action-you-dont-"
           "want-to-be-crossing-jordan-and-more.html?p=1")

WWE_ARCHIVE_HTML = (
    "<html><head>"
    "<title>WWE NXT: Nov. 4, 2025 | WWE</title>"
    '<meta property="og:title" content="WWE NXT results, Nov. 4, 2025: Joe Hendry '
    'and Thea Hail ambush Ethan Page and Chelsea Green"/>'
    '<meta property="og:description" content="Check out everything that happened '
    'on the Nov. 4 episode of WWE NXT."/>'
    '<meta property="og:url" content="https://www.wwe.com/shows/wwenxt/2025-11-04"/>'
    "</head><body><p>Tuesday, Nov 4 | 8/7 PM C</p></body></html>"
)
WWE_RESULTS_HTML = (
    "<html><head>"
    "<title>WWE NXT results: Nov. 4, 2025 | WWE</title>"
    '<meta property="og:title" content="WWE NXT results: Nov. 4, 2025"/>'
    '<meta property="og:description" content="Fatal Influence def. NXT Women\'s '
    'Champion Tatum Paxley, Izzi Dame and Lola Vice - Six Woman Tag Team Match"/>'
    "</head><body><p>Full details</p></body></html>"
)
WI_HTML = (
    "<html><head>"
    "<title>WWE NXT Results 11/4 - Six Woman Tag Team Match, Je&apos;Von Evans "
    "Takes On Saquon Shugars &amp; More</title>"
    '<meta property="article:published_time" content="2025-11-05T00:00:00+00:00"/>'
    '<script type="application/ld+json">'
    + json.dumps({
        "@context": "http://schema.org",
        "@type": "NewsArticle",
        "headline": "WWE NXT Results 11/4 - Six Woman Tag Team Match, Je'Von Evans Takes On Saquon Shugars & More",
        "description": ("This is Wrestling Inc.'s results for \"WWE NXT\" on November 4, 2025, "
                        "with a singles match between Je'Von Evans and Saquon Shugars of DarkState!"),
        "datePublished": "2025-11-05T00:00:00+00:00",
        "dateModified": "2025-11-05T02:46:04+00:00",
    })
    + "</script></head><body><p>Welcome to Wrestling Inc.'s results for WWE NXT.</p></body></html>"
)
PWI_HTML = (
    "<html><head>"
    "<title>11/4 WWE NXT REPORT: RICKY AND TRICK TALK, ETHAN AND CHELSEA "
    "'CELEBRATE', GRANDE AMERICANO IN ACTION | PWInsider.com</title>"
    '<meta name="description" content=""/>'
    "</head><body>"
    "<p>11/4 WWE NXT REPORT: RICKY AND TRICK TALK</p>"
    "<p>By Richard Trionfo on 2025-11-04 22:00:00</p>"
    "<p>Match Number One: Tatum Paxley, Izzi Dame, and Lola Vice versus Jacy Jayne.</p>"
    "</body></html>"
)


class RealSourceParserTests(unittest.TestCase):
    def test_official_archive_without_jsonld_or_numbers(self):
        rows = audio.parse_official_page(WWE_ARCHIVE_HTML, WWE_ARCHIVE_URL, "WWE NXT")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.series_title, "WWE NXT")
        self.assertEqual(row.original_air_date, "2025-11-04")
        self.assertIsNone(row.season_number)
        self.assertIsNone(row.episode_number)
        self.assertEqual(row.code(), "")
        self.assertIn("Joe Hendry", row.episode_title)
        self.assertTrue(row.checked())

    def test_official_results_page_uses_explicit_date(self):
        rows = audio.parse_official_page(WWE_RESULTS_HTML, WWE_RESULTS_URL, "WWE NXT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].original_air_date, "2025-11-04")
        self.assertIsNone(rows[0].season_number)
        self.assertEqual(rows[0].episode_title, "Nov. 4, 2025")

    def test_independent_jsonld_report_uses_show_date_not_publish_date(self):
        rows = audio.parse_report_page(WI_HTML, WI_URL, {"seriesTitle": "WWE NXT"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].original_air_date, "2025-11-04")
        self.assertEqual(rows[0].published_at, "2025-11-05")
        self.assertTrue(rows[0].terms)
        self.assertFalse(rows[0].rejection())

    def test_independent_html_report_without_jsonld(self):
        rows = audio.parse_report_page(PWI_HTML, PWI_URL, {"seriesTitle": "WWE NXT"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].original_air_date, "2025-11-04")
        self.assertEqual(rows[0].published_at, "2025-11-04")
        self.assertEqual(rows[0].series_title, "WWE NXT")
        self.assertTrue(rows[0].terms)

    def test_date_only_official_requires_independent_corroboration(self):
        official = audio.parse_official_page(WWE_ARCHIVE_HTML, WWE_ARCHIVE_URL, "WWE NXT")
        reports = (audio.parse_report_page(WI_HTML, WI_URL, {"seriesTitle": "WWE NXT"})
                   + audio.parse_report_page(PWI_HTML, PWI_URL, {"seriesTitle": "WWE NXT"}))
        clues = audio.extract_clues("Je'Von Evans takes on Saquon Shugars of DarkState")
        missing = audio.corroborate_episode(clues, official, [], now=NOW)
        self.assertFalse(missing.accepted)
        self.assertEqual(missing.reason, "no-independent-report")
        verified = audio.corroborate_episode(clues, official, reports, now=NOW)
        self.assertTrue(verified.accepted)
        self.assertEqual(verified.identity.original_air_date, "2025-11-04")
        self.assertIsNone(verified.identity.season_number)
        self.assertIsNone(verified.identity.episode_number)
        self.assertEqual(verified.identity.broadcaster_code, "")
        # A provider numbering already present in programme metadata is not
        # contradicted merely because the official archive omitted numbers.
        context = context_row(seasonNumber=20, episodeNumber=24, originalAirDate="2025-11-04")
        self.assertTrue(audio.programme_matches(verified.identity, context))
        self.assertFalse(audio.programme_matches(verified.identity,
                                                 context_row(originalAirDate="2025-11-05")))

    def test_date_only_official_still_requires_matching_clues(self):
        official = audio.parse_official_page(WWE_ARCHIVE_HTML, WWE_ARCHIVE_URL, "WWE NXT")
        reports = [audio.parse_report_page(WI_HTML, WI_URL, {"seriesTitle": "WWE NXT"})[0]]
        self.assertFalse(audio.corroborate_episode(("the", "tonight"), official, reports,
                                                   now=NOW).accepted)


class PublicationStoreTests(unittest.TestCase):
    def promotion(self, now=NOW, identity=None):
        request = nxt_request(now)
        promoted = audio.promote_episode(request, [nxt_official()], nxt_reports(), current(now), now)
        self.assertTrue(promoted.accepted)
        return promoted

    def test_publish_is_bounded_expiring_and_contains_no_transcript_or_clues(self):
        store = audio.PublicationStore(ttl=60, clock=lambda: NOW)
        promoted = self.promotion()
        context = context_row(originalAirDate="2026-06-16")
        self.assertTrue(store.publish(promoted, context, NOW))
        row = store.get(CHANNEL, NOW)
        self.assertIsNotNone(row)
        self.assertEqual(row["episodeSource"], "local-audio-episode-identity")
        self.assertEqual(row["seasonNumber"], 20)
        self.assertEqual(row["episodeNumber"], 24)
        self.assertEqual(row["originalAirDate"], "2026-06-16")
        text = json.dumps(row)
        self.assertNotIn("zaria", text.casefold())
        self.assertNotIn("transcript", text.casefold())
        self.assertNotIn("clues", text.casefold())
        self.assertIsNone(store.get(CHANNEL, NOW + 61))
        self.assertEqual(store.snapshot(NOW + 61), {})

    def test_publish_rejects_programme_mismatch_and_never_invents_numbers(self):
        store = audio.PublicationStore(clock=lambda: NOW)
        promoted = self.promotion()
        self.assertFalse(store.publish(promoted, context_row(seriesTitle="Another Show"), NOW))
        official = audio.parse_official_page(WWE_ARCHIVE_HTML, WWE_ARCHIVE_URL, "WWE NXT")
        reports = [audio.parse_report_page(WI_HTML, WI_URL, {"seriesTitle": "WWE NXT"})[0]]
        request = nxt_request(now=NOW, clues=audio.extract_clues(
            "Je'Von Evans takes on Saquon Shugars of DarkState"))
        date_only = audio.promote_episode(request, official, reports, current(NOW), NOW,
                                          programme=context_row(originalAirDate="2025-11-04"))
        self.assertTrue(date_only.accepted)
        self.assertTrue(store.publish(date_only, context_row(originalAirDate="2025-11-04"), NOW))
        row = store.get(CHANNEL, NOW)
        self.assertNotIn("seasonNumber", row)
        self.assertNotIn("episodeNumber", row)
        self.assertEqual(row["originalAirDate"], "2025-11-04")

    def test_existing_stronger_metadata_is_never_replaced(self):
        self.assertFalse(audio.programme_has_stronger_episode_metadata({"title": "WWE NXT"}))
        self.assertTrue(audio.programme_has_stronger_episode_metadata(
            {"title": "WWE NXT", "episodeTitle": "Guide Title"}))
        self.assertTrue(audio.programme_has_stronger_episode_metadata(
            {"title": "WWE NXT", "seasonNumber": 20, "episodeNumber": 24}))


class ObserverFreshnessTests(unittest.TestCase):
    def job(self, wall=None):
        wall = time.time() if wall is None else wall
        return {"cid": CHANNEL, "name": "CW Presents WWE NXT", "deviceVerified": True,
                "changed": False, "launchID": "launch-1", "streamGeneration": "generation-1",
                "visitID": "visit-1", "observedAt": wall - 5, "expiresAt": wall + 50,
                "sampleDigest": "b" * 32}

    def decision(self):
        return {"outcome": "allowed", "language": "en", "confidence": 0.97,
                "speechSeconds": 21.0, "sampleSeconds": 55.0}

    def test_research_reprobes_and_rejects_a_changed_physical_visit(self):
        wall = time.time()
        probes = [current(now=wall), current(now=wall, visitID="visit-2")]
        observer = audio.EpisodeObserver(
            lambda: probes.pop(0), lambda payload: ([nxt_official()], nxt_reports()),
            enabled=True, programme=lambda cid: context_row(originalAirDate="2026-06-16"),
            activity=lambda cid: {"channel_id": cid, "active_seconds": 60,
                                  "hasEpisodeIdentity": False})
        self.assertEqual(observer(self.job(wall), self.decision(), NXT_TEXT).reason,
                         "session-changed")

    def test_research_reprobes_and_rejects_a_changed_programme(self):
        wall = time.time()
        contexts = [context_row(originalAirDate="2026-06-16"),
                    context_row(seriesTitle="Another Show", originalAirDate="2026-06-16")]
        observer = audio.EpisodeObserver(
            lambda: current(now=wall), lambda payload: ([nxt_official()], nxt_reports()),
            enabled=True, programme=lambda cid: contexts.pop(0),
            activity=lambda cid: {"channel_id": cid, "active_seconds": 60,
                                  "hasEpisodeIdentity": False})
        self.assertEqual(observer(self.job(wall), self.decision(), NXT_TEXT).reason,
                         "programme-changed")

    def test_async_submit_does_not_retain_transcript_and_publishes(self):
        wall = time.time()
        payloads = []
        publisher = audio.PublicationStore(clock=lambda: time.time())
        observer = audio.EpisodeObserver(
            lambda: current(now=time.time()),
            lambda payload: payloads.append(payload) or ([nxt_official()], nxt_reports()),
            enabled=True, budget=audio.EpisodeBudget(clock=lambda: time.time()),
            programme=lambda cid: context_row(originalAirDate="2026-06-16"),
            activity=lambda cid: {"channel_id": cid, "active_seconds": 60,
                                  "hasEpisodeIdentity": False},
            publisher=publisher)
        self.assertIsNone(observer.submit(self.job(wall), self.decision(), NXT_TEXT))
        self.assertTrue(observer.flush(5))
        self.assertEqual(len(payloads), 1)
        self.assertIn("zaria", payloads[0]["clues"])
        self.assertNotIn("Zaria becomes", json.dumps(payloads[0]))
        self.assertIsNotNone(publisher.get(CHANNEL))
        self.assertEqual(observer.diagnostics()["submitted"], 1)


class SampleBackedActiveViewingTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.budget = audio.EpisodeBudget(clock=lambda: self.now[0])
        self.view = {"channel_id": CHANNEL, "session_hash": "session-1"}

    def eligible(self, active, sample):
        view = dict(self.view, active_seconds=active)
        return self.budget.eligible(
            view, has_episode_identity=False, profile="american", language="en",
            language_outcome="allowed", now=self.now[0], sample_seconds=sample)

    def test_verified_sample_window_is_eligible_without_lowering_language_gates(self):
        self.assertEqual(self.eligible(55, 55), (True, ""))
        self.assertEqual(self.eligible(54, 55), (False, "inactive"))
        self.assertEqual(self.eligible(55, 39), (False, "inactive"))
        self.assertEqual(self.eligible(119, 0), (False, "inactive"))
        self.assertEqual(self.eligible(120, 0), (True, ""))


if __name__ == "__main__":
    unittest.main()

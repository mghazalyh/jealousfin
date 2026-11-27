"""Generalized local spoken-language triage for the English IPTV trial.

Reuses the verified sampler/decoder/whisper pipeline from spanish_audio but
generalizes the decision: confidently identified non-English European
languages are eligible for exclusion; English, Chinese (including Cantonese)
and Korean are allowed; other non-European languages remain eligible. Exact
detected language and confidence are recorded. Mixed, ambiguous, silent and
unauthorized samples are never excluded.
"""
import json
import pathlib
import shutil
import tempfile
import time
import uuid
from types import SimpleNamespace

import language_catalog
import language_visits
import spanish_audio as base

REVISION = "language-auto-v1"
MIN_LANGUAGE_WINDOWS = 3
MIN_LANGUAGE_PROBABILITY = 0.82
MIN_WINDOW_PROBABILITY = base.MIN_WINDOW_PROBABILITY
MIN_LEXICAL_MARGIN = -0.05
MAX_PORTUGUESE_MARGIN = 0.05
STRICT_NO_LEXICAL_PROBABILITY = 0.92
SAME_VISIT_RETRY_SECONDS = base.SAME_VISIT_RETRY_SECONDS
MAX_SAME_VISIT_ATTEMPTS = base.MAX_SAME_VISIT_ATTEMPTS
STATE_PUBLISH_SECONDS = 10.0
# An episode lookup rejected for a commercial/no-match reason may request a
# fresh, episode-only sample in the same physical visit. It reuses already
# fetched media bytes, never records another language evidence event and is
# bounded independently of the language retry marker.
EPISODE_RETRY_SECONDS = 8.0
MAX_EPISODE_RETRY_ATTEMPTS = 2


def _decision_language(outcome, reason, confidence, analysis, window_results, lexical,
                       language=None, full_language=None):
    margin = lexical[0] if lexical else None
    return {
        "outcome": outcome,
        "reason": reason,
        "confidence": round(float(confidence), 4),
        "language": language,
        "languageName": language_catalog.language_name(language) if language else None,
        "speechSeconds": analysis["speechSeconds"],
        "sampleSeconds": analysis["duration"],
        "speechCoverage": analysis["coverage"],
        "windows": len(window_results),
        "languages": [
            {"language": w["language"], "probability": round(float(w["probability"]), 4)}
            for w in window_results if w.get("language") and w.get("probability") is not None
        ],
        "lexicalMargin": margin,
        "fullLanguage": full_language,
    }


def decide_language(analysis, window_results, lexical, full_language=None):
    """Conservative generalized decision across separated speech windows."""
    usable = [w for w in window_results
              if w.get("usable") and w.get("language") and w.get("probability") is not None]
    if analysis["duration"] < base.MIN_SAMPLE_SECONDS:
        return _decision_language("inconclusive", "sample_too_short", 0.0, analysis, window_results, lexical)
    if analysis["speechSeconds"] < base.MIN_SPEECH_SECONDS:
        return _decision_language("inconclusive", "insufficient_speech", 0.0, analysis, window_results, lexical)
    if analysis["coverage"] < base.MIN_COVERAGE:
        return _decision_language("inconclusive", "low_speech_coverage", 0.0, analysis, window_results, lexical)
    if len(usable) < MIN_LANGUAGE_WINDOWS:
        return _decision_language("inconclusive", "insufficient_speech_windows", 0.0, analysis, window_results, lexical)
    confident = [w for w in usable if w["probability"] >= MIN_WINDOW_PROBABILITY]
    low = [w for w in usable if w["probability"] < MIN_WINDOW_PROBABILITY]
    if len(confident) < MIN_LANGUAGE_WINDOWS:
        return _decision_language("inconclusive", "ambiguous_language", 0.0, analysis, window_results, lexical)
    if low:
        return _decision_language("inconclusive", "ambiguous_language", 0.0, analysis, window_results, lexical)
    languages = {w["language"] for w in confident}
    if len(languages) != 1:
        return _decision_language("inconclusive", "mixed_language", 0.0, analysis, window_results, lexical)
    code = next(iter(languages))
    mean_probability = sum(w["probability"] for w in confident) / len(confident)
    if mean_probability < MIN_LANGUAGE_PROBABILITY:
        return _decision_language("inconclusive", "low_language_confidence", mean_probability, analysis, window_results, lexical, code)
    if full_language and full_language in language_catalog.SUPPORTED and full_language != code:
        return _decision_language("inconclusive", "transcript_language_mismatch", mean_probability, analysis, window_results, lexical, code, full_language)
    category = language_catalog.classify(code)
    if category == "allowed":
        return _decision_language("allowed", "allowed_language_" + code, mean_probability, analysis, window_results, lexical, code, full_language)
    if category == "unauthorized":
        return _decision_language("unauthorized", "unauthorized_language_" + code, mean_probability, analysis, window_results, lexical, code, full_language)
    if category != "excluded":
        return _decision_language("inconclusive", "unknown_language", mean_probability, analysis, window_results, lexical, code, full_language)
    margin = lexical[0] if lexical else None
    if code in ("es", "pt"):
        if margin is not None:
            if code == "es" and margin < MIN_LEXICAL_MARGIN:
                return _decision_language("inconclusive", "language_confusion_es_pt", mean_probability, analysis, window_results, lexical, code, full_language)
            if code == "pt" and margin > MAX_PORTUGUESE_MARGIN:
                return _decision_language("inconclusive", "language_confusion_pt_es", mean_probability, analysis, window_results, lexical, code, full_language)
        elif mean_probability < STRICT_NO_LEXICAL_PROBABILITY:
            return _decision_language("inconclusive", "lexical_check_unavailable", mean_probability, analysis, window_results, lexical, code, full_language)
    return _decision_language("excluded", "language_" + code + "_dominant", mean_probability, analysis, window_results, lexical, code, full_language)


def decide(analysis, window_results, lexical, full_language=None):
    """Backward-compatible Spanish-era wrapper used by legacy tests."""
    decision = decide_language(analysis, window_results, lexical, full_language)
    if decision["outcome"] == "excluded":
        decision["outcome"] = "spanish" if decision["language"] == "es" else "non_spanish"
    elif decision["outcome"] in ("allowed", "unauthorized"):
        decision["outcome"] = "non_spanish"
    return decision


class Manager(base.Manager):
    """Generalized manager; sampler/decoder/AES are inherited from base."""

    def __init__(self, root, model_path, on_exclusion, control, probe=None, log=None,
                 ffmpeg=None, whisper=None, threads=4, visits=None, on_speech=None,
                 on_state=None):
        self.on_exclusion = on_exclusion
        self.on_speech = on_speech
        self.on_state = on_state
        self._state_published = {}
        self._episode_retries = {}
        self._episode_retry_results = []
        super().__init__(root, model_path, None, control, probe=probe, log=log,
                         ffmpeg=ffmpeg, whisper=whisper, threads=threads, visits=visits)

    def _empty_state(self):
        return {
            "schemaVersion": 1,
            "revision": REVISION,
            "updatedAt": 0,
            "counters": {
                "tested": 0, "excluded": 0, "spanish": 0, "nonSpanish": 0,
                "allowed": 0, "unauthorized": 0, "inconclusive": 0,
                "errors": 0, "suppressed": 0, "segmentsDecoded": 0,
                "segmentsRejected": 0, "segmentsDecrypted": 0, "keyMissing": 0,
                "samplesFinalized": 0, "evidence": 0, "enforced": 0,
                "duplicates": 0, "stale": 0, "deferred": 0,
            },
            "languages": {},
            "last": None,
            "current": None,
            "fault": self.fault,
        }

    def _load_state(self):
        self.state_file = self.root / "language-auto-state.json"
        state = self._empty_state()
        try:
            if self.state_file.exists() and self.state_file.stat().st_size <= 128 * 1024:
                raw = json.loads(self.state_file.read_text(encoding="utf-8"))
                if raw.get("schemaVersion") == 1 and raw.get("revision") == REVISION:
                    counters = raw.get("counters")
                    if isinstance(counters, dict):
                        for key in state["counters"]:
                            if type(counters.get(key)) is int and counters[key] >= 0:
                                state["counters"][key] = counters[key]
                    languages = raw.get("languages")
                    if isinstance(languages, dict):
                        for code, count in languages.items():
                            if isinstance(code, str) and type(count) is int and count >= 0:
                                state["languages"][code] = count
                    if isinstance(raw.get("last"), dict):
                        state["last"] = raw["last"]
                    state["fault"] = self.fault
                    return state
            legacy = self.root / "spanish-auto-state.json"
            if legacy.exists() and legacy.stat().st_size <= 128 * 1024:
                raw = json.loads(legacy.read_text(encoding="utf-8"))
                old = raw.get("counters") if isinstance(raw, dict) else None
                if isinstance(old, dict):
                    for key in ("tested", "spanish", "nonSpanish", "inconclusive", "errors",
                                "suppressed", "segmentsDecoded", "segmentsRejected",
                                "segmentsDecrypted", "keyMissing", "samplesFinalized"):
                        if type(old.get(key)) is int and old[key] >= 0:
                            state["counters"][key] = old[key]
                    state["counters"]["excluded"] = old.get("spanish", 0)
                    state["counters"]["allowed"] = old.get("nonSpanish", 0)
                    state["languages"] = {"es": old.get("spanish", 0)} if old.get("spanish", 0) else {}
                if isinstance(raw.get("last"), dict):
                    state["last"] = dict(raw["last"])
                    if state["last"].get("outcome") == "spanish":
                        state["last"]["language"] = "es"
                        state["last"]["languageName"] = "Spanish"
        except (OSError, ValueError, TypeError):
            pass
        state["fault"] = self.fault
        return state

    def start(self):
        started = super().start()
        if started:
            self.log("language auto ready model=" + pathlib.Path(self.model_path).name)
        return started

    def _infer(self, job):
        if not self._enabled():
            if job.get("episodeOnly"):
                self._mark_finalized_result(job, "episode-retry", True, "episode_only")
            else:
                self._mark_finalized_result(job, "inconclusive", False, "observation_disabled")
            return
        workdir = tempfile.mkdtemp(prefix="job-", dir=self._tmpdir)
        transcript = None
        try:
            analysis = base.analyze_speech(job["pcm"])
            if analysis["duration"] < base.MIN_SAMPLE_SECONDS or analysis["speechSeconds"] < base.MIN_SPEECH_SECONDS:
                decision = _decision_language(
                    "inconclusive",
                    "sample_too_short" if analysis["duration"] < base.MIN_SAMPLE_SECONDS else "insufficient_speech",
                    0.0, analysis, [], None,
                )
            else:
                window_results = []
                for window in analysis["windows"]:
                    if not window["usable"]:
                        continue
                    start = int(window["start"] * base.RATE) * base.WIDTH
                    end = int((window["start"] + window["duration"]) * base.RATE) * base.WIDTH
                    path = pathlib.Path(workdir) / ("w%d.wav" % window["index"])
                    base._write_wav(path, job["pcm"][start:end])
                    detected = base.detect_language(path, self.whisper, self.model_path, self.threads)
                    if detected:
                        window_results.append({
                            "index": window["index"], "start": window["start"],
                            "duration": window["duration"], "speechSeconds": window["speechSeconds"],
                            "speechRatio": window["speechRatio"], "usable": True,
                            "language": detected[0], "probability": detected[1],
                        })
                decision = decide_language(analysis, window_results, None, None)
                if base.decision_needs_transcript(decision):
                    full = pathlib.Path(workdir) / "full.wav"
                    base._write_wav(full, job["pcm"])
                    transcript = base.transcribe(full, self.whisper, self.model_path, workdir, self.threads)
                    lexical = base.lexical_margin(transcript[1]) if transcript else (None, 0, 0)
                    decision = decide_language(analysis, window_results, lexical,
                                               transcript[0] if transcript else None)
            self._notify_speech(job, decision, transcript)
            if job.get("episodeOnly"):
                # Reuse the transcript for episode clues only. Never record a
                # second language result, evidence event or visit count.
                self._finish_episode_retry(job, decision)
            else:
                self._record_result(job, decision)
        except Exception as error:
            if job.get("episodeOnly"):
                self._mark_finalized_result(job, "episode-retry", True, "episode_only")
                self.log("EPISODE_RETRY_RESULT channel=%s outcome=inconclusive reason=inference_error" % (
                    job.get("cid"),))
                raise
            self._mark_finalized_result(job, "inconclusive", False, "inference_error")
            method = getattr(self, "_state_inconclusive", None)
            if method is not None:
                sample = SimpleNamespace(
                    provider=job.get("provider"), name=job.get("name"),
                    probe={"launchID": job.get("launchID", ""),
                           "streamGeneration": job.get("streamGeneration", ""),
                           "surfPaused": job.get("surfPaused"),
                           "surfing": job.get("surfing")},
                    visit={"visitID": job.get("visitID", ""),
                           "verified": bool(job.get("visitID"))})
                marker = self._finalized.get(job.get("cid"))
                method(job.get("cid"), sample, marker or {}, "inference_error", job.get("seconds"))
            raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _verified_visit(visit):
        if not isinstance(visit, dict) or visit.get("verified") is not True:
            return ""
        visit_id = visit.get("visitID")
        if not isinstance(visit_id, str) or not visit_id:
            return ""
        return visit_id if language_visits.is_verified(visit_id) else ""

    def request_episode_retry(self, view, reason=""):
        """Request one bounded episode-only sample in the same physical visit.

        Called by the episode observer after a commercial/no-match rejection.
        No language decision is recorded when the retry completes, so the one
        conclusive language result for the visit is never counted twice.
        """
        if not isinstance(view, dict):
            return False
        cid = view.get("cid")
        visit_id = view.get("visitID")
        if not isinstance(cid, str) or not cid:
            return False
        if not isinstance(visit_id, str) or not language_visits.is_verified(visit_id):
            return False
        now = time.time()
        key = (cid, visit_id)
        with self._lock:
            row = self._episode_retries.get(key)
            if row is None:
                row = {"attempts": 0, "requested": 0.0, "reason": ""}
                self._episode_retries[key] = row
            if row["attempts"] >= MAX_EPISODE_RETRY_ATTEMPTS:
                self.log("EPISODE_RETRY_SUPPRESSED channel=%s reason=retry_limit" % cid)
                return False
            row["requested"] = now
            row["reason"] = str(reason or "")[:40]
            while len(self._episode_retries) > 128:
                self._episode_retries.pop(next(iter(self._episode_retries)))
            label = row["reason"] or "-"
        self.log("EPISODE_RETRY_REQUEST channel=%s reason=%s" % (cid, label))
        return True

    def _episode_retry_ready(self, cid, visit, now):
        visit_id = self._verified_visit(visit)
        if not visit_id or not isinstance(cid, str):
            return False
        rows = getattr(self, "_episode_retries", None)
        if not isinstance(rows, dict):
            return False
        with self._lock:
            row = rows.get((cid, visit_id))
            if row is None or row["attempts"] >= MAX_EPISODE_RETRY_ATTEMPTS:
                return False
            requested = row.get("requested") or 0.0
            return bool(requested and now - requested >= EPISODE_RETRY_SECONDS)

    def _take_episode_retry(self, cid, visit, now):
        visit_id = self._verified_visit(visit)
        if not visit_id:
            return False
        with self._lock:
            if not self._episode_retry_ready(cid, visit, now):
                return False
            row = self._episode_retries.get((cid, visit_id))
            if row is None:
                return False
            row["attempts"] += 1
            row["requested"] = 0.0
            return True

    def _finish_episode_retry(self, job, decision):
        """Complete an episode-only sample without touching language evidence."""
        self._mark_finalized_result(job, "episode-retry", True, "episode_only")
        record = {
            "at": round(time.time(), 3), "channelID": job.get("cid"),
            "visitID": job.get("visitID", ""), "outcome": decision.get("outcome"),
            "reason": decision.get("reason"), "language": decision.get("language"),
            "sampleSeconds": decision.get("sampleSeconds"),
        }
        self._episode_retry_results.append(record)
        while len(self._episode_retry_results) > 16:
            self._episode_retry_results.pop(0)
        self.log("EPISODE_RETRY_RESULT channel=%s outcome=%s reason=%s" % (
            job.get("cid"), decision.get("outcome"), decision.get("reason")))
        self._persist(force=True)

    def _notify_speech(self, job, decision, transcript):
        """Hand off only an allowed, device-verified transient transcript.

        The callback must never persist the text. It receives a sanitized job
        view (no PCM bytes) plus the already-computed language decision. This
        is the adapter boundary for optional local episode identification; it
        acts on the existing single decode/transcribe pass and never starts a
        second capture or Whisper run.

        The observation time is the handoff time, not the sample start, so a
        long sample cannot create an already-expired binding. The durable
        ``visitID`` from the language visit ledger is passed through unchanged;
        the observer re-probes the device and requires the same exact visit.
        """
        if self.on_speech is None or not transcript:
            return
        if not isinstance(decision, dict) or decision.get("outcome") != "allowed":
            return
        if job.get("deviceVerified") is not True or job.get("changed"):
            return
        now = time.time()
        view = {
            "cid": job.get("cid"), "provider": job.get("provider"), "name": job.get("name"),
            "seconds": job.get("seconds"), "deviceVerified": job.get("deviceVerified"),
            "changed": bool(job.get("changed")), "launchID": job.get("launchID", ""),
            "streamGeneration": job.get("streamGeneration", ""), "visitID": job.get("visitID", ""),
            "observedAt": now, "expiresAt": None,
            "sampleDigest": job.get("sampleDigest"), "profile": job.get("profile", "american"),
            "episodeOnly": bool(job.get("episodeOnly")),
        }
        try:
            result = self.on_speech(view, dict(decision), transcript[1])
            # A synchronous observer (tests or a reviewed synchronous adapter)
            # returns a Promotion. The gateway adapter queues the bounded clues
            # and returns None immediately so research can never block language
            # counting. Never log the transcript or spoken clues here.
            accepted = getattr(result, "accepted", None)
            reason = getattr(result, "reason", None)
            if accepted is True:
                self.log("EPISODE_IDENTITY_PROMOTED")
            elif accepted is False and reason:
                self.log("EPISODE_IDENTITY_SKIPPED reason=" + str(reason)[:48])
        except Exception as error:
            self.log("episode clue handoff error " + type(error).__name__)

    def _identity_matches(self, job, probe, allow_stream_reload=False):
        if not isinstance(probe, dict) or not probe.get("channelID"):
            return False
        if probe.get("channelID") != job["cid"]:
            return False
        expected_launch = job.get("launchID")
        current_launch = probe.get("launchID")
        if expected_launch and current_launch and expected_launch != current_launch:
            return False
        expected_stream = job.get("streamGeneration")
        current_stream = probe.get("streamGeneration")
        if (not allow_stream_reload and expected_stream and current_stream
                and expected_stream != current_stream):
            return False
        return True

    def _job_is_current(self, job, probe):
        """Reject stale jobs while allowing a reload within one physical visit."""
        visit_id = job.get("visitID", "")
        verified = language_visits.is_verified(visit_id)
        if not self._identity_matches(job, probe, allow_stream_reload=verified):
            return False
        if not verified:
            return True
        visit = self._observe_visit(probe)
        return bool(visit.get("verified") and visit.get("visitID") == visit_id)

    def _publish_state(self, state, cid, provider, name, *, reason="", seconds=None,
                       target=None, attempt=None, max_attempts=None, retry_seconds=None,
                       surf_paused=None, surfing=None, language=None, language_name=None,
                       outcome=None, visit_id="", launch_id="", stream_generation="",
                       conclusive=False, at=None):
        """Push one bounded, event-driven overlay state through the adapter.

        The view contains no PCM, transcript, URL or credential.  The adapter
        re-probes the current channel before writing the single overlay field,
        and stale channel/visit/stream results are suppressed there.
        """
        callback = getattr(self, "on_state", None)
        if callback is None:
            return False
        now = time.time() if at is None else at
        if not isinstance(cid, str) or not cid:
            return False
        view = {
            "channelID": cid, "channelName": str(name or provider or cid)[:160],
            "state": state, "reason": str(reason or "")[:80],
            "sampleSeconds": round(float(seconds), 2) if isinstance(seconds, (int, float)) else None,
            "targetSeconds": round(float(target), 2) if isinstance(target, (int, float)) else None,
            "attempt": attempt if type(attempt) is int and attempt >= 0 else None,
            "maxAttempts": max_attempts if type(max_attempts) is int and max_attempts >= 0 else None,
            "retrySeconds": round(float(retry_seconds), 1) if isinstance(retry_seconds, (int, float)) else None,
            "surfPaused": surf_paused if type(surf_paused) is bool else None,
            "surfing": surfing if type(surfing) is bool else None,
            "language": language if isinstance(language, str) else None,
            "languageName": language_name if isinstance(language_name, str) else None,
            "outcome": outcome if isinstance(outcome, str) else None,
            "visitID": str(visit_id or ""), "launchID": str(launch_id or ""),
            "streamGeneration": str(stream_generation or ""),
            "conclusive": bool(conclusive), "observedAt": now,
        }
        try:
            callback(view)
            return True
        except Exception as error:
            self.log("language state callback error " + type(error).__name__)
            return False

    def _session_from_sample(self, sample, marker=None):
        marker = marker if isinstance(marker, dict) else {}
        probe = getattr(sample, "probe", None)
        probe = probe if isinstance(probe, dict) else {}
        visit = getattr(sample, "visit", None)
        visit = visit if isinstance(visit, dict) else {}
        return {
            "visitID": str(visit.get("visitID") or marker.get("visitID") or ""),
            "launchID": str((probe or {}).get("launchID") or marker.get("launchID") or ""),
            "streamGeneration": str((probe or {}).get("streamGeneration") or marker.get("streamGeneration") or ""),
            "surfPaused": (probe or {}).get("surfPaused"),
            "surfing": (probe or {}).get("surfing"),
            "attempts": marker.get("attempts"),
            "maxAttempts": marker.get("maxAttempts", MAX_SAME_VISIT_ATTEMPTS),
            "sampleToken": marker.get("sampleToken"),
        }

    def _update_current(self):
        super()._update_current()
        if not self._samples:
            return
        cid, sample = max(self._samples.items(), key=lambda item: item[1].last_tap)
        best = self._best_base(sample)
        if best is None:
            return
        marker = self._finalized.get(cid)
        visit = sample.visit if isinstance(sample.visit, dict) else {}
        visit_id = str(visit.get("visitID") or "")
        if getattr(sample, "episode_only", False):
            # Episode-only retries never own the language label.
            return
        attempt = 1
        if isinstance(marker, dict) and marker.get("visitID") == visit_id:
            attempts = marker.get("attempts")
            max_attempts = marker.get("maxAttempts", MAX_SAME_VISIT_ATTEMPTS)
            if marker.get("conclusive") is True:
                # The conclusive language result already owns the label; a
                # late/in-flight collecting event must not hide it.
                return
            if type(attempts) is int and type(max_attempts) is int and attempts >= max_attempts:
                return
            attempt = int(attempts or 0) + 1
        key = (cid, visit_id, sample.started)
        now = time.time()
        last = self._state_published.get(key, 0.0)
        if last and now - last < STATE_PUBLISH_SECONDS:
            return
        self._state_published[key] = now
        if len(self._state_published) > 64:
            for old in list(self._state_published)[:len(self._state_published) - 64]:
                self._state_published.pop(old, None)
        session = self._session_from_sample(sample, marker if isinstance(marker, dict) else None)
        self._publish_state(
            "collecting", cid, sample.provider, sample.name,
            reason="collecting", seconds=best.seconds, target=base.TARGET_SECONDS,
            attempt=attempt, max_attempts=MAX_SAME_VISIT_ATTEMPTS,
            surf_paused=session.get("surfPaused"), surfing=session.get("surfing"),
            visit_id=session.get("visitID"), launch_id=session.get("launchID"),
            stream_generation=session.get("streamGeneration"), at=now,
        )

    def _state_inference(self, cid, sample, marker, seconds):
        if getattr(sample, "episode_only", False):
            return
        session = self._session_from_sample(sample, marker)
        self._publish_state(
            "inference", cid, sample.provider, sample.name,
            reason="inference", seconds=seconds, target=base.TARGET_SECONDS,
            attempt=marker.get("attempts"), max_attempts=marker.get("maxAttempts"),
            surf_paused=session.get("surfPaused"), surfing=session.get("surfing"),
            visit_id=session.get("visitID"), launch_id=session.get("launchID"),
            stream_generation=session.get("streamGeneration"),
        )

    def _state_inconclusive(self, cid, sample, marker, reason, seconds):
        if getattr(sample, "episode_only", False):
            return
        session = self._session_from_sample(sample, marker)
        attempts = marker.get("attempts") if isinstance(marker, dict) else None
        max_attempts = marker.get("maxAttempts", MAX_SAME_VISIT_ATTEMPTS) if isinstance(marker, dict) else MAX_SAME_VISIT_ATTEMPTS
        retry = SAME_VISIT_RETRY_SECONDS if (type(attempts) is int and attempts < max_attempts) else None
        self._publish_state(
            "inconclusive", cid, sample.provider, sample.name,
            reason=reason, seconds=seconds, target=base.MIN_SAMPLE_SECONDS,
            attempt=attempts, max_attempts=max_attempts, retry_seconds=retry,
            surf_paused=session.get("surfPaused"), surfing=session.get("surfing"),
            visit_id=session.get("visitID"), launch_id=session.get("launchID"),
            stream_generation=session.get("streamGeneration"),
        )

    def _state_disabled(self, cid, sample, marker):
        session = self._session_from_sample(sample, marker)
        self._publish_state(
            "disabled", cid, sample.provider, sample.name, reason="observation_disabled",
            attempt=marker.get("attempts") if isinstance(marker, dict) else None,
            max_attempts=marker.get("maxAttempts") if isinstance(marker, dict) else None,
            surf_paused=session.get("surfPaused"), surfing=session.get("surfing"),
            visit_id=session.get("visitID"), launch_id=session.get("launchID"),
            stream_generation=session.get("streamGeneration"),
        )

    def _record_result(self, job, decision):
        if job.get("episodeOnly"):
            # Safety net: an episode-only sample must never become language
            # evidence even if a caller reaches the normal completion path.
            self._finish_episode_retry(job, decision)
            return
        outcome = decision["outcome"]
        reason = decision["reason"]
        language = decision.get("language")
        language_name = decision.get("languageName")
        marked = False
        evidence_recorded = False
        suppressed = False
        deferred = False
        commit_reason = None
        count = None
        threshold = None
        if outcome in ("excluded", "allowed", "unauthorized") and self._enabled():
            if job.get("deviceVerified") is not True:
                outcome = "inconclusive"
                reason = "device_unverified"
            elif job.get("changed"):
                outcome = "inconclusive"
                reason = "stale_channel_changed"
            else:
                probe = self._probe()
                if not self._job_is_current(job, probe):
                    outcome = "inconclusive"
                    reason = "stale_session"
            if outcome in ("excluded", "allowed", "unauthorized"):
                evidence = {
                    "model": pathlib.Path(self.model_path).name,
                    "windows": decision.get("windows", 0),
                    "speechSeconds": decision.get("speechSeconds"),
                    "sampleSeconds": decision.get("sampleSeconds"),
                    "speechCoverage": decision.get("speechCoverage"),
                    "languages": decision.get("languages", []),
                    "deviceVerified": job.get("deviceVerified"),
                    "channelChangedBeforeResult": bool(job.get("changed")),
                    "lexicalMargin": decision.get("lexicalMargin"),
                    "outcome": outcome,
                    "visitID": job.get("visitID", ""),
                    "launchID": job.get("launchID", ""),
                    "streamGeneration": job.get("streamGeneration", ""),
                }
                session = {
                    "launchID": job.get("launchID", ""),
                    "streamGeneration": job.get("streamGeneration", ""),
                    "visitID": job.get("visitID", ""),
                    "deviceVerified": job.get("deviceVerified"),
                    "channelChangedBeforeResult": bool(job.get("changed")),
                    "surfPaused": job.get("surfPaused"),
                    "surfing": job.get("surfing"),
                }
                try:
                    result = self.on_exclusion(
                        job["cid"], job["provider"], job["name"] or job["provider"],
                        language, language_name, decision.get("confidence", 0.0),
                        "auto:" + reason, evidence, session=session,
                    ) or {}
                    commit_reason = result.get("reason")
                    count = result.get("count")
                    threshold = result.get("threshold")
                    if result.get("enforced"):
                        marked = True
                        evidence_recorded = bool(result.get("changed"))
                    elif result.get("changed"):
                        evidence_recorded = True
                    if commit_reason == "manual_not_spanish":
                        suppressed = True
                        outcome = "inconclusive"
                        reason = "manual_not_spanish"
                    elif commit_reason in ("deferred_paused", "deferred_uncertain"):
                        deferred = True
                        outcome = "deferred"
                        reason = commit_reason
                    elif commit_reason in ("duplicate_visit", "maintenance", "observation_disabled"):
                        outcome = "inconclusive"
                        reason = commit_reason
                    elif commit_reason in ("manual_exclusion", "already_marked"):
                        marked = marked or False
                    elif commit_reason in ("unknown_provider", "device_unverified", "stale_channel_changed"):
                        outcome = "inconclusive"
                        reason = commit_reason
                except Exception as error:
                    commit_reason = "commit_" + type(error).__name__
                    outcome = "inconclusive"
                    reason = "commit_error"
        conclusive = decision.get("outcome") in ("excluded", "allowed", "unauthorized")
        if (conclusive and not deferred
                and commit_reason in ("evidence_recorded", "auto_threshold", "already_marked",
                                      "duplicate_visit", "manual_exclusion", "manual_not_spanish")):
            self._remember_visit_result(job.get("visitID", ""))
        marker_conclusive = bool(
            outcome in ("excluded", "allowed", "unauthorized", "deferred")
            or reason in ("duplicate_visit", "manual_not_spanish", "manual_exclusion", "already_marked"))
        self._mark_finalized_result(job, outcome, marker_conclusive, reason)
        with self._lock:
            counters = self.telemetry["counters"]
            counters["tested"] += 1
            counters["samplesFinalized"] += 1
            if outcome == "excluded":
                counters["excluded"] += 1
                if language == "es":
                    counters["spanish"] += 1
            elif outcome == "allowed":
                counters["allowed"] += 1
                counters["nonSpanish"] += 1
            elif outcome == "unauthorized":
                counters["unauthorized"] += 1
                counters["nonSpanish"] += 1
            elif outcome == "deferred":
                counters["deferred"] = counters.get("deferred", 0) + 1
            else:
                counters["inconclusive"] += 1
            if language:
                self.telemetry["languages"][language] = self.telemetry["languages"].get(language, 0) + 1
            if evidence_recorded:
                counters["evidence"] = counters.get("evidence", 0) + 1
            if marked:
                counters["enforced"] = counters.get("enforced", 0) + 1
            if reason == "duplicate_visit":
                counters["duplicates"] = counters.get("duplicates", 0) + 1
            if reason in ("stale_session", "stale_channel_changed", "device_unverified"):
                counters["stale"] = counters.get("stale", 0) + 1
            if suppressed:
                counters["suppressed"] += 1
            if reason in ("commit_error", "inference_error"):
                counters["errors"] += 1
            last = {
                "at": round(time.time(), 3), "channelID": job["cid"], "providerID": job["provider"],
                "name": job["name"], "language": language, "languageName": language_name,
                "outcome": outcome, "reason": reason,
                "confidence": decision.get("confidence", 0.0),
                "speechSeconds": decision.get("speechSeconds", 0.0),
                "sampleSeconds": decision.get("sampleSeconds", 0.0),
                "speechCoverage": decision.get("speechCoverage", 0.0),
                "windows": decision.get("windows", 0),
                "deviceVerified": job.get("deviceVerified"),
                "launchID": job.get("launchID", ""),
                "streamGeneration": job.get("streamGeneration", ""),
                "visitID": job.get("visitID", ""),
                "surfPaused": job.get("surfPaused"),
                "surfing": job.get("surfing"),
                "attempt": job.get("attempt"),
                "maxAttempts": job.get("maxAttempts"),
                "conclusive": marker_conclusive,
                "channelChangedBeforeResult": bool(job.get("changed")),
                "marked": marked, "evidence": evidence_recorded, "count": count,
                "threshold": threshold, "suppressed": suppressed, "commitReason": commit_reason,
            }
            self.telemetry["last"] = last
            self.telemetry["current"] = None
        markers = getattr(self, "_finalized", None)
        marker = markers.get(job.get("cid")) if isinstance(markers, dict) else None
        attempts = marker.get("attempts") if isinstance(marker, dict) else job.get("attempt")
        max_attempts = marker.get("maxAttempts") if isinstance(marker, dict) else job.get("maxAttempts")
        retry_seconds = None
        if (outcome == "inconclusive" and type(attempts) is int
                and type(max_attempts) is int and attempts < max_attempts
                and reason not in ("duplicate_visit", "manual_not_spanish", "manual_exclusion",
                                   "already_marked", "stale_session", "stale_channel_changed",
                                   "device_unverified", "session_mismatch", "launch_changed")):
            retry_seconds = SAME_VISIT_RETRY_SECONDS
        if outcome == "deferred":
            self._publish_state(
                "deferred", job["cid"], job.get("provider"), job.get("name"),
                reason=reason, attempt=attempts, max_attempts=max_attempts,
                surf_paused=job.get("surfPaused"), surfing=job.get("surfing"),
                language=language, language_name=language_name, outcome=outcome,
                visit_id=job.get("visitID", ""), launch_id=job.get("launchID", ""),
                stream_generation=job.get("streamGeneration", ""), conclusive=True)
        elif outcome == "inconclusive" and reason == "observation_disabled":
            self._publish_state(
                "disabled", job["cid"], job.get("provider"), job.get("name"),
                reason=reason, attempt=attempts, max_attempts=max_attempts,
                surf_paused=job.get("surfPaused"), surfing=job.get("surfing"),
                visit_id=job.get("visitID", ""), launch_id=job.get("launchID", ""),
                stream_generation=job.get("streamGeneration", ""), conclusive=False)
        elif outcome == "inconclusive" and reason not in (
                "duplicate_visit", "manual_not_spanish", "manual_exclusion", "already_marked"):
            self._publish_state(
                "inconclusive", job["cid"], job.get("provider"), job.get("name"),
                reason=reason, seconds=decision.get("sampleSeconds"),
                target=base.MIN_SAMPLE_SECONDS, attempt=attempts, max_attempts=max_attempts,
                retry_seconds=retry_seconds,
                surf_paused=job.get("surfPaused"), surfing=job.get("surfing"),
                language=language, language_name=language_name, outcome=outcome,
                visit_id=job.get("visitID", ""), launch_id=job.get("launchID", ""),
                stream_generation=job.get("streamGeneration", ""), conclusive=False)
        if outcome == "deferred":
            self.log("LANGUAGE_AUTO_DEFER language=%s name=%s provider=%s channel=%s reason=%s confidence=%.3f speech=%.1fs" % (
                language, language_name, job["provider"], job["cid"], reason,
                decision.get("confidence", 0.0), decision.get("speechSeconds", 0.0)))
        elif marked:
            self.log("LANGUAGE_AUTO_EXCLUDE language=%s name=%s provider=%s channel=%s count=%s threshold=%s confidence=%.3f speech=%.1fs" % (
                language, language_name, job["provider"], job["cid"], str(count), str(threshold),
                decision.get("confidence", 0.0), decision.get("speechSeconds", 0.0)))
        else:
            self.log("LANGUAGE_AUTO_RESULT language=%s name=%s provider=%s channel=%s outcome=%s reason=%s evidence=%s count=%s threshold=%s confidence=%.3f speech=%.1fs" % (
                language, language_name, job["provider"], job["cid"], outcome, reason,
                str(evidence_recorded), str(count), str(threshold),
                decision.get("confidence", 0.0), decision.get("speechSeconds", 0.0)))
        self._persist(force=True)

    def _record_last(self, cid, provider, name, decision, device_verified, changed, marked, reason_detail,
                     session=None, conclusive=False):
        session = session if isinstance(session, dict) else {}
        reason = decision.get("reason", reason_detail)
        attempts = session.get("attempts")
        max_attempts = session.get("maxAttempts", MAX_SAME_VISIT_ATTEMPTS)
        marker_conclusive = bool(conclusive or decision.get("outcome") in (
            "excluded", "allowed", "unauthorized", "deferred"))
        self._mark_finalized_result(
            {"cid": cid, "sampleToken": session.get("sampleToken")},
            decision.get("outcome", "inconclusive"), marker_conclusive, reason)
        with self._lock:
            counters = self.telemetry["counters"]
            counters["tested"] += 1
            counters["samplesFinalized"] += 1
            counters["inconclusive"] += 1
            self.telemetry["last"] = {
                "at": round(time.time(), 3), "channelID": cid, "providerID": provider, "name": name,
                "language": decision.get("language"), "languageName": decision.get("languageName"),
                "outcome": decision.get("outcome", "inconclusive"),
                "reason": reason,
                "confidence": decision.get("confidence", 0.0),
                "speechSeconds": decision.get("speechSeconds", 0.0),
                "sampleSeconds": decision.get("sampleSeconds", 0.0),
                "speechCoverage": decision.get("speechCoverage", 0.0),
                "windows": decision.get("windows", 0),
                "deviceVerified": device_verified,
                "launchID": str(session.get("launchID") or ""),
                "streamGeneration": str(session.get("streamGeneration") or ""),
                "visitID": str(session.get("visitID") or ""),
                "surfPaused": session.get("surfPaused"),
                "surfing": session.get("surfing"),
                "attempt": attempts, "maxAttempts": max_attempts,
                "conclusive": marker_conclusive,
                "channelChangedBeforeResult": bool(changed),
                "marked": bool(marked), "suppressed": False, "commitReason": None,
            }
            self.telemetry["current"] = None
        retry_seconds = None
        if (type(attempts) is int and type(max_attempts) is int and attempts < max_attempts
                and reason == "sample_too_short"):
            retry_seconds = SAME_VISIT_RETRY_SECONDS
        state = "no_audio" if reason == "no_audio" else "inconclusive"
        self._publish_state(
            state, cid, provider, name, reason=reason,
            seconds=decision.get("sampleSeconds"), target=base.MIN_SAMPLE_SECONDS,
            attempt=attempts, max_attempts=max_attempts, retry_seconds=retry_seconds,
            surf_paused=session.get("surfPaused"), surfing=session.get("surfing"),
            language=decision.get("language"), language_name=decision.get("languageName"),
            outcome=decision.get("outcome", "inconclusive"),
            visit_id=session.get("visitID", ""), launch_id=session.get("launchID", ""),
            stream_generation=session.get("streamGeneration", ""), conclusive=marker_conclusive)
        self._persist(force=True)

    def status(self):
        now = time.time()
        with self._lock:
            counters = dict(self.telemetry["counters"])
            languages = dict(self.telemetry.get("languages", {}))
            last = self.telemetry.get("last")
            current = self.telemetry.get("current")
            last_media = self._last_media
        active = bool(not self.fault and self._enabled() and last_media and now - last_media <= base.INACTIVE_SECONDS)
        decision_total = counters.get("excluded", 0) + counters.get("allowed", 0) + counters.get("unauthorized", 0) + counters.get("inconclusive", 0) + counters.get("deferred", 0)
        return {
            "revision": REVISION,
            "enabled": bool(not self.fault and self._enabled()),
            "active": active,
            "fault": self.fault,
            "model": pathlib.Path(self.model_path).name,
            "allowedLanguages": sorted(language_catalog.ALLOWED),
            "excludedLanguages": sorted(language_catalog.EXCLUDED),
            "thresholds": {
                "sampleSeconds": base.TARGET_SECONDS, "minSampleSeconds": base.MIN_SAMPLE_SECONDS,
                "minSpeechSeconds": base.MIN_SPEECH_SECONDS, "minCoverage": base.MIN_COVERAGE,
                "minWindows": MIN_LANGUAGE_WINDOWS, "minWindowProbability": MIN_WINDOW_PROBABILITY,
                "minLanguageProbability": MIN_LANGUAGE_PROBABILITY,
            },
            "counters": counters,
            "languages": languages,
            "languageNames": {code: language_catalog.language_name(code) for code in languages},
            "decisionTotal": decision_total,
            "pending": current is not None or bool(self._jobs and self._jobs.qsize()),
            "current": current,
            "last": last,
            "diagnostic": self.telemetry.get("diagnostic"),
            "lastMediaAt": round(last_media, 3) if last_media else None,
        }

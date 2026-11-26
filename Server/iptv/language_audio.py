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
                 ffmpeg=None, whisper=None, threads=4, visits=None, on_speech=None):
        self.on_exclusion = on_exclusion
        self.on_speech = on_speech
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
                full = pathlib.Path(workdir) / "full.wav"
                base._write_wav(full, job["pcm"])
                transcript = base.transcribe(full, self.whisper, self.model_path, workdir, self.threads)
                lexical = base.lexical_margin(transcript[1]) if transcript else (None, 0, 0)
                decision = decide_language(analysis, window_results, lexical,
                                           transcript[0] if transcript else None)
            self._notify_speech(job, decision, transcript)
            self._record_result(job, decision)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

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
        }
        try:
            self.on_speech(view, dict(decision), transcript[1])
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

    def _record_result(self, job, decision):
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
                "channelChangedBeforeResult": bool(job.get("changed")),
                "marked": marked, "evidence": evidence_recorded, "count": count,
                "threshold": threshold, "suppressed": suppressed, "commitReason": commit_reason,
            }
            self.telemetry["last"] = last
            self.telemetry["current"] = None
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

    def _record_last(self, cid, provider, name, decision, device_verified, changed, marked, reason_detail):
        with self._lock:
            counters = self.telemetry["counters"]
            counters["tested"] += 1
            counters["samplesFinalized"] += 1
            counters["inconclusive"] += 1
            self.telemetry["last"] = {
                "at": round(time.time(), 3), "channelID": cid, "providerID": provider, "name": name,
                "language": decision.get("language"), "languageName": decision.get("languageName"),
                "outcome": decision.get("outcome", "inconclusive"),
                "reason": decision.get("reason", reason_detail),
                "confidence": decision.get("confidence", 0.0),
                "speechSeconds": decision.get("speechSeconds", 0.0),
                "sampleSeconds": decision.get("sampleSeconds", 0.0),
                "speechCoverage": decision.get("speechCoverage", 0.0),
                "windows": decision.get("windows", 0),
                "deviceVerified": device_verified,
                "channelChangedBeforeResult": bool(changed),
                "marked": bool(marked), "suppressed": False, "commitReason": None,
            }
            self.telemetry["current"] = None
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

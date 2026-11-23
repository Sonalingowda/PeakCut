"""Transcript evidence gates for candidate selection; no media or network access."""
import os
import sys
import tempfile
from unittest.mock import patch

os.environ.setdefault("PEAKCUT_DATA", tempfile.mkdtemp(prefix="peakcut-selection-test-"))
sys.path.insert(0, ".")

import numpy as np

from studio import brain


def transcript(start=0):
    sentences = [
        "I hear guards are coming.",
        "They're coming through the tunnel.",
        "I need to hide now.",
        "They found me at the door.",
    ]
    words = []
    for line, sentence in enumerate(sentences):
        t = start + line * 4.2
        for token in sentence.split():
            words.append({"s": t, "e": t + 0.22, "w": token})
            t += 0.24
    return words


def candidate(start=0, **changes):
    out = {"start": start, "end": start + 15, "score": 85, "hook": "GUARDS FOUND ME",
           "title": "The guards found me", "reason": "The guards found me at the door",
           "description": "", "hashtags": [], "highlight_words": []}
    out.update(changes)
    return out


def valid(c=None, start=0, words=None):
    words = transcript(start) if words is None else words
    lines = brain.make_lines(words)
    c = candidate(start) if c is None else c
    return brain.validate_candidate(c, start, start + 15, lines, words, 15, 30)


def test_valid_standalone_clip_is_supported_by_selected_speech():
    assert valid()[0]


def test_hook_with_only_one_matching_word_is_rejected():
    ok, reason = valid(candidate(hook="GUARDS DESTROY EVERYTHING"))
    assert not ok and "hook" in reason


def test_generic_phrases_inside_hook_or_title_and_misleading_title_are_rejected():
    assert not valid(candidate(hook="THIS IS BRO WAIT FOR IT"))[0]
    assert not valid(candidate(title="Wait for it, the guards found me"))[0]
    assert not valid(candidate(title="A dragon wins the tournament"))[0]


def test_missing_setup_or_payoff_inside_selected_footage_is_rejected():
    words = transcript()
    # Move the setup before the clip while leaving enough later speech.
    no_setup = [dict(w, s=w["s"] - 6, e=w["e"] - 6) if w["s"] < 1 else w for w in words]
    lines = brain.make_lines(no_setup)
    ok, reason = brain.validate_candidate(candidate(), 0, 15, lines, no_setup, 15, 30)
    assert not ok
    assert "context" in reason or "sequence" in reason
    # The payoff is spoken after the clip, not merely after a punctuation mark.
    no_payoff = [dict(w, s=w["s"] + 6, e=w["e"] + 6) if w["s"] >= 12 else w for w in words]
    lines = brain.make_lines(no_payoff)
    ok, reason = brain.validate_candidate(candidate(), 0, 15, lines, no_payoff, 15, 30)
    assert not ok
    assert "context" in reason or "sequence" in reason


def test_loudness_peak_without_meaningful_transcript_does_not_pass():
    words = [{"s": i * 0.25, "e": i * 0.25 + 0.1, "w": "ah"} for i in range(64)]
    lines = brain.make_lines(words, np.r_[np.zeros(8), np.ones(8) * 12, np.zeros(8)])
    assert not brain.validate_candidate(candidate(), 0, 15, lines, words, 15, 30)[0]
    assert "(LOUD)" not in brain._fmt(lines)


def test_overlapping_windows_give_each_candidate_one_owner_and_keep_padding():
    words = transcript(810) + transcript(840)
    words += transcript(857)
    prompts = []

    def answer(prompt, **_kwargs):
        prompts.append(prompt)
        return [candidate(840)], "test-model", 1

    with patch.object(brain, "ask_ai", side_effect=answer):
        clips, _ = brain.find_clips(words, np.zeros(1000), 1000, min_len=15, max_len=30)
    assert len(prompts) >= 2
    assert "[810.0]" in prompts[1]  # 30-second transcript context before owner window
    assert len(clips) == 1


def test_ai_unavailable_never_selects_loudness_only_clips():
    words = transcript()
    with patch.object(brain, "ask_ai", side_effect=RuntimeError("offline")):
        clips, info = brain.find_clips(words, np.r_[np.zeros(100), np.ones(10) * 20, np.zeros(100)],
                                       100, min_len=15, max_len=30, log=lambda _message: None)
    assert clips == []
    assert info["model"] is None
    assert "unavailable" in info["note"]


def test_short_video_gets_transcript_grounded_fallback_moments_when_ai_finds_none():
    with patch.object(brain, "ask_ai", return_value=([], "test-model", 1)):
        clips, info = brain.find_clips(transcript(), np.zeros(30), 20, max_clips=1, min_len=5, max_len=15)
    assert len(clips) == 1
    assert info["candidate_diagnostics"][0]["fallback"] == "random_transcript_moment"
    assert info["candidate_diagnostics"][0]["accepted"]


def live_judged(candidate_changes=None, words=None):
    words = transcript() if words is None else words
    c = candidate(**(candidate_changes or {}))
    with patch.object(brain, "ask_ai", return_value=([c], "test-model", 1)):
        clips, _ = brain.judge_transcript(words, 0, 15, min_len=15, max_len=30)
    return clips


def test_live_judge_rejects_generic_hook():
    assert live_judged({"hook": "WAIT FOR IT"}) == []


def test_live_judge_rejects_unsupported_claim():
    assert live_judged({"title": "A dragon wins the tournament"}) == []


def test_live_judge_rejects_insufficient_transcript_evidence():
    words = [{"s": i * 0.25, "e": i * 0.25 + 0.1, "w": "ah"} for i in range(64)]
    assert live_judged(words=words) == []


def test_live_judge_keeps_valid_candidate():
    clips = live_judged()
    assert len(clips) == 1 and clips[0]["start"] == 0


def test_language_detection_distinguishes_english_and_arabic_scripts():
    assert brain.script_language("guards found me at the door") == "latin"
    assert brain.script_language("وجدوني عند الباب") == "ar"
    assert brain.detect_language("the guards are coming through the tunnel") == "en"
    assert brain.detect_language("los guardias están entrando por el túnel") == "es"


def test_arabic_youtube_captions_are_rejected_for_english_audio():
    from studio import jobs
    captions = ([{"w": "وجدوني", "s": 0, "e": 1}] * 30, "ar")
    english = [{"w": w, "s": i, "e": i + .2} for i, w in enumerate(
        "the guards are coming through the tunnel and they found me at the door".split())]
    with patch.object(jobs.transcribe, "transcribe", return_value=english):
        verified, independent, _caption, audio, _note = jobs._verify_youtube_transcript(captions, np.zeros(10), 0, 10)
    assert not verified and independent == english and audio == "en"


def test_english_youtube_captions_are_rejected_for_spanish_audio():
    from studio import jobs
    captions = ([{"w": w, "s": i, "e": i + .2} for i, w in enumerate(
        "the guards are coming through the tunnel".split())], "en")
    spanish = [{"w": w, "s": i, "e": i + .2} for i, w in enumerate(
        "los guardias están entrando por el túnel y vienen hacia la puerta".split())]
    with patch.object(jobs.transcribe, "transcribe", return_value=spanish):
        verified, independent, _caption, audio, _note = jobs._verify_youtube_transcript(captions, np.zeros(10), 0, 10)
    assert not verified and independent == spanish and audio == "es"


def test_matching_english_transcript_keeps_lexical_grounding():
    assert valid(candidate(hook="GUARDS FOUND ME"), words=transcript())[0]


def test_arabic_transcript_translated_hook_requires_grounded_semantic_check():
    en = transcript()
    arabic = [{**w, "w": {"I":"أنا", "hear":"أسمع", "guards":"الحراس", "are":"قادمون", "coming.":"قادمون.",
                           "They’re":"هم", "coming":"قادمون", "through":"عبر", "the":"ال", "tunnel.":"النفق.",
                           "need":"أحتاج", "to":"أن", "hide":"أختبئ", "now.":"الآن.", "They":"هم", "found":"وجدوا",
                           "me":"ني", "at":"عند", "door.":"الباب."}.get(w["w"], w["w"])} for w in en]
    with patch.object(brain, "ask_ai", return_value=({"supported": True, "evidence": "The guards find him at the door."}, "test", 1)) as check:
        ok, reason = brain.validate_candidate(candidate(), 0, 15, brain.make_lines(arabic), arabic, 15, 30,
                                               transcript_language="ar", allow_cross_language=True)
    assert ok and "transcript" in reason
    check.assert_called_once()


def test_unsupported_cross_language_hook_fails_closed():
    words = [{**w, "w": "وجدوني"} for w in transcript()]
    with patch.object(brain, "ask_ai", return_value=({"supported": False, "evidence": "No support."}, "test", 1)):
        ok, reason = brain.validate_candidate(candidate(hook="DRAGON WINS", title="A dragon wins"), 0, 15,
                                               brain.make_lines(words), words, 15, 30,
                                               transcript_language="ar", allow_cross_language=True)
    assert not ok and "cross-language semantic grounding rejected" in reason


def test_cross_language_diagnostics_save_evidence_and_decision():
    words = [{**w, "w": "الحراس"} for w in transcript()]
    c = candidate()
    def answer(prompt, **_kwargs):
        if "cross-language" in _kwargs.get("purpose", "").lower():
            return {"supported": True, "evidence": "The guards are found at the door."}, "test", 1
        return [c], "test", 1
    with patch.object(brain, "ask_ai", side_effect=answer):
        clips, info = brain.find_clips(words, np.zeros(100), 20, min_len=15, max_len=30,
                                       language="ar")
    assert clips and info["candidate_diagnostics"][0]["grounding"] == {
        "evidence": "The guards are found at the door.", "decision": "accepted"
    }


def test_candidate_diagnostics_keep_raw_wording_excerpt_and_rejection():
    words = transcript()
    bad = candidate(hook="GUARDS DESTROY EVERYTHING")
    with patch.object(brain, "ask_ai", return_value=([bad], "test-model", 1)):
        clips, info = brain.find_clips(words, np.zeros(100), 20, min_len=15, max_len=30, language="latin")
    diag = info["candidate_diagnostics"][0]
    assert clips == [] and diag["raw"]["hook"] == bad["hook"]
    assert diag["language"] == "latin" and "guards" in diag["transcript_excerpt"].lower()
    assert not diag["accepted"] and diag["rejection_reason"]


if __name__ == "__main__":
    checks = [value for name, value in sorted(globals().items())
              if name.startswith("test_") and callable(value)]
    for check in checks:
        check()
        print("PASS " + check.__name__)
    print(f"{len(checks)} checks passed")

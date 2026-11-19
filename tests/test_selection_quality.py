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


if __name__ == "__main__":
    checks = [value for name, value in sorted(globals().items())
              if name.startswith("test_") and callable(value)]
    for check in checks:
        check()
        print("PASS " + check.__name__)
    print(f"{len(checks)} checks passed")

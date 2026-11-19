"""Small deterministic checks for the autonomous safety gates."""
import os
import sys
import tempfile

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-autonomy-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")

from studio import brain, quality  # noqa: E402
import numpy as np  # noqa: E402


def main():
    clips = [{"id": "a", "start": 10, "end": 30}, {"id": "b", "start": 12, "end": 29},
             {"id": "c", "start": 40, "end": 55}]
    assert quality.duplicate_candidates(clips) == [("a", "b")]
    assert not quality.validate_clip(os.path.join(os.environ["PEAKCUT_DATA"], "missing.mp4"))["ok"]

    old = brain.ask_ai
    try:
        brain.ask_ai = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
        words = [{"w": "hello", "s": i * 0.5, "e": i * 0.5 + 0.3} for i in range(30)]
        clips, info = brain.find_clips(words, np.zeros(20), 20, max_clips=2, min_len=5, max_len=10)
        assert clips == [] and "no clips" in info["note"].lower()
    finally:
        brain.ask_ai = old
    print("ALL PASSED")


if __name__ == "__main__":
    main()

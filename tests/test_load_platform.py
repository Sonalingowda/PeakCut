"""Platform-specific CPU sampling tests; no real process load is created."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio import load


def main():
    mac_output = "CPU usage: 12.50% user, 7.50% sys, 80.00% idle\n"
    with patch.object(load.sys, "platform", "darwin"), patch.object(load.subprocess, "check_output", return_value=mac_output) as command:
        busy, total = load._cpu_times()
        assert busy == 20.0 and total == 100.0
        command.assert_called_once_with(("/usr/bin/top", "-l", "1", "-n", "0", "-F"),
                                        text=True, stderr=load.subprocess.STDOUT, timeout=5)

    with patch.object(load.sys, "platform", "darwin"), patch.object(load.subprocess, "check_output", return_value="malformed"):
        try:
            load._cpu_times()
        except RuntimeError:
            pass
        else:
            raise AssertionError("malformed macOS CPU output was accepted")

    with patch.object(load.sys, "platform", "freebsd"):
        try:
            load._cpu_times()
        except RuntimeError as e:
            assert "unsupported" in str(e)
        else:
            raise AssertionError("unsupported CPU platform was treated as Linux")

    # A failed sample is retried rather than permanently disabling CPU
    # monitoring. The loop also continues updating other machine metrics.
    samples = iter([RuntimeError("temporary failure"), (90.0, 100.0)])
    ticks = iter([None, None])

    def sample():
        value = next(samples)
        if isinstance(value, Exception):
            raise value
        return value

    def sleep(_):
        try:
            next(ticks)
        except StopIteration:
            raise KeyboardInterrupt

    with patch.object(load.sys, "platform", "darwin"), patch.object(load, "_cpu_times", side_effect=sample), \
         patch.object(load.time, "sleep", side_effect=sleep), patch.object(load, "_ram_free_gb", return_value=10.0), \
         patch.object(load.shutil, "disk_usage", return_value=type("Usage", (), {"free": 10_000_000_000})()):
        load._state.update(cpu=0.0, ram_free=99.0, disk_free=99.0, at=0.0)
        try:
            load._watch()
        except KeyboardInterrupt:
            pass
    assert load._state["cpu"] > 0
    print("ALL PASSED")


if __name__ == "__main__":
    main()

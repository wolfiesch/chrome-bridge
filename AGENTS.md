# Agent notes

- On this Mac, Homebrew Python 3.14 may fail to import `pyexpat` with a missing `XML_SetAllocTrackerActivationThreshold` symbol. This breaks `plistlib`, `xml.etree`, `verify_benchmark_harness.py`, and `setup-broker.sh` even though the bridge code is healthy. Confirm with `python3 -c 'import pyexpat'`. Until Homebrew Python is repaired, run XML-only checks with `/usr/bin/python3` and invoke broker setup with `/usr/bin` before `/opt/homebrew/bin` in `PATH` so system Python writes the plist while Homebrew Node remains available.

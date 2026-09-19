"""Make ``src`` importable for the test suite (example repo, no packaging)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

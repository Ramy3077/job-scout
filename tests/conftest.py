import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Set before anything imports jobscout.config, which resolves paths once at import.
os.environ["JOBSCOUT_DEMO"] = "1"
os.environ["JOBSCOUT_DATA"] = tempfile.mkdtemp(prefix="jobscout-test-")
os.environ["JOBSCOUT_DEMO_CACHE"] = tempfile.mkdtemp(prefix="jobscout-test-cache-")

sys.path.insert(0, str(ROOT / "jobscout"))
sys.path.insert(0, str(ROOT))

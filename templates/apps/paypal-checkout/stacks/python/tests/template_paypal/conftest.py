import sys
from pathlib import Path

# The app's root (where main.py and payments/ live) and this directory (the fake) are importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

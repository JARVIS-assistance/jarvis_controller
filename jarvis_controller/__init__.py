from pathlib import Path

# Expose the existing src-based modules as the jarvis_controller package.
__path__ = [str(Path(__file__).resolve().parent.parent / "src")]

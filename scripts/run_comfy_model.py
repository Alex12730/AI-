"""Stable script entry point for LocalCommandAdapter (no shell evaluation)."""
from video_workstation.adapters.comfyui import main

if __name__ == '__main__':
    raise SystemExit(main())

"""Configure this workstation's three installed models; never bypass benchmarks."""
import argparse
import json
from pathlib import Path
import shutil
import sys

from video_workstation.adapters.comfyui import ComfyClient
from video_workstation.config import Settings
from video_workstation.db import Database
from video_workstation.services.comfy_deployment import check_workflows, configure_profiles
from video_workstation.services.models import model_readiness


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comfy-url', default='http://127.0.0.1:8188')
    parser.add_argument('--ffmpeg', default='ffmpeg')
    args = parser.parse_args()
    if not shutil.which(args.ffmpeg):
        parser.error('FFmpeg executable not found')
    check_workflows(ComfyClient(args.comfy_url))
    database = Database(Settings())
    database.create_schema()
    with database.session() as session:
        profiles = configure_profiles(session, python=sys.executable, ffmpeg=args.ffmpeg, comfy_url=args.comfy_url)
        directory = database.settings.data_dir / 'model-commands'
        directory.mkdir(parents=True, exist_ok=True)
        for profile in profiles:
            (directory / (profile.slug + '.json')).write_text(json.dumps(profile.runtime_config_json['command'], indent=2), encoding='utf-8')
            print(f'{profile.slug}: {model_readiness(profile)} (production benchmark required)')


if __name__ == '__main__':
    main()

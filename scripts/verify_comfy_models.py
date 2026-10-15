"""Real adapter smoke tests, deliberately separate from production admission."""
import argparse
from dataclasses import asdict
import json
import time

from sqlalchemy import select

from video_workstation.adapters.base import GenerationRequest
from video_workstation.adapters.local_command import LocalCommandAdapter
from video_workstation.adapters.comfyui import MODELS, ComfyClient
from video_workstation.config import Settings
from video_workstation.db import Database
from video_workstation.models import ModelProfile
from video_workstation.qc import probe_media


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--model', choices=MODELS)
    args = parser.parse_args()
    database = Database(Settings())
    database.create_schema()
    results = {}
    directory = database.settings.data_dir / 'deployment-verification'
    directory.mkdir(parents=True, exist_ok=True)
    with database.session() as session:
        for slug in ((args.model,) if args.model else MODELS):
            profile = session.scalar(select(ModelProfile).where(ModelProfile.slug == slug))
            if profile is None or not profile.runtime_config_json.get('command'):
                raise ValueError('Configure local models first')
            command = profile.runtime_config_json['command'] + ['--preview']
            comfy_url = command[command.index('--comfy-url') + 1]
            queue = ComfyClient(comfy_url).request('/queue')
            if queue.get('queue_running') or queue.get('queue_pending'):
                raise RuntimeError('ComfyUI is busy; smoke tests must run on an idle engine')
            duration = 124 / 24 if slug == MODELS[2] else 17 / 24
            output = directory / (slug + '.mp4')
            request = GenerationRequest(task_id='smoke-' + slug,
                prompt='A red ball rolls slowly across a wooden table. Quiet room ambience.',
                output_path=output, duration_seconds=duration, aspect_ratio='16:9', seed=42)
            started = time.monotonic()
            result = LocalCommandAdapter(command, slug).execute(request)
            qc = probe_media(output, expected_duration=duration, ffprobe_bin=args.ffprobe) if result.success else None
            results[slug] = {'passed': bool(result.success and qc and qc.passed),
                'elapsed_seconds': round(time.monotonic() - started, 2), 'output': str(output),
                'qc': asdict(qc) if qc else None, 'error': result.error_message,
                'scope': 'small adapter integration smoke test; not production admission'}
            (directory / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'{slug}: ' + ('passed' if results[slug]['passed'] else 'failed'), flush=True)
            if not results[slug]['passed']:
                raise SystemExit(1)


if __name__ == '__main__':
    main()

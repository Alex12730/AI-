"""Fixed local ComfyUI workflows used by the existing command adapter."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

WORKFLOWS = Path(__file__).resolve().parents[3] / 'config' / 'workflows'
MODELS = ('wan-2.2', 'ltx-2.3', 'minimax-h3-fl2va')


def build_graph(model, *, prompt, negative_prompt, duration, aspect_ratio, seed,
                preview, output_prefix, workflow_sha256=None):
    if model not in MODELS or aspect_ratio not in ('16:9', '9:16'):
        raise ValueError('Unsupported model or aspect ratio')
    if not math.isfinite(duration) or not 0 < duration <= (15 if model == MODELS[2] else 20):
        raise ValueError('Invalid duration')
    if not 0 <= seed < 2**64:
        raise ValueError('Invalid seed')
    raw = (WORKFLOWS / (model + '.json')).read_bytes()
    if workflow_sha256 and hashlib.sha256(raw).hexdigest() != workflow_sha256:
        raise ValueError('Workflow changed; configure the model again and repeat benchmarks')
    graph = json.loads(raw.decode('utf-8-sig'))
    grid, offset = (4, 1) if model == MODELS[0] else (8, 1) if model == MODELS[1] else (17, 5)
    frames = max(grid + offset, math.floor((duration * 24 - offset) / grid + .5) * grid + offset)
    if model == MODELS[2]:
        if negative_prompt:
            raise ValueError('MiniMax H3 workflow does not support a negative prompt')
        graph['115']['inputs'].update(aspect_ratio='16:9 (Widescreen)' if aspect_ratio == '16:9' else '9:16 (Portrait Widescreen)', megapixels=.1 if preview else 1.032)
        graph['140:131']['inputs'].update(prompt=prompt, length=frames)
        graph['140:129']['inputs']['noise_seed'] = seed
        graph['140:137']['inputs']['value'] = 2 if preview else 50
        # Base mode: turbo LoRA is intentionally disabled.
        graph['140:139']['inputs']['value'] = False
    else:
        width, height = ((320, 192) if preview else (1280, 704)) if model == MODELS[0] else ((256, 256) if preview else (1024, 576))
        if aspect_ratio == '9:16':
            width, height = height, width
        graph['7']['inputs'].update(width=width, height=height, length=frames)
        if model == MODELS[0]:
            graph['5']['inputs']['text'] = prompt
            graph['6']['inputs']['text'] = negative_prompt
            graph['8']['inputs'].update(seed=seed, steps=2 if preview else 20, cfg=1 if preview else 5)
        else:
            graph['4']['inputs']['text'] = prompt
            graph['5']['inputs']['text'] = negative_prompt
            graph['8']['inputs']['frames_number'] = frames
            graph['11']['inputs']['noise_seed'] = seed
    for node in graph.values():
        if node['class_type'] == 'SaveVideo':
            node['inputs'].update(filename_prefix=output_prefix, format='mp4', codec='h264', **{'format.codec': 'h264'})
    return graph


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('ComfyUI redirects are prohibited')


class ComfyClient:
    def __init__(self, url='http://127.0.0.1:8188'):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('ComfyUI endpoint must be a loopback origin')
        self.url = url.rstrip('/')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, data=None, *, timeout=30, binary=False):
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(self.url + path, data=body, headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read()
                return raw if binary else (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            # Error bodies can echo prompts; do not log them.
            raise RuntimeError(f'ComfyUI HTTP error {exc.code}') from exc

    def generate(self, graph, output: Path, *, timeout=6900, poll_interval=2):
        deadline = time.monotonic() + timeout
        submitted = self.request('/prompt', {'prompt': graph, 'client_id': str(uuid.uuid4())})
        if submitted.get('node_errors') or not submitted.get('prompt_id'):
            raise RuntimeError('ComfyUI rejected the workflow')
        prompt_id = submitted['prompt_id']
        temporary = output.with_name(output.name + '.' + uuid.uuid4().hex + '.part')
        try:
            while time.monotonic() < deadline:
                remaining = max(.01, deadline - time.monotonic())
                history = self.request('/history/' + urllib.parse.quote(prompt_id, safe=''), timeout=min(30, remaining))
                result = history.get(prompt_id)
                if result:
                    status = result.get('status', {})
                    if status.get('status_str') != 'success':
                        messages = json.dumps(status.get('messages', [])).lower()
                        raise RuntimeError('CUDA out of memory' if 'out of memory' in messages else 'ComfyUI generation failed')
                    save_nodes = {key for key, value in graph.items() if value['class_type'] == 'SaveVideo'}
                    files = [item for key, value in result.get('outputs', {}).items() if key in save_nodes
                             for entries in value.values() if isinstance(entries, list)
                             for item in entries if isinstance(item, dict) and str(item.get('filename', '')).lower().endswith('.mp4')
                             and item.get('type') == 'output']
                    if len(files) != 1:
                        raise RuntimeError('Expected exactly one saved MP4 output')
                    data = self.request('/view?' + urllib.parse.urlencode(files[0]), timeout=min(120, remaining), binary=True)
                    if not data:
                        raise RuntimeError('ComfyUI returned an empty video')
                    output.parent.mkdir(parents=True, exist_ok=True)
                    temporary.write_bytes(data)
                    os.replace(temporary, output)
                    return prompt_id
                time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
            raise RuntimeError('ComfyUI generation timed out')
        except BaseException:
            # Delete only our queued job and interrupt only our active job.
            for path, body in (('/queue', {'delete': [prompt_id]}), ('/interrupt', {'prompt_id': prompt_id})):
                try:
                    self.request(path, body, timeout=5)
                except Exception:
                    pass
            raise
        finally:
            temporary.unlink(missing_ok=True)


def add_silent_audio(source: Path, output: Path, ffmpeg: str):
    completed = subprocess.run([ffmpeg, '-nostdin', '-y', '-i', str(source), '-f', 'lavfi',
        '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000', '-map', '0:v:0', '-map', '1:a:0',
        '-c:v', 'copy', '-c:a', 'aac', '-shortest', str(output)],
        shell=False, capture_output=True, timeout=120)
    if completed.returncode or not output.is_file() or not output.stat().st_size:
        raise RuntimeError('Unable to add the Wan silent audio track')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', choices=MODELS, required=True)
    parser.add_argument('--comfy-url', default='http://127.0.0.1:8188')
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--negative-prompt', default='')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration', type=float, required=True)
    parser.add_argument('--aspect-ratio', choices=('16:9', '9:16'), required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--preview', action='store_true', help='Small integration test; never a production benchmark')
    parser.add_argument('--workflow-sha256')
    parser.add_argument('--bridge-sha256')
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--timeout', type=float, default=6900)
    args = parser.parse_args(argv)
    staging = args.output.with_name(args.output.stem + '.' + uuid.uuid4().hex + '.mp4')
    raw = staging.with_name(staging.stem + '.raw.mp4')
    try:
        if args.timeout <= 0 or args.timeout > 6900 or not math.isfinite(args.timeout):
            raise ValueError('Timeout must be between 0 and 6900 seconds')
        client = ComfyClient(args.comfy_url)
        if args.bridge_sha256 and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != args.bridge_sha256:
            raise ValueError('Bridge changed; configure the models again and repeat benchmarks')
        graph = build_graph(args.model, prompt=args.prompt, negative_prompt=args.negative_prompt,
                            duration=args.duration, aspect_ratio=args.aspect_ratio, seed=args.seed,
                            preview=args.preview, output_prefix='workstation/' + uuid.uuid4().hex,
                            workflow_sha256=args.workflow_sha256)
        if args.model == MODELS[0] and not shutil.which(args.ffmpeg):
            raise ValueError('FFmpeg is required for the Wan silent audio track')
        client.generate(graph, raw, timeout=args.timeout)
        if args.model == MODELS[0]:
            add_silent_audio(raw, staging, args.ffmpeg)
        else:
            os.replace(raw, staging)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, args.output)
        print(json.dumps({'model': args.model, 'preview': args.preview, 'audio': 'silence' if args.model == MODELS[0] else 'native'}))
        return 0
    except Exception as exc:
        # Only controlled messages are emitted; transport exceptions can contain user data.
        print(str(exc) if isinstance(exc, (ValueError, RuntimeError)) else 'Local ComfyUI execution failed', file=sys.stderr)
        return 1
    finally:
        raw.unlink(missing_ok=True)
        staging.unlink(missing_ok=True)


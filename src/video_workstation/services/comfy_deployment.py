"""Register reproducible ComfyUI commands without admitting untested presets."""
import hashlib
from pathlib import Path

from ..adapters.comfyui import ComfyClient, MODELS, WORKFLOWS
from .models import configure_local_profile, seed_model_profiles

VERSIONS = {
    'wan-2.2': ('Wan2.2-TI2V-5B / comfy-bridge-v1', 'fp16'),
    'ltx-2.3': ('LTX-2.3-22B-distilled / comfy-bridge-v1', 'fp8 + Gemma fp4'),
    'minimax-h3-fl2va': ('H3-Base-FL2VA / comfy-bridge-v1', 'pruned-int8-convrot + Qwen nvfp4-awq'),
}


def configure_profiles(session, *, python, ffmpeg, comfy_url='http://127.0.0.1:8188'):
    ComfyClient(comfy_url)  # Validate before touching any profile.
    profiles = {p.slug: p for p in seed_model_profiles(session)}
    launcher = WORKFLOWS.parents[1] / 'scripts' / 'run_comfy_model.py'
    bridge_hash = hashlib.sha256((WORKFLOWS.parents[1] / 'src/video_workstation/adapters/comfyui.py').read_bytes()).hexdigest()
    registered = []
    for slug in MODELS:
        graph_hash = hashlib.sha256((WORKFLOWS / (slug + '.json')).read_bytes()).hexdigest()
        command = [str(Path(python).resolve()), str(launcher), '--model', slug,
                   '--comfy-url', comfy_url, '--workflow-sha256', graph_hash, '--bridge-sha256', bridge_hash,
                   '--prompt={prompt}', '--negative-prompt={negative_prompt}',
                   '--output', '{output}', '--duration', '{duration}',
                   '--aspect-ratio', '{aspect_ratio}', '--seed', '{seed}']
        if slug == 'wan-2.2':
            command += ['--ffmpeg', str(ffmpeg)]
        version, quantization = VERSIONS[slug]
        version += ' / ' + bridge_hash
        profile = profiles[slug]
        if (profile.runtime_config_json != {'command': command} or profile.model_version != version
                or profile.quantization != quantization or profile.provider != 'local'):
            configure_local_profile(profile, command=command, version=version, quantization=quantization)
        profile.enabled = True
        capabilities = dict(profile.capabilities_json or {})
        capabilities.update(deployment='local ComfyUI command bridge', modes=['text_to_video'],
                            audio='silent track for postproduction' if slug == 'wan-2.2' else 'native',
                            first_frame=False, last_frame=False)
        profile.capabilities_json = capabilities
        registered.append(profile)
    return registered


def check_workflows(client: ComfyClient):
    import json
    info = client.request('/object_info')
    for slug in MODELS:
        graph = json.loads((WORKFLOWS / (slug + '.json')).read_text(encoding='utf-8-sig'))
        for node in graph.values():
            kind = node['class_type']
            if kind not in info:
                raise ValueError(f'Missing ComfyUI node: {kind}')
            required = info[kind].get('input', {}).get('required', {})
            for name in ('unet_name', 'clip_name', 'vae_name', 'ckpt_name', 'text_encoder', 'lora_name'):
                value = node['inputs'].get(name)
                choices = required.get(name, [None])[0]
                if value and isinstance(choices, list) and value not in choices:
                    raise ValueError(f'Missing ComfyUI model file: {value}')


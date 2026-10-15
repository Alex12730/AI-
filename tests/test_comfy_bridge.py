import importlib
import json
import threading
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def bridge():
    try:
        return importlib.import_module('video_workstation.adapters.comfyui')
    except ModuleNotFoundError:
        pytest.fail('The local ComfyUI bridge has not been implemented')


@pytest.mark.parametrize('model,node,frames', [
    ('wan-2.2', '7', 121), ('ltx-2.3', '7', 121),
    ('minimax-h3-fl2va', '140:131', 124),
])
def test_duration_prompt_and_portrait_reach_the_generation_graph(model, node, frames):
    graph = bridge().build_graph(model, prompt='literal {text}; $HOME', negative_prompt='' if model == 'minimax-h3-fl2va' else 'blur',
                                 duration=5, aspect_ratio='9:16', seed=17, preview=False,
                                 output_prefix='workstation/test')
    assert graph[node]['inputs']['length'] == frames
    if model == 'minimax-h3-fl2va':
        assert graph[node]['inputs']['prompt'] == 'literal {text}; $HOME'
        assert graph['115']['inputs']['aspect_ratio'] == '9:16 (Portrait Widescreen)'
        assert graph['140:129']['inputs']['noise_seed'] == 17
    else:
        positive = '5' if model == 'wan-2.2' else '4'
        assert graph[positive]['inputs']['text'] == 'literal {text}; $HOME'
        assert graph[node]['inputs']['height'] > graph[node]['inputs']['width']
    assert any(n['inputs'].get('filename_prefix') == 'workstation/test' for n in graph.values())


@pytest.mark.parametrize('url', ['https://example.com', 'http://127.0.0.1@evil.test',
                                'http://localhost:8188/path', 'file:///tmp/server'])
def test_external_or_ambiguous_comfy_endpoints_are_rejected(url):
    with pytest.raises(ValueError):
        bridge().ComfyClient(url)


@pytest.mark.parametrize('mode', ['success', 'oom', 'missing', 'redirect', 'timeout'])
def test_history_controls_publication_and_timeout_only_cancels_own_job(tmp_path, mode):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, data))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'prompt_id': 'own-job', 'node_errors': {}} if self.path == '/prompt' else {}).encode())

        def do_GET(self):
            if self.path.startswith('/view'):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'actual-video-bytes')
                return
            if mode == 'redirect':
                self.send_response(302)
                self.send_header('Location', 'https://example.com')
                self.end_headers()
                return
            history = {} if mode == 'timeout' else {'own-job': {
                'status': {'status_str': 'error' if mode == 'oom' else 'success',
                           'completed': True, 'messages': [['execution_error', {'exception_message': 'CUDA out of memory'}]] if mode == 'oom' else []},
                'outputs': {} if mode == 'missing' else {'11': {'images': [
                    {'filename': 'generated.mp4', 'subfolder': 'workstation', 'type': 'output'}]}}}}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(history).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = tmp_path / 'result.mp4'
    try:
        client = bridge().ComfyClient(f'http://127.0.0.1:{server.server_port}')
        if mode == 'success':
            client.generate({'11': {'class_type': 'SaveVideo', 'inputs': {}}}, output, timeout=1, poll_interval=.01)
            assert output.read_bytes() == b'actual-video-bytes'
        else:
            with pytest.raises(RuntimeError, match='out of memory' if mode == 'oom' else None):
                client.generate({'11': {'class_type': 'SaveVideo', 'inputs': {}}}, output, timeout=.05, poll_interval=.01)
            assert not output.exists()
        if mode == 'timeout':
            assert ('/interrupt', {'prompt_id': 'own-job'}) in calls
            assert ('/queue', {'delete': ['own-job']}) in calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_invalid_durations_and_unsupported_h3_negative_prompt_fail_before_submission():
    for duration in [0, float('nan'), float('inf'), 16]:
        with pytest.raises(ValueError):
            bridge().build_graph('minimax-h3-fl2va', prompt='ball', negative_prompt='',
                                 duration=duration, aspect_ratio='16:9', seed=1,
                                 preview=False, output_prefix='workstation/test')
    with pytest.raises(ValueError, match='negative'):
        bridge().build_graph('minimax-h3-fl2va', prompt='ball', negative_prompt='blur',
                             duration=5, aspect_ratio='16:9', seed=1,
                             preview=False, output_prefix='workstation/test')


def test_workflow_edit_requires_reconfiguration():
    with pytest.raises(ValueError, match='Workflow changed'):
        bridge().build_graph('wan-2.2', prompt='ball', negative_prompt='', duration=5,
                             aspect_ratio='16:9', seed=1, preview=False,
                             output_prefix='workstation/test', workflow_sha256='0' * 64)


def test_configured_profiles_remain_unadmitted_and_repeated_setup_preserves_existing_benchmark(tmp_path):
    from video_workstation.config import Settings
    from video_workstation.db import Database
    from video_workstation.services.models import model_readiness, model_config_fingerprint
    try:
        deployment = importlib.import_module('video_workstation.services.comfy_deployment')
    except ModuleNotFoundError:
        pytest.fail('Model registration has not been implemented')
    db = Database(Settings(data_dir=tmp_path))
    db.create_schema()
    with db.session() as session:
        profiles = deployment.configure_profiles(session, python=sys.executable, ffmpeg='ffmpeg')
        assert len(profiles) == 3
        assert all(p.enabled and model_readiness(p) == 'configured' for p in profiles)
        command = profiles[0].runtime_config_json['command']
        assert '--preview' not in command
        assert '--workflow-sha256' in command
        assert any('{prompt}' in arg for arg in command) and '{output}' in command
        profiles[0].validated_presets_json = [{'duration_seconds': 5, 'aspect_ratio': '16:9',
                                              'config_fingerprint': model_config_fingerprint(profiles[0])}]
        session.flush()
        repeated = deployment.configure_profiles(session, python=sys.executable, ffmpeg='ffmpeg')
        assert model_readiness(repeated[0]) == 'admitted'


def test_ffprobe_utf8_metadata_is_read_independently_of_windows_locale(tmp_path, monkeypatch):
    import subprocess
    from video_workstation.qc import probe_media
    payload = json.dumps({'streams': [{'codec_type': 'video', 'width': 320, 'height': 192},
                                      {'codec_type': 'audio'}],
                          'format': {'duration': '5.0', 'tags': {'comment': '中文模型路径'}}},
                         ensure_ascii=False).encode('utf-8')

    def ffprobe_result(argv, **kwargs):
        # FFprobe emits UTF-8, even on a machine whose default locale is GBK.
        text = payload.decode(kwargs.get('encoding') or 'gbk')
        return subprocess.CompletedProcess(argv, 0, stdout=text, stderr='')

    monkeypatch.setattr(subprocess, 'run', ffprobe_result)
    assert probe_media(tmp_path / '中文视频.mp4', expected_duration=5).passed


def test_prompt_starting_with_an_option_is_forwarded_as_literal_text(tmp_path, monkeypatch):
    from video_workstation.config import Settings
    from video_workstation.db import Database
    from video_workstation.services.comfy_deployment import configure_profiles
    from video_workstation.adapters.base import GenerationRequest
    from video_workstation.adapters.local_command import LocalCommandAdapter
    db = Database(Settings(data_dir=tmp_path))
    db.create_schema()
    captured = []

    def generate(self, graph, output, **kwargs):
        captured.append(graph['4']['inputs']['text'])
        output.write_bytes(b'video')

    monkeypatch.setattr(bridge().ComfyClient, 'generate', generate)
    with db.session() as session:
        profile = configure_profiles(session, python=sys.executable, ffmpeg='ffmpeg')[1]
        argv = LocalCommandAdapter(profile.runtime_config_json['command'], profile.slug).render_argv(
            GenerationRequest('job', '--seed', tmp_path / 'result.mp4', 5, '16:9', 42,
                              negative_prompt='--output'))
        assert bridge().main(argv[2:]) == 0
        assert captured == ['--seed']

"""Explicitly register a verified writer package; never admit video presets."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sqlalchemy import select
from video_workstation.config import Settings
from video_workstation.db import Database
from video_workstation.models import ModelProfile
from video_workstation.services.models import model_config_fingerprint, seed_model_profiles
from video_workstation.writer_deployment import file_sha256, validate_writer_deployment
from video_workstation.writing_contracts import WritingBody


def register(manifest_path: Path, report_path: Path, data_dir: Path):
    manifest_path, report_path = manifest_path.resolve(), report_path.resolve()
    info = validate_writer_deployment(manifest_path)
    report = json.loads(report_path.read_text(encoding='utf-8'))
    digest = file_sha256(report_path)
    package_fingerprint = hashlib.sha256(json.dumps(
        {key: value for key, value in info['manifest'].items() if key != 'verification'},
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    previous = info['manifest'].get('verification') or {}
    if (report.get('passed') is not True or report.get('engine_version') != info['manifest']['engine']['version']
        or (report.get('manifest_sha256') != info['manifest_sha256'] and
            (previous.get('report_sha256') != digest or previous.get('package_fingerprint') != package_fingerprint))):
        raise ValueError('Verification report does not match this package')
    cases = report['cases']
    required = {('story', 'generate'), ('product', 'generate'), ('knowledge', 'generate'), ('story', 'custom')}
    if {(row['recipe'], row['operation']) for row in cases} != required:
        raise ValueError('Missing required real generation cases')
    for row in cases:
        if row.get('passed') is not True or (row['operation'] == 'custom' and not row.get('manual_input_preserved')):
            raise ValueError('Unsuccessful verification case')
        output = Path(row['output']).resolve()
        if report_path.parent not in output.parents:
            raise ValueError('Verification output must stay beside its report')
        body = WritingBody.model_validate_json(output.read_text(encoding='utf-8'))
        body.validate_for_target(row['shot_count'] * 5)
    manifest = info['manifest']
    manifest['verification'] = {'report': str(report_path), 'report_sha256': digest,
        'package_fingerprint': package_fingerprint,
        'finished_at': report['finished_at'], 'scope': 'structured local writing only; human fact checking required',
        'network_isolation': report.get('network_isolation', 'not_yet_verified')}
    temporary = manifest_path.with_suffix('.registering.json')
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(manifest_path)
    database = Database(Settings(data_dir=data_dir))
    database.migrate()
    with database.session() as session:
        seed_model_profiles(session)
        profile = session.scalar(select(ModelProfile).where(ModelProfile.slug == 'qwen-local'))
        profile.provider = 'local'
        profile.enabled = True
        profile.model_version = f"Qwen3-8B@{manifest['model']['revision'][:8]} / llama-{manifest['engine']['version']} / writer-v1"
        profile.quantization = 'Q4_K_M / CPU'
        profile.runtime_config_json = {'writer_manifest': str(manifest_path), 'manifest_sha256': file_sha256(manifest_path)}
        profile.capabilities_json = {**(profile.capabilities_json or {}), 'text_ready': True,
            'text_ready_fingerprint': model_config_fingerprint(profile), 'writing_verification': manifest['verification']}
        profile.validated_presets_json = []
        profile.license_name = 'Qwen Apache-2.0 / llama.cpp MIT'
        profile.license_version = manifest['model']['revision']
        profile.notice = '本地文案草稿需人工确认和事实核对；不代表视频模型档位通过验证。'
    return database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--data-dir', required=True, type=Path)
    args = parser.parse_args()
    register(args.manifest, args.report, args.data_dir)
    print('Local writing registered; video admission unchanged.')


if __name__ == '__main__':
    main()

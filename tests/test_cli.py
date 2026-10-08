from video_workstation.cli import build_parser


def test_cli_exposes_worker_and_model_operations():
    parser = build_parser()
    assert parser.parse_args(["worker", "--once"]).once is True
    configured = parser.parse_args(
        [
            "configure-model",
            "minimax-h3-fl2va",
            "--command-json",
            "h3-command.json",
            "--version",
            "H3-Base",
            "--quantization",
            "pruned-int8",
        ]
    )
    assert configured.command == "configure-model"
    assert configured.slug == "minimax-h3-fl2va"
    benchmark = parser.parse_args(
        ["record-benchmark", "minimax-h3-fl2va", "5", "16:9", "runs.json"]
    )
    assert benchmark.duration == 5
    assert benchmark.aspect_ratio == "16:9"

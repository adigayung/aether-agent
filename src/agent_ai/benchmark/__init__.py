"""Benchmark Coding Tasks AETHER (#49).

Benchmark coding nyata yang memakai Evaluation Framework #48 untuk mengukur
kemampuan AETHER secara reproducible. Provider/model agnostic.

    from agent_ai.benchmark import (
        BenchmarkCaseRegistry,
        BenchmarkRunner,
        BenchmarkReportBuilder,
        build_registry,
    )

    registry = build_registry()
    runner = BenchmarkRunner(target=my_callable, registry=registry)
    run = runner.run(run_id="qwen")
    report = BenchmarkReportBuilder.build(run)

Prinsip:
    - Benchmark adalah ALAT UKUR AETHER, bukan bagian dari cara AETHER berpikir.
    - Memakai Evaluation Framework #48 (tidak membuat runtime/loop/engine baru).
    - Fixture temporary & terisolasi (BUKAN project AETHER), dibersihkan setelah.
    - Tidak ada penilaian subjektif, penyimpanan vektor, basis data, UI, atau API.
    - Tidak menjalankan model/API nyata; target diberikan pemanggil.
"""

from agent_ai.benchmark.cases import (
    BenchmarkCaseRegistry,
    build_registry,
    builtin_cases,
)
from agent_ai.benchmark.models import (
    BenchmarkCase,
    BenchmarkExpected,
    BenchmarkInput,
    BenchmarkResult,
    BenchmarkRun,
    BenchmarkStatus,
    FixtureSpec,
)
from agent_ai.benchmark.reports import (
    BenchmarkCaseReport,
    BenchmarkReport,
    BenchmarkReportBuilder,
)
from agent_ai.benchmark.runner import BenchmarkRunner, BenchmarkTarget

__all__ = [
    "BenchmarkCase",
    "BenchmarkInput",
    "BenchmarkExpected",
    "BenchmarkResult",
    "BenchmarkRun",
    "BenchmarkStatus",
    "FixtureSpec",
    "BenchmarkCaseRegistry",
    "BenchmarkRunner",
    "BenchmarkTarget",
    "BenchmarkReport",
    "BenchmarkCaseReport",
    "BenchmarkReportBuilder",
    "build_registry",
    "builtin_cases",
]

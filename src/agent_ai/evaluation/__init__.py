"""Evaluation Framework AETHER (#48).

Framework evaluasi internal untuk mengukur perilaku agent secara repeatable.
Provider/model agnostic. TIDAK membuat AgentRuntime/AgentLoop/engine eksekusi
baru: runner menjalankan case melalui callable yang DIBERIKAN pemanggil.

    from agent_ai.evaluation import (
        EvaluationCase,
        EvaluationInput,
        EvaluationExpected,
        EvaluationCaseRegistry,
        EvaluationRunner,
        MetricsCollector,
        ReportBuilder,
    )

    registry = EvaluationCaseRegistry()
    registry.register(EvaluationCase(id="c1", input=EvaluationInput(task="...")))
    runner = EvaluationRunner(target=my_callable, registry=registry)
    run = runner.run()
    report = ReportBuilder.build(run)

Dapat dipakai untuk membandingkan konfigurasi (model A vs B, routing aktif vs
nonaktif, recovery aktif vs nonaktif) dengan menjalankan run terpisah.

Tidak ada penilaian subjektif, penyimpanan vektor, basis data, atau UI.
"""

from agent_ai.evaluation.cases import EvaluationCaseRegistry
from agent_ai.evaluation.metrics import MetricsCollector
from agent_ai.evaluation.models import (
    EvaluationCase,
    EvaluationExpected,
    EvaluationInput,
    EvaluationResult,
    EvaluationRun,
    EvaluationStatus,
    MetricResult,
)
from agent_ai.evaluation.reports import EvaluationReport, ReportBuilder
from agent_ai.evaluation.runner import EvaluationRunner, EvaluationTarget

__all__ = [
    "EvaluationCase",
    "EvaluationInput",
    "EvaluationExpected",
    "EvaluationResult",
    "EvaluationRun",
    "EvaluationStatus",
    "MetricResult",
    "EvaluationCaseRegistry",
    "EvaluationRunner",
    "EvaluationTarget",
    "MetricsCollector",
    "EvaluationReport",
    "ReportBuilder",
]

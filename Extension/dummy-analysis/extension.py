"""Dummy Data Analysis Extension — example for Task 05.

Demonstrates:
 - Tool (analyze)
 - Config (output_format etc.)
 - UI Form (declarative via config)
 - UI Table
 - UI Chart
 - UI Result Renderer (structured result -> chart/table viewer)
 - UI Modal / Panel / Wizard / Viewer / Action / Custom View
 - Artifact reference

Generic contract only — no hardcode extension name in core.
"""
from agent_ai.extensions import Extension
from agent_ai.tools.base import BaseTool


class AnalyzeTool(BaseTool):
    name = "dummy.analysis.analyze"
    description = "Analyze dummy data and return chart/table"
    input_schema = {
        "type": "object",
        "properties": {
            "dataset": {"type": "string", "description": "dataset name"},
        },
        "required": [],
    }

    def execute(self, dataset: str = "sales", **kwargs):
        # Structured result — UI runtime picks renderer based on type/renderer
        return {
            "type": "chart",
            "renderer": "chart",
            "data": {
                "type": "bar",
                "labels": ["A", "B", "C"],
                "datasets": [{"label": "Sales", "data": [10, 20, 15]}],
                "title": f"Analysis of {dataset}",
            },
            "metadata": {"dataset": dataset},
        }


class DummyAnalysisExtension(Extension):

    def register(self, context):
        # Config (used for declarative form)
        context.config.register(key="output_format", type="enum", choices=["table", "chart", "both"], default="both", description="Output format")
        context.config.register(key="api_key", type="secret", required=False, description="Dummy secret key")
        context.config.register(key="base_url", type="url", default="http://example.com", description="Base URL")
        context.config.register(key="max_items", type="integer", default=50, description="Max items")
        context.config.register(key="enable_feature", type="boolean", default=True, description="Enable feature")
        context.config.register(key="notes", type="string", default="", description="Notes")
        context.config.register(key="extra_json", type="json", default={"x": 1}, description="Extra JSON")

        # Tool via existing ToolRegistry path (namespaced)
        context.tools.register(AnalyzeTool())

        # UI — declarative, generic (no hardcode)
        context.ui.register(
            id="dummy.analysis.settings",
            type="form",
            title="Analysis Settings",
            description="Configure analysis",
            schema={
                "type": "form",
                "fields": [
                    {"key": "output_format", "type": "enum", "field_type": "select", "choices": ["table", "chart", "both"]},
                    {"key": "max_items", "type": "integer", "field_type": "number"},
                ],
            },
        )
        context.ui.register(
            id="dummy.analysis.table",
            type="table",
            title="Results Table",
            description="Table view of results",
            props={
                "columns": [
                    {"key": "name", "title": "Name"},
                    {"key": "value", "title": "Value"},
                ],
                "rows": [
                    {"name": "A", "value": 10},
                    {"name": "B", "value": 20},
                ],
            },
        )
        context.ui.register(
            id="dummy.analysis.chart",
            type="chart",
            title="Sales Chart",
            description="Chart view",
            props={
                "type": "bar",
                "labels": ["A", "B", "C"],
                "datasets": [{"label": "Sales", "data": [10, 20, 15]}],
            },
        )
        context.ui.register(
            id="dummy.analysis.viewer_image",
            type="viewer",
            title="Image Viewer",
            description="Generic image viewer",
            props={"viewer_type": "image", "mime_type": "image/png"},
        )
        context.ui.register(
            id="dummy.analysis.modal_settings",
            type="modal",
            title="Settings Modal",
            props={"size": "large"},
            actions=[{"id": "submit"}],
        )
        context.ui.register(
            id="dummy.analysis.panel_live",
            type="panel",
            title="Live Panel",
            props={"placement": "sidebar", "slot": "right"},
        )
        context.ui.register(
            id="dummy.analysis.wizard_setup",
            type="wizard",
            title="Setup Wizard",
            props={
                "steps": [
                    {"title": "API configuration", "fields": [{"key": "api_key"}]},
                    {"title": "Model selection", "fields": [{"key": "output_format"}]},
                    {"title": "Output configuration", "fields": [{"key": "max_items"}]},
                    {"title": "Confirm", "fields": []},
                ]
            },
        )
        context.ui.register(
            id="dummy.analysis.result_renderer",
            type="result_renderer",
            title="Result Renderer",
            props={"renderer": "chart"},
        )
        context.ui.register(
            id="dummy.analysis.action_generate",
            type="action",
            title="Generate",
            props={"target_tool": "dummy.analysis.analyze"},
            actions=[{"id": "dummy.analysis.analyze"}],
        )
        context.ui.register(
            id="dummy.analysis.custom_view",
            type="custom_view",
            title="Custom HTML View",
            entry="ui/views/custom.html",
            props={"sandbox": False},
        )


extension = DummyAnalysisExtension()

from agent_ai.extensions import Extension


class TestExtension(Extension):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logs = []

    def register(self, context):
        self.logs.append("register")
        # Demonstrate Config + Storage (Task 04 example)
        context.config.register(
            key="api_key",
            type="secret",
            required=True,
            description="API key for service",
        )
        context.config.register(
            key="base_url",
            type="url",
            default="http://localhost:8188",
            description="Base service URL",
        )
        context.config.register(
            key="timeout",
            type="integer",
            default=60,
            description="Timeout in seconds",
        )
        context.config.register(
            key="quality",
            type="enum",
            choices=["draft", "standard", "high"],
            default="standard",
            description="Quality preset",
        )
        # Storage example: store last job id (runtime state)
        try:
            context.storage.set("last_job_id", "example-job-123")
        except Exception:
            pass

    def enable(self, context):
        self.logs.append("enable")

    def disable(self, context):
        self.logs.append("disable")


extension = TestExtension()

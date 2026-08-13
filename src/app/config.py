from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/autonomous_ops"
    redis_url: str = "redis://localhost:6379/0"

    github_token: str = ""
    github_repo: str = ""

    monitored_endpoints: str = ""  # comma-separated URLs; see monitored_endpoint_list
    app_log_path: str = ""

    llm_provider: str = "undecided"
    llm_model: str = ""  # blank = provider's default (see app.llm._DEFAULT_MODELS)
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openrouter_api_key: str = ""

    max_recovery_attempts: int = 3
    confidence_threshold: float = 0.7
    require_human_approval: bool = True

    @property
    def monitored_endpoint_list(self) -> list[str]:
        return [url.strip() for url in self.monitored_endpoints.split(",") if url.strip()]


settings = Settings()

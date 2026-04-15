from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    melcloud_email: str
    melcloud_password: str
    poll_interval: int = Field(default=60, ge=10, le=3600)
    api_host: str = "0.0.0.0"
    api_port: int = 8099

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    melcloud_email: str
    melcloud_password: str
    poll_interval: int = 60
    api_host: str = "0.0.0.0"
    api_port: int = 8099
    chromium_path: str = "/usr/bin/chromium"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()

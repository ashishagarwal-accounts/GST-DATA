from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "mysql+pymysql://gst_app:change_me@localhost:3306/alcove_gst?charset=utf8mb4"
    test_database_url: str | None = None  # scratch DB used by scripts/smoke_test.py
    upload_max_mb: int = 25
    cookie_secure: bool = False  # set COOKIE_SECURE=true once the app is served over HTTPS


settings = Settings()

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    gemini_model: str = Field(default="gemini-1.5-flash", alias="GEMINI_MODEL")
    allowed_users_raw: str = Field(default="", alias="ALLOWED_USERS")
    allowed_chats_raw: str = Field(default="", alias="ALLOWED_CHATS")

    postgres_db: str = Field(default="orderbot", alias="POSTGRES_DB")
    postgres_user: str = Field(default="orderbot", alias="POSTGRES_USER")
    postgres_password: str = Field(default="orderbot_password", alias="POSTGRES_PASSWORD")
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def allowed_users(self) -> set[int]:
        return self._parse_int_set(self.allowed_users_raw)

    @property
    def allowed_chats(self) -> set[int]:
        return self._parse_int_set(self.allowed_chats_raw)

    @staticmethod
    def _parse_int_set(raw_values: str) -> set[int]:
        values: set[int] = set()
        for raw in raw_values.split(","):
            raw = raw.strip()
            if raw:
                values.add(int(raw))
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()

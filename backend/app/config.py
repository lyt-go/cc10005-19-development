"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    app_name: str = "多源异构数据接入引擎"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 允许在受控环境下注入批次写入失败，用于验证「解析与清洗同生共死」；
    # 默认 0 表示不注入，只接受 fail_after / empty_xpath 两种值。
    cleaning_failure_injection: str = field(
        default_factory=lambda: __import__("os").environ.get("CLEANING_FAILURE_INJECTION", "")
    )


settings = Settings()

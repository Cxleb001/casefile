"""Settings for Casefile, read from the project's .env file and the environment.

Every other module asks for settings through ``get_settings()`` so there is one
place that knows variable names and defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Read .env first, so the folder settings below can come from it too (the environment still wins).
load_dotenv(PROJECT_ROOT / ".env", override=False)

DATA_DIR = PROJECT_ROOT / "data"
# Sample mode: a small slice of the real data and recorded model answers, bundled in sample/, so the
# whole agent runs with no Zetaris, no Nightshift and no keys. See sample/README.md.
SAMPLE_DIR = PROJECT_ROOT / "sample"
SAMPLE_MODE = os.environ.get("SAMPLE_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
# Overridable so a second dataset (e.g. a tuning batch) can be evaluated without moving files.
PREPARED_DIR = Path(os.environ.get("CASEFILE_PREPARED_DIR")
                    or (SAMPLE_DIR / "data" if SAMPLE_MODE else DATA_DIR / "prepared")).resolve()
EVAL_DIR = Path(os.environ.get("CASEFILE_EVAL_DIR") or (SAMPLE_DIR if SAMPLE_MODE else PROJECT_ROOT / "eval")).resolve()
RUNS_DIR = PROJECT_ROOT / "runs" / "sample" if SAMPLE_MODE else PROJECT_ROOT / "runs"

# Logical table keys -> the environment variable that can override the name
# Zetaris knows the table by.
TABLE_ENV = {
    "ledger_a": "CASEFILE_TABLE_LEDGER_A",
    "kyc_a": "CASEFILE_TABLE_KYC_A",
    "ledger_b": "CASEFILE_TABLE_LEDGER_B",
    "kyc_b": "CASEFILE_TABLE_KYC_B",
    "watchlist": "CASEFILE_TABLE_WATCHLIST",
}

# Default registered names, matching scripts/zetaris/register_sources.sql.
# How much each step lets a Nemotron model think before it answers: "off", "medium"
# or "on". Thinking tokens count against the reply's token cap and take time, so the
# many short investigation steps run without it. Override with CASEFILE_THINKING_<STEP>.
DEFAULT_THINKING = {"fast": "off", "plan": "off", "report": "medium", "verify": "medium"}

DEFAULT_TABLES = {
    "ledger_a": "inst_a.ledger_a",
    "kyc_a": "inst_a.kyc_a",
    "ledger_b": "inst_b.ledger_b",
    "kyc_b": "shared.kyc_b",
    "watchlist": "compliance.watchlist",
}

# Default model per step for each provider. "plan" runs the investigation loop
# (many short tool choices, so a fast model), "report" writes the decision and SAR
# draft (defaults to the verify model), "verify" checks the claims (the strongest
# model); "fast" is spare for quick extraction. Override any of them in .env; `python scripts/probe_models.py`
# shows which models your key can actually call.
DEFAULT_MODELS = {
    "nvidia": {
        "fast": "nvidia/nemotron-3.5-lightning-30b-a3b",
        "plan": "nvidia/nemotron-3.5-lightning-30b-a3b",
        "verify": "nvidia/nemotron-3-ultra-550b-a55b",
    },
    # Nightshift model ids are routing modes, not models: the runtime picks from the fleet it has
    # (local lanes, or cloud models such as NVIDIA's once their key is added in Nightshift).
    "meterless": {
        "fast": "meterless/text.fast",
        "plan": "meterless/text.fast",
        "verify": "meterless/text.deep",
    },
}


def normalize_runtime_url(url: str) -> str:
    """Normalise a pasted Meterless Runtime address to <host>/v1 (Meterless provider contract).

    Accepts it with or without a scheme, and ending in /v1, /v1/chat/completions, /v1/models or /healthz;
    never produces /v1/v1.
    """
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    text = text.rstrip("/")
    stripped = True
    while stripped:
        stripped = False
        for suffix in ("/chat/completions", "/models", "/healthz", "/v1"):
            if text.endswith(suffix):
                text = text[: -len(suffix)].rstrip("/")
                stripped = True
    return text + "/v1"


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    return value.strip() if value.strip() else default


@dataclass(frozen=True)
class Settings:
    data_backend: str
    tables: dict[str, str]

    zetaris_route: str
    zetaris_rest_url: str
    zetaris_base_url: str
    zetaris_org_id: str
    zetaris_api_key: str
    zetaris_username: str
    zetaris_password: str
    zetaris_engine_id: str
    zetaris_query_limit: int

    llm_provider: str
    llm_base_url: str
    llm_api_key: str
    models: dict[str, str] = field(default_factory=dict)
    thinking: dict[str, str] = field(default_factory=dict)

    sample_mode: bool = False

    pg_url: str = ""
    s3_bucket: str = ""
    s3_prefix: str = "casefile"
    aws_region: str = "us-east-1"

    def table(self, key: str) -> str:
        """Registered name for a logical table, e.g. table('ledger_a') -> 'inst_a.ledger_a'."""
        try:
            return self.tables[key]
        except KeyError as error:
            known = ", ".join(sorted(self.tables))
            raise KeyError(f"Unknown table key {key!r}; known keys: {known}") from error


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    provider = _env("CASEFILE_LLM_PROVIDER", "nvidia").lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError(
            f"CASEFILE_LLM_PROVIDER must be one of {sorted(DEFAULT_MODELS)}, got {provider!r}"
        )
    if provider == "nvidia":
        base_url = _env("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
        api_key = _env("NVIDIA_API_KEY")
    else:
        base_url = normalize_runtime_url(_env("METERLESS_BASE_URL", "http://127.0.0.1:8088/v1"))
        api_key = _env("METERLESS_TOKEN") or _env("METERLESS_RUNTIME_TOKEN")

    defaults = DEFAULT_MODELS[provider]
    models = {
        "fast": _env("CASEFILE_MODEL_FAST", defaults["fast"]),
        "plan": _env("CASEFILE_MODEL_PLAN", defaults["plan"]),
        "verify": _env("CASEFILE_MODEL_VERIFY", defaults["verify"]),
    }
    models["report"] = _env("CASEFILE_MODEL_REPORT", models["verify"])
    thinking = {}
    for step, default in DEFAULT_THINKING.items():
        level = _env(f"CASEFILE_THINKING_{step.upper()}", default).lower()
        if level not in {"off", "medium", "on"}:
            raise ValueError(f"CASEFILE_THINKING_{step.upper()} must be off, medium or on, got {level!r}")
        thinking[step] = level

    backend = "local" if SAMPLE_MODE else _env("CASEFILE_DATA_BACKEND", "local").lower()
    if backend not in {"local", "zetaris"}:
        raise ValueError(f"CASEFILE_DATA_BACKEND must be 'local' or 'zetaris', got {backend!r}")

    route = _env("ZETARIS_ROUTE", "rest").lower()
    if route not in {"rest", "proxy"}:
        raise ValueError(f"ZETARIS_ROUTE must be 'rest' or 'proxy', got {route!r}")

    try:
        query_limit = int(_env("ZETARIS_QUERY_LIMIT", "5000"))
    except ValueError as error:
        raise ValueError("ZETARIS_QUERY_LIMIT must be a whole number") from error

    return Settings(
        data_backend=backend,
        tables={key: _env(var, DEFAULT_TABLES[key]) for key, var in TABLE_ENV.items()},
        zetaris_route=route,
        zetaris_rest_url=_env("ZETARIS_REST_URL").rstrip("/"),
        zetaris_base_url=_env("ZETARIS_BASE_URL").rstrip("/"),
        zetaris_org_id=_env("ZETARIS_ORG_ID"),
        zetaris_api_key=_env("ZETARIS_API_KEY"),
        zetaris_username=_env("ZETARIS_USERNAME"),
        zetaris_password=_env("ZETARIS_PASSWORD"),
        zetaris_engine_id=_env("ZETARIS_ENGINE_ID"),
        zetaris_query_limit=query_limit,
        llm_provider=provider,
        llm_base_url=base_url,
        llm_api_key=api_key,
        models=models,
        thinking=thinking,
        sample_mode=SAMPLE_MODE,
        pg_url=_env("PG_URL"),
        s3_bucket=_env("S3_BUCKET"),
        s3_prefix=_env("S3_PREFIX", "casefile").strip("/"),
        aws_region=_env("AWS_REGION", "us-east-1"),
    )

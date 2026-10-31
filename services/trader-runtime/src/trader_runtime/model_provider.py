from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

DEEPSEEK_MODELS = ("deepseek-flash", "deepseek-v4-pro")
DEFAULT_DEEPSEEK_MODEL = DEEPSEEK_MODELS[0]
ZHIPU_MODELS = ("glm-5.3-flash",)
# Every provider speaks the OpenAI Responses wire; these are the documented
# Responses endpoints (Zhipu's /api/paas/v4 is chat-only).
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/"
# Kimi Code 套餐使用自己的模型名；模型升级只改这里。
KIMI_MODEL = "k3.1-pro"
KIMI_BASE_URL = "https://api.kimi.com/coding/v1"


def canonical_model_slug(slug: str) -> str:
    return KIMI_MODEL if slug in {"gpt-6-astra", "kimi-k3"} else slug


def validate_model_url(value: str) -> str:
    parsed = urlsplit(value)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query
            or parsed.fragment or not (parsed.scheme == "https" or (local and parsed.scheme == "http"))):
        raise ValueError("model_url_invalid")
    return value.rstrip("/")


@dataclass(frozen=True)
class ModelSpec:
    slug: str
    provider: str
    display_name: str
    tier: str  # "all" | "member" | "platform" (Runtime-internal: never offered for the user's conversations)
    api_key_env: str


MODEL_CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec("deepseek-flash", "deepseek", "DeepSeek V4.1 Flash", "all", "DEEPSEEK_API_KEY"),
    ModelSpec("glm-5.3-flash", "zhipu", "GLM 5.3 Flash", "all", "ZHIPU_API_KEY"),
    ModelSpec(KIMI_MODEL, "moonshot", "Kimi K3.1 Pro", "member", "MOONSHOT_API_KEY"),
)
# The models a user can pick for their own conversations (plans, the model menu, the owner's tier editor).
USER_MODEL_CATALOG: tuple[ModelSpec, ...] = tuple(spec for spec in MODEL_CATALOG if spec.tier != "platform")

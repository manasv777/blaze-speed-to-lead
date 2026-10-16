"""Settings loaded from the environment (.env via python-dotenv)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEV_APP_SECRET = "dev-app-secret-change-me"
DEFAULT_MODEL = "claude-haiku-5-5"


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    anthropic_api_key: str = ""
    model: str = DEFAULT_MODEL
    llm_timeout_s: float = 15.0          # hard per-attempt timeout
    llm_budget_s: float = 35.0           # all attempts together, keeps alert < 60s
    fb_app_secret: str = DEV_APP_SECRET
    fb_verify_token: str = ""
    sheets_url: str = ""
    sheets_token: str = ""
    notifier: str = "discord"            # discord | slack
    discord_webhook_url: str = ""
    slack_webhook_url: str = ""
    booking_url: str = "https://cal.com/your-handle/15min"
    data_dir: Path = ROOT / "data"
    mock_llm: bool = False
    mock_sheet: bool = False
    mock_notify: bool = False
    mock_llm_fail: str = ""              # "", "timeout", "garbage" (demo the fallback)
    sheet_timeout_s: float = 10.0
    notify_timeout_s: float = 10.0
    demo_enabled: bool = True
    price_in_per_mtok: float = 0.10      # Claude Haiku 5.5 list price, USD
    price_out_per_mtok: float = 0.50

    @property
    def leads_path(self) -> Path:
        return self.data_dir / "leads.jsonl"

    @classmethod
    def from_env(cls, mock: bool | None = None) -> "Settings":
        load_dotenv(ROOT / ".env")
        mock_all = _bool("MOCK") if mock is None else mock
        s = cls(
            # BLAZE_ANTHROPIC_API_KEY: for hosts that reserve ANTHROPIC_API_KEY (e.g. Claude Code cloud).
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or os.getenv("BLAZE_ANTHROPIC_API_KEY", ""),
            model=os.getenv("ANTHROPIC_MODEL") or DEFAULT_MODEL,
            llm_timeout_s=float(os.getenv("LLM_TIMEOUT_S") or 15),
            fb_app_secret=os.getenv("FB_APP_SECRET") or DEV_APP_SECRET,
            fb_verify_token=os.getenv("FB_VERIFY_TOKEN", ""),
            sheets_url=os.getenv("SHEETS_WEBAPP_URL", ""),
            sheets_token=os.getenv("SHEETS_TOKEN", ""),
            notifier=(os.getenv("NOTIFIER") or "discord").lower(),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", ""),
            booking_url=os.getenv("BOOKING_URL") or cls.booking_url,
            data_dir=Path(os.getenv("DATA_DIR") or ROOT / "data"),
            mock_llm=mock_all or _bool("MOCK_LLM"),
            mock_sheet=mock_all or _bool("MOCK_SHEET"),
            mock_notify=mock_all or _bool("MOCK_NOTIFY"),
            mock_llm_fail=os.getenv("MOCK_LLM_FAIL", ""),
            demo_enabled=_bool("DEMO_ENABLED", True),
        )
        return s

    def problems(self) -> list[str]:
        """Config that would make the real (non-mock) path fail."""
        out = []
        if not self.mock_llm and not self.anthropic_api_key:
            out.append("ANTHROPIC_API_KEY (or BLAZE_ANTHROPIC_API_KEY) is empty (or run with --mock / MOCK_LLM=1)")
        if not self.mock_sheet and not (self.sheets_url and self.sheets_token):
            out.append("SHEETS_WEBAPP_URL / SHEETS_TOKEN empty (or MOCK_SHEET=1)")
        if not self.mock_notify:
            if self.notifier == "slack" and not self.slack_webhook_url:
                out.append("SLACK_WEBHOOK_URL empty (or MOCK_NOTIFY=1)")
            if self.notifier != "slack" and not self.discord_webhook_url:
                out.append("DISCORD_WEBHOOK_URL empty (or MOCK_NOTIFY=1)")
        if self.fb_app_secret == DEV_APP_SECRET and not self.mock_llm:
            out.append("FB_APP_SECRET not set; using the insecure dev default")
        return out

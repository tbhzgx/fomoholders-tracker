"""Configuration management for the wallet tracker."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# Load .env file from project root
_project_root = Path(__file__).parent.parent
_env_path = _project_root / ".env"
_config_path = _project_root / "config.json"

load_dotenv(_env_path)

# Defaults (used when config.json is missing or incomplete)
_DEFAULTS = {
    "token_amount_pct": 0.001,   # 0.1% tolerance on token amount
    "max_holder_pages": 50,      # max pages of holders to scan
}


def _load_config_json() -> dict:
    """Load config.json from project root. Returns empty dict if missing."""
    if not _config_path.exists():
        return {}
    try:
        with open(_config_path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] Could not parse config.json: {e}  -- using defaults")
        return {}


@dataclass
class Tolerances:
    """Matching tolerances for holder identification."""
    # Token amount tolerance (fraction, e.g. 0.001 = 0.1%)
    token_amount: float = _DEFAULTS["token_amount_pct"]


@dataclass
class Config:
    """Application configuration."""
    # API Keys (at least one required depending on which chains you use)
    helius_api_key: str | None = None   # For Solana holder lookups
    moralis_api_key: str | None = None  # For Base/BNB holder lookups & top traders
    mobula_api_key: str | None = None   # For top traders (PnL data, all chains)
    fomo_bearer_token: str | None = None        # Fomo.family JWT (60-min access token)
    fomo_installation_id: str | None = None     # Fomo.family x-app-installation-id
    fomo_refresh_token: str | None = None       # Privy long-lived refresh token (optional)

    # Tolerances
    tolerances: Tolerances | None = None

    # Search settings
    max_holder_pages: int = _DEFAULTS["max_holder_pages"]

    def __post_init__(self):
        if self.tolerances is None:
            self.tolerances = Tolerances()

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from .env + config.json."""
        helius_key = os.getenv("HELIUS_API_KEY", "") or None
        moralis_key = os.getenv("MORALIS_API_KEY", "") or None
        mobula_key = os.getenv("MOBULA_API_KEY", "") or None
        fomo_bearer = os.getenv("FOMO_BEARER_TOKEN", "") or None
        fomo_install = os.getenv("FOMO_INSTALLATION_ID", "") or None
        fomo_refresh = os.getenv("FOMO_REFRESH_TOKEN", "") or None

        if not helius_key and not moralis_key:
            raise ValueError(
                "At least one API key is required:\n"
                "- HELIUS_API_KEY for Solana (https://helius.dev)\n"
                "- MORALIS_API_KEY for Base/BNB (https://moralis.io)"
            )

        # Read user-editable config.json
        user_cfg = _load_config_json()
        tol_cfg = user_cfg.get("tolerances", {})

        tolerances = Tolerances(
            token_amount=float(tol_cfg.get(
                "token_amount_pct", _DEFAULTS["token_amount_pct"]
            )),
        )

        max_pages = int(user_cfg.get(
            "max_holder_pages", _DEFAULTS["max_holder_pages"]
        ))

        return cls(
            helius_api_key=helius_key,
            moralis_api_key=moralis_key,
            mobula_api_key=mobula_key,
            fomo_bearer_token=fomo_bearer,
            fomo_installation_id=fomo_install,
            fomo_refresh_token=fomo_refresh,
            tolerances=tolerances,
            max_holder_pages=max_pages,
        )

    @classmethod
    def load(cls) -> "Config":
        """Load configuration, with helpful error messages."""
        try:
            return cls.from_env()
        except ValueError as e:
            print(f"\n[ERROR] Configuration Error:\n{e}\n")
            print("Setup instructions:")
            print("1. Copy .env.example to .env")
            print("2. For Solana: Sign up at https://helius.dev, add HELIUS_API_KEY")
            print("3. For Base/BNB: Sign up at https://moralis.io, add MORALIS_API_KEY")
            print("4. You can use one or both depending on which chains you need\n")
            raise


def get_config() -> Config:
    """Get the application configuration (singleton pattern)."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


_config: Config | None = None

"""
Pydantic models for trading system configuration.

This module provides validated data models for:
- API credentials (ByBit, Binance, etc.)
- Exchange configurations
- Trading parameters
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator


class Credentials(BaseModel):
    """API credentials for an exchange."""

    api_key: str = Field(..., min_length=1, description="API key")
    api_secret: str = Field(..., min_length=1, description="API secret")
    exchange: str = Field(default="bybit", description="Exchange name")
    testnet: bool = Field(default=True, description="Use testnet")

    @field_validator("api_key", "api_secret")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("API key and secret must be non-empty")
        return v.strip()

    def mask_api_key(self) -> str:
        """Return masked API key for logging."""
        if len(self.api_key) <= 8:
            return "***"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (excluding secret)."""
        return {
            "api_key": self.mask_api_key(),
            "exchange": self.exchange,
            "testnet": self.testnet,
        }


class ByBitConfig(BaseModel):
    """ByBit exchange configuration."""

    api_key: str = Field(default="", description="ByBit API key")
    api_secret: str = Field(default="", description="ByBit API secret")
    testnet: bool = Field(default=True, description="Use testnet")
    default_leverage: int = Field(default=5, ge=1, le=125, description="Default leverage")
    pos_size_multiplier: float = Field(default=1.0, ge=0.01, le=100.0, description="Position size multiplier")
    enabled: bool = Field(default=False, description="Enable ByBit execution")

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def empty_str_to_default(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    def is_configured(self) -> bool:
        """Check if ByBit is properly configured."""
        return bool(self.api_key and self.api_secret and len(self.api_key) > 10 and len(self.api_secret) > 10)


class BinanceConfig(BaseModel):
    """Binance exchange configuration."""

    api_key: str = Field(default="", description="Binance API key")
    api_secret: str = Field(default="", description="Binance API secret")
    testnet: bool = Field(default=True, description="Use testnet")
    enabled: bool = Field(default=False, description="Enable Binance data fetching")

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def empty_str_to_default(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    def is_configured(self) -> bool:
        """Check if Binance is properly configured."""
        return bool(self.api_key and self.api_secret and len(self.api_key) > 10 and len(self.api_secret) > 10)


class TradingConfig(BaseModel):
    """Trading configuration."""

    max_risk_per_trade_pct: float = Field(default=0.02, ge=0.001, le=0.1, description="Max risk per trade as percentage")
    max_position_size_pct: float = Field(default=0.05, ge=0.01, le=1.0, description="Max position size as percentage")
    default_leverage: int = Field(default=5, ge=1, le=125, description="Default leverage")
    dry_run: bool = Field(default=False, description="Dry run mode (no real trades)")
    signal_threshold_buy: float = Field(default=0.65, ge=0.5, le=0.95, description="Buy signal threshold")
    signal_threshold_sell: float = Field(default=0.35, ge=0.05, le=0.5, description="Sell signal threshold")


class AppSettings(BaseModel):
    """Application settings with secrets integration."""

    bybit: ByBitConfig = Field(default_factory=ByBitConfig)
    binance: BinanceConfig = Field(default_factory=BinanceConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)

    @classmethod
    def from_secrets(cls) -> "AppSettings":
        """
        Load settings from Streamlit secrets or environment variables.

        Priority:
        1. Streamlit secrets (st.secrets)
        2. Environment variables
        3. Default values

        Returns:
            AppSettings instance
        """
        bybit_api_key = ""
        bybit_api_secret = ""
        binance_api_key = ""
        binance_api_secret = ""

        # Try Streamlit secrets first
        try:
            import streamlit as st
            if hasattr(st, "secrets") and st.secrets:
                # ByBit credentials
                if "bybit" in st.secrets:
                    bybit_api_key = st.secrets["bybit"].get("api_key", "")
                    bybit_api_secret = st.secrets["bybit"].get("api_secret", "")
                else:
                    bybit_api_key = st.secrets.get("BYBIT_API_KEY", "")
                    bybit_api_secret = st.secrets.get("BYBIT_API_SECRET", "")

                # Binance credentials
                if "binance" in st.secrets:
                    binance_api_key = st.secrets["binance"].get("api_key", "")
                    binance_api_secret = st.secrets["binance"].get("api_secret", "")
                else:
                    binance_api_key = st.secrets.get("BINANCE_API_KEY", "")
                    binance_api_secret = st.secrets.get("BINANCE_API_SECRET", "")

        except ImportError:
            pass
        except Exception:
            pass

        # Fallback to environment variables
        import os

        if not bybit_api_key:
            bybit_api_key = os.environ.get("BYBIT_API_KEY", "")
        if not bybit_api_secret:
            bybit_api_secret = os.environ.get("BYBIT_API_SECRET", "")
        if not binance_api_key:
            binance_api_key = os.environ.get("BINANCE_API_KEY", "")
        if not binance_api_secret:
            binance_api_secret = os.environ.get("BINANCE_API_SECRET", "")

        # Determine testnet setting
        bybit_testnet = True
        try:
            import streamlit as st
            if hasattr(st, "secrets") and st.secrets:
                bybit_testnet = st.secrets.get("BYBIT_TESTNET", "true").lower() == "true"
        except Exception:
            pass
        bybit_testnet = os.environ.get("BYBIT_TESTNET", "true").lower() == "true"

        return cls(
            bybit=ByBitConfig(
                api_key=bybit_api_key,
                api_secret=bybit_api_secret,
                testnet=bybit_testnet,
            ),
            binance=BinanceConfig(
                api_key=binance_api_key,
                api_secret=binance_api_secret,
            ),
        )

    def get_bybit_credentials(self) -> tuple[Optional[str], Optional[str]]:
        """
        Get ByBit API credentials.

        Returns:
            Tuple of (api_key, api_secret) or (None, None) if not configured
        """
        if self.bybit.is_configured():
            return self.bybit.api_key, self.bybit.api_secret
        return None, None

    def get_binance_credentials(self) -> tuple[Optional[str], Optional[str]]:
        """
        Get Binance API credentials.

        Returns:
            Tuple of (api_key, api_secret) or (None, None) if not configured
        """
        if self.binance.is_configured():
            return self.binance.api_key, self.binance.api_secret
        return None, None

    def is_bybit_configured(self) -> bool:
        """Check if ByBit is properly configured."""
        return self.bybit.is_configured()

    def is_binance_configured(self) -> bool:
        """Check if Binance is properly configured."""
        return self.binance.is_configured()

    def get_credentials(self, exchange: str) -> Optional[Credentials]:
        """
        Get credentials for a specific exchange.

        Args:
            exchange: Exchange name ("bybit" or "binance")

        Returns:
            Credentials object or None if not configured
        """
        exchange_lower = exchange.lower()

        if exchange_lower == "bybit":
            if self.bybit.is_configured():
                return Credentials(
                    api_key=self.bybit.api_key,
                    api_secret=self.bybit.api_secret,
                    exchange="bybit",
                    testnet=self.bybit.testnet,
                )
        elif exchange_lower == "binance":
            if self.binance.is_configured():
                return Credentials(
                    api_key=self.binance.api_key,
                    api_secret=self.binance.api_secret,
                    exchange="binance",
                    testnet=self.binance.testnet,
                )

        return None


__all__ = ["Credentials", "ByBitConfig", "BinanceConfig", "TradingConfig", "AppSettings"]

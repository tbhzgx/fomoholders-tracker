"""Mobula API client for cross-chain top trader / PnL lookups."""

from typing import Any

from .base import BaseAPIClient


class MobulaClient(BaseAPIClient):
    """
    Client for Mobula API (FREE tier: 10K credits/month).

    Primary use: Get top trader positions with pre-computed PnL for any chain.
    Endpoint: GET /api/2/token/trader-positions
    """

    BASE_URL = "https://api.mobula.io"

    def __init__(self, api_key: str):
        super().__init__(base_url=self.BASE_URL)
        self.api_key = api_key

    def _get_default_headers(self) -> dict[str, str]:
        """Add Mobula API key to headers."""
        return {
            "Accept": "application/json",
            "Authorization": self.api_key,
            "User-Agent": "WalletTracker/0.3.0",
        }

    def get_top_trader_positions(
        self,
        token_address: str,
        blockchain: str = "solana",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        Get top token trader positions with PnL data.

        Args:
            token_address: Token mint/contract address
            blockchain: Chain identifier (solana, ethereum, base, bsc)
            limit: Results per page (max 1000)
            offset: Pagination offset

        Returns:
            Dict with list of trader position dicts, each containing:
            - walletAddress, tokenAmount, tokenAmountUSD
            - realizedPnlUSD, unrealizedPnlUSD, totalPnlUSD
            - volumeBuyUSD, volumeSellUSD, avgBuyPriceUSD, avgSellPriceUSD
            - buys, sells, firstTradeAt, lastTradeAt
            - labels (sniper, insider, proTrader, etc.)
        """
        params: dict[str, Any] = {
            "address": token_address,
            "blockchain": blockchain,
            "limit": min(limit, 1000),
            "offset": offset,
        }
        return self.get("/api/2/token/trader-positions", params=params)

    def get_all_trader_positions(
        self,
        token_address: str,
        blockchain: str = "solana",
        max_pages: int = 10,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Paginate through top trader positions.

        Args:
            token_address: Token mint/contract address
            blockchain: Chain identifier
            max_pages: Safety limit on pages to fetch
            per_page: Results per page (max 1000)

        Returns:
            List of all trader position dicts
        """
        all_traders: list[dict[str, Any]] = []
        offset = 0

        for _ in range(max_pages):
            response = self.get_top_trader_positions(
                token_address,
                blockchain=blockchain,
                limit=per_page,
                offset=offset,
            )

            traders = response.get("data", [])
            if not traders:
                break

            all_traders.extend(traders)

            if len(traders) < per_page:
                break

            offset += per_page

        return all_traders

    def get_wallet_positions(
        self,
        wallet_address: str,
        blockchain: str = "solana",
    ) -> list[dict[str, Any]]:
        """
        Get all token positions for a wallet on a specific chain.

        Args:
            wallet_address: Wallet address to look up
            blockchain: Chain identifier (solana, base, 56)

        Returns:
            List of position dicts, each containing:
            - token (dict with address, symbol, name, priceUSD, etc.)
            - volumeBuy, volumeSell (USD amounts)
            - realizedPnlUSD, unrealizedPnlUSD, totalPnlUSD
            - buys, sells, avgBuyPriceUSD, avgSellPriceUSD
            - firstDate, lastDate, labels
        """
        params: dict[str, Any] = {
            "wallet": wallet_address,
            "blockchain": blockchain,
        }
        response = self.get("/api/2/wallet/positions", params=params)
        return response.get("data", [])

"""Moralis API client for EVM token holder lookups (Base, BNB Chain)."""

from typing import Any

from .base import BaseAPIClient


class MoralisClient(BaseAPIClient):
    """
    Client for Moralis API (FREE tier: 25K requests/month).

    Primary use: Get token holders for ERC20 tokens on Base and BNB Chain.
    """

    BASE_URL = "https://deep-index.moralis.io/api/v2.2"

    def __init__(self, api_key: str):
        super().__init__(base_url=self.BASE_URL)
        self.api_key = api_key

    def _get_default_headers(self) -> dict[str, str]:
        """Add Moralis API key to headers."""
        return {
            "Accept": "application/json",
            "X-API-Key": self.api_key,
            "User-Agent": "WalletTracker/0.2.0",
        }

    def get_token_holders(
        self,
        token_address: str,
        chain: str = "base",
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """
        Get token holders for an ERC20 token.

        Args:
            token_address: Token contract address (0x...)
            chain: Chain identifier (base, bsc)
            limit: Results per page (max 100)
            cursor: Pagination cursor from previous response

        Returns:
            Dict with 'result' (list of holders) and 'cursor' for pagination
        """
        params: dict[str, Any] = {
            "chain": chain,
            "limit": min(limit, 100),
            "order": "DESC",
        }
        if cursor:
            params["cursor"] = cursor

        return self.get(f"/erc20/{token_address}/owners", params=params)

    def get_all_holders(
        self,
        token_address: str,
        chain: str = "base",
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Paginate through ALL token holders for a token.

        Args:
            token_address: Token contract address
            chain: Chain identifier (base, bsc)
            max_pages: Safety limit on pages to fetch

        Returns:
            List of all holder dicts
        """
        all_holders: list[dict[str, Any]] = []
        cursor: str | None = None
        page = 0

        while page < max_pages:
            response = self.get_token_holders(
                token_address,
                chain=chain,
                limit=100,
                cursor=cursor,
            )

            holders = response.get("result", [])
            if not holders:
                break

            all_holders.extend(holders)
            cursor = response.get("cursor")

            if not cursor:
                break

            page += 1

        return all_holders

    def get_token_metadata(
        self,
        token_address: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        """
        Get token metadata including decimals, name, symbol.

        Args:
            token_address: Token contract address
            chain: Chain identifier (base, bsc)

        Returns:
            Token metadata dict
        """
        params = {"chain": chain}
        return self.get(f"/erc20/metadata", params={**params, "addresses[]": token_address})

    def get_top_gainers(
        self,
        token_address: str,
        chain: str = "base",
        days: str = "all",
    ) -> dict[str, Any]:
        """
        Get top profitable wallets for a token.

        Args:
            token_address: Token contract address
            chain: Chain identifier (base, bsc)
            days: Timeframe — 'all', '7', or '30'

        Returns:
            Dict with token metadata and 'result' list of top gainers.
            Each gainer has: address, avg_buy_price_usd, avg_sell_price_usd,
            total_usd_invested, total_sold_usd, realized_profit_usd,
            realized_profit_percentage, total_tokens_bought, total_tokens_sold,
            count_of_trades
        """
        params: dict[str, Any] = {
            "chain": chain,
            "days": days,
        }
        return self.get(f"/erc20/{token_address}/top-gainers", params=params)

    def get_wallet_profitability(
        self,
        wallet_address: str,
        chain: str = "base",
        token_addresses: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Get PnL breakdown for a specific wallet.

        Args:
            wallet_address: Wallet address
            chain: Chain identifier (base, bsc)
            token_addresses: Optional list of token addresses to filter

        Returns:
            Dict with profitability data per token
        """
        params: dict[str, Any] = {
            "chain": chain,
            "days": "all",
        }
        if token_addresses:
            params["token_addresses[]"] = token_addresses

        return self.get(f"/wallets/{wallet_address}/profitability", params=params)

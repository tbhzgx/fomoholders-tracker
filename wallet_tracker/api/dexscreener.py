"""DexScreener API client for token search and pair data."""

from typing import Any

from .base import BaseAPIClient

# Supported chains for wallet tracking
SUPPORTED_CHAINS = ["solana", "base", "bsc"]


class DexScreenerClient(BaseAPIClient):
    """Client for DexScreener API (FREE - 60 requests/min)."""

    BASE_URL = "https://api.dexscreener.com"

    def __init__(self):
        super().__init__(base_url=self.BASE_URL)

    def search_tokens(self, query: str) -> list[dict[str, Any]]:
        """
        Search for tokens by name, symbol, or address.

        Args:
            query: Search query (ticker symbol, name, or address)

        Returns:
            List of matching pairs with token info
        """
        response = self.get(f"/latest/dex/search", params={"q": query})
        return response.get("pairs", [])

    def search_tokens_by_chain(
        self,
        ticker: str,
        chain: str,
    ) -> list[dict[str, Any]]:
        """
        Search for tokens by ticker on a specific chain.

        Args:
            ticker: Token ticker symbol (e.g., "BONK")
            chain: Chain identifier (solana, base, bsc)

        Returns:
            List of token pairs matching the ticker on that chain
        """
        pairs = self.search_tokens(ticker)
        return [p for p in pairs if p.get("chainId") == chain]

    def search_tokens_multi_chain(
        self,
        ticker: str,
        chains: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search for tokens by ticker across multiple chains.

        Args:
            ticker: Token ticker symbol (e.g., "BONK")
            chains: List of chain identifiers (defaults to all supported)

        Returns:
            List of token pairs matching the ticker on any of the chains
        """
        if chains is None:
            chains = SUPPORTED_CHAINS

        pairs = self.search_tokens(ticker)
        return [p for p in pairs if p.get("chainId") in chains]

    def search_solana_tokens(self, ticker: str) -> list[dict[str, Any]]:
        """
        Search for Solana tokens by ticker symbol.

        Args:
            ticker: Token ticker symbol (e.g., "BONK")

        Returns:
            List of Solana token pairs matching the ticker
        """
        return self.search_tokens_by_chain(ticker, "solana")

    def get_token_pairs(self, mint_address: str) -> list[dict[str, Any]]:
        """
        Get all trading pairs for a specific Solana token.

        Args:
            mint_address: Token mint address

        Returns:
            List of trading pairs for the token
        """
        response = self.get(f"/latest/dex/tokens/{mint_address}")
        return response.get("pairs", [])

    def get_pair_info(
        self,
        pair_address: str,
        chain: str = "solana",
    ) -> dict[str, Any] | None:
        """
        Get detailed info for a specific trading pair.

        Args:
            pair_address: Liquidity pool/pair address
            chain: Chain identifier (solana, base, bsc)

        Returns:
            Pair information dict or None if not found
        """
        response = self.get(f"/latest/dex/pairs/{chain}/{pair_address}")
        pairs = response.get("pairs", [])
        return pairs[0] if pairs else None

    def get_token_by_address(self, mint_address: str) -> dict[str, Any] | None:
        """
        Get token info by mint address.

        Args:
            mint_address: Token mint address

        Returns:
            Token info from the most liquid pair, or None
        """
        pairs = self.get_token_pairs(mint_address)
        if not pairs:
            return None

        # Return info from the most liquid pair
        pairs_sorted = sorted(
            pairs,
            key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
            reverse=True
        )
        return pairs_sorted[0] if pairs_sorted else None

    def extract_token_info(self, pair_data: dict[str, Any]) -> dict[str, Any]:
        """
        Extract standardized token info from pair data.

        Args:
            pair_data: Raw pair data from DexScreener

        Returns:
            Standardized token info dict
        """
        base_token = pair_data.get("baseToken", {})
        return {
            "mint_address": base_token.get("address"),
            "symbol": base_token.get("symbol"),
            "name": base_token.get("name"),
            "price_usd": float(pair_data.get("priceUsd", 0) or 0),
            "market_cap": float(pair_data.get("marketCap", 0) or 0),
            "fdv": float(pair_data.get("fdv", 0) or 0),
            "liquidity_usd": float(pair_data.get("liquidity", {}).get("usd", 0) or 0),
            "volume_24h": float(pair_data.get("volume", {}).get("h24", 0) or 0),
            "pair_address": pair_data.get("pairAddress"),
            "dex_id": pair_data.get("dexId"),
            "pair_created_at": pair_data.get("pairCreatedAt"),
        }

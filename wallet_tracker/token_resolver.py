"""Token resolution - convert ticker symbols to mint addresses."""

from .api.dexscreener import DexScreenerClient, SUPPORTED_CHAINS
from .api.solana_rpc import SolanaRPCClient
from .models import TokenInfo


class TokenResolver:
    """
    Resolves token ticker symbols to mint addresses.

    Uses DexScreener for lookup and handles disambiguation
    when multiple tokens share the same ticker.
    """

    def __init__(self, rpc_url: str | None = None):
        self.dex_client = DexScreenerClient()
        self.rpc_client = SolanaRPCClient(rpc_url)

    def search_by_ticker(
        self,
        ticker: str,
        chain: str | None = None,
    ) -> list[TokenInfo]:
        """
        Search for tokens by ticker symbol.

        Args:
            ticker: Token ticker symbol (e.g., "BONK")
            chain: Optional chain filter (solana, base, bsc). If None, searches all.

        Returns:
            List of matching TokenInfo objects, sorted by liquidity
        """
        if chain:
            pairs = self.dex_client.search_tokens_by_chain(ticker.upper(), chain)
        else:
            pairs = self.dex_client.search_tokens_multi_chain(ticker.upper())

        if not pairs:
            return []

        # Group by (chain, mint_address) - multiple pairs can exist for same token
        tokens_by_key: dict[tuple[str, str], TokenInfo] = {}

        for pair in pairs:
            base_token = pair.get("baseToken", {})
            mint = base_token.get("address")
            pair_chain = pair.get("chainId", "solana")

            if not mint:
                continue

            # Filter to exact ticker matches
            symbol = base_token.get("symbol", "").upper()
            if symbol != ticker.upper():
                continue

            key = (pair_chain, mint)
            if key not in tokens_by_key:
                token = TokenInfo.from_dexscreener(pair)
                tokens_by_key[key] = token
            else:
                # Update with higher liquidity data if found
                existing = tokens_by_key[key]
                new_liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                if new_liquidity > existing.liquidity_usd:
                    tokens_by_key[key] = TokenInfo.from_dexscreener(pair)

        # Sort by liquidity (most liquid first)
        tokens = list(tokens_by_key.values())
        tokens.sort(key=lambda t: t.liquidity_usd, reverse=True)

        return tokens

    def search_solana_tokens(self, ticker: str) -> list[TokenInfo]:
        """Search for tokens on Solana only (backward compatibility)."""
        return self.search_by_ticker(ticker, chain="solana")

    def get_by_mint_address(
        self,
        mint_address: str,
        chain: str | None = None,
    ) -> TokenInfo | None:
        """
        Get token info by mint/contract address.

        Args:
            mint_address: Token mint/contract address
            chain: Optional chain hint. If None, will use address from DexScreener response.

        Returns:
            TokenInfo or None if not found
        """
        pair_data = self.dex_client.get_token_by_address(mint_address)
        if not pair_data:
            return None

        token = TokenInfo.from_dexscreener(pair_data)

        # Override chain if specified
        if chain:
            token.chain = chain

        # Fetch supply from RPC (only for Solana)
        if token.chain == "solana":
            try:
                supply = self.rpc_client.get_token_supply_ui(mint_address)
                token.supply = supply
            except Exception:
                pass

        return token

    def disambiguate_by_market_cap(
        self,
        candidates: list[TokenInfo],
        target_market_cap: float,
        tolerance: float = 0.5,  # 50% tolerance for disambiguation
    ) -> TokenInfo | None:
        """
        Select the token whose market cap is closest to the target.

        Args:
            candidates: List of potential token matches
            target_market_cap: Expected market cap from user
            tolerance: How far off market cap can be (as fraction)

        Returns:
            Best matching token or None
        """
        if not candidates:
            return None

        if len(candidates) == 1:
            return candidates[0]

        best_match: TokenInfo | None = None
        best_diff = float("inf")

        for token in candidates:
            # Use market_cap if available, otherwise fdv
            mcap = token.market_cap or token.fdv
            if mcap <= 0:
                continue

            diff = abs(mcap - target_market_cap) / target_market_cap

            if diff < best_diff:
                best_diff = diff
                best_match = token

        # Only return if within tolerance
        if best_match and best_diff <= tolerance:
            return best_match

        # If no good market cap match, return highest liquidity
        return candidates[0] if candidates else None

    def resolve(
        self,
        ticker: str,
        chain: str | None = None,
        market_cap_hint: float | None = None,
    ) -> TokenInfo | None:
        """
        Resolve a ticker symbol to a single token.

        Args:
            ticker: Token ticker symbol
            chain: Optional chain filter (solana, base, bsc)
            market_cap_hint: Optional market cap to help disambiguation

        Returns:
            TokenInfo for the best match, or None
        """
        candidates = self.search_by_ticker(ticker, chain=chain)

        if not candidates:
            return None

        if len(candidates) == 1:
            token = candidates[0]
        elif market_cap_hint:
            token = self.disambiguate_by_market_cap(candidates, market_cap_hint)
        else:
            # Default to highest liquidity
            token = candidates[0]

        if token and token.chain == "solana":
            # Fetch supply (only for Solana)
            try:
                supply = self.rpc_client.get_token_supply_ui(token.mint_address)
                token.supply = supply
            except Exception:
                pass

        return token

    def close(self) -> None:
        """Clean up resources."""
        self.dex_client.close()

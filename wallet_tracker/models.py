"""Data models for the wallet tracker."""

from dataclasses import dataclass, field
from typing import Any


# Supported chains
SUPPORTED_CHAINS = ["solana", "base", "bsc"]
CHAIN_NAMES = {"solana": "Solana", "base": "Base", "bsc": "BNB Chain"}
CHAIN_ICONS = {"solana": "◎", "base": "🔵", "bsc": "🟡"}


@dataclass
class TokenInfo:
    """Information about a token."""
    mint_address: str
    symbol: str
    name: str
    chain: str = "solana"  # solana, base, or bsc
    price_usd: float = 0.0
    market_cap: float = 0.0
    fdv: float = 0.0
    liquidity_usd: float = 0.0
    volume_24h: float = 0.0
    supply: float = 0.0
    decimals: int = 9
    pair_address: str | None = None
    dex_id: str | None = None

    @property
    def chain_name(self) -> str:
        """Human-readable chain name."""
        return CHAIN_NAMES.get(self.chain, self.chain.title())

    @property
    def chain_icon(self) -> str:
        """Chain icon/emoji."""
        return CHAIN_ICONS.get(self.chain, "🔗")

    @classmethod
    def from_dexscreener(cls, data: dict[str, Any]) -> "TokenInfo":
        """Create TokenInfo from DexScreener pair data."""
        base_token = data.get("baseToken", {})
        chain_id = data.get("chainId", "solana")
        # Default decimals based on chain (EVM uses 18, Solana uses 9)
        default_decimals = 18 if chain_id in ("base", "bsc") else 9
        return cls(
            mint_address=base_token.get("address", ""),
            symbol=base_token.get("symbol", ""),
            name=base_token.get("name", ""),
            chain=chain_id,
            price_usd=float(data.get("priceUsd", 0) or 0),
            market_cap=float(data.get("marketCap", 0) or 0),
            fdv=float(data.get("fdv", 0) or 0),
            liquidity_usd=float(data.get("liquidity", {}).get("usd", 0) or 0),
            volume_24h=float(data.get("volume", {}).get("h24", 0) or 0),
            pair_address=data.get("pairAddress"),
            dex_id=data.get("dexId"),
            decimals=default_decimals,
        )


@dataclass
class HolderEntry:
    """A single token holder from Helius getTokenAccounts."""
    owner: str              # Wallet address
    token_account: str      # Token account address
    amount: int             # Raw amount (before decimals)
    ui_amount: float        # Human-readable amount (after decimals)

    @classmethod
    def from_helius(cls, data: dict[str, Any], decimals: int = 9) -> "HolderEntry":
        raw_amount = int(data.get("amount", 0))
        return cls(
            owner=data.get("owner", ""),
            token_account=data.get("address", ""),
            amount=raw_amount,
            ui_amount=raw_amount / (10 ** decimals),
        )


@dataclass
class HoldingQuery:
    """User-provided holding to search for."""
    ticker: str
    token_amount: float     # Exact token amount held
    chain: str = "solana"   # solana, base, or bsc

    # Resolved after token lookup
    mint_address: str | None = None
    decimals: int = 9


@dataclass
class WalletMatch:
    """A wallet that matches one or more holding queries."""
    address: str
    holdings: dict[str, float] = field(default_factory=dict)  # mint -> amount

    def add_holding(self, mint: str, amount: float) -> None:
        self.holdings[mint] = amount


@dataclass
class SearchResult:
    """Result of a single token holder search."""
    query: HoldingQuery
    token_info: TokenInfo | None
    candidates: list[WalletMatch]
    total_holders_scanned: int = 0
    search_time_ms: int = 0

    @property
    def found(self) -> bool:
        return len(self.candidates) > 0

    @property
    def unique_match(self) -> bool:
        return len(self.candidates) == 1


@dataclass
class WalletPositionHit:
    """A single qualifying trade from a wallet's position history."""
    token_symbol: str
    token_address: str
    invested_usd: float
    realized_pnl_usd: float
    realized_return_pct: float


@dataclass
class ConsistencyProfile:
    """Results of scanning a wallet's trade history for consistency."""
    wallet_address: str
    qualifying_hits: list[WalletPositionHit] = field(default_factory=list)
    total_positions_scanned: int = 0
    scan_error: str | None = None

    @property
    def is_consistent(self) -> bool:
        """Whether this wallet has at least 2 qualifying hits (besides the original token)."""
        return len(self.qualifying_hits) >= 2

    @property
    def hit_count(self) -> int:
        return len(self.qualifying_hits)


@dataclass
class WalletTopHolding:
    """A top token holding in a wallet's portfolio."""
    symbol: str
    name: str
    balance: float
    usd_value: float


@dataclass
class WalletProfile:
    """Enriched wallet profile from Helius Wallet API (Solana only)."""
    # Identity
    identity_name: str | None = None
    identity_type: str | None = None  # exchange, protocol, etc.

    # Funding source
    funded_by: str | None = None
    funder_name: str | None = None
    funder_type: str | None = None  # exchange, DeFi, etc.
    funded_amount_sol: float = 0.0
    funded_date: str | None = None

    # Portfolio snapshot
    total_usd_value: float = 0.0
    top_holdings: list[WalletTopHolding] = field(default_factory=list)


@dataclass
class TopTrader:
    """A top trader for a token with PnL data."""
    address: str
    total_usd_invested: float = 0.0
    total_sold_usd: float = 0.0
    realized_profit_usd: float = 0.0
    realized_profit_pct: float = 0.0
    unrealized_pnl_usd: float = 0.0
    unrealized_pnl_pct: float = 0.0
    avg_buy_price_usd: float = 0.0
    avg_sell_price_usd: float = 0.0
    total_tokens_bought: float = 0.0
    total_tokens_sold: float = 0.0
    count_of_trades: int = 0
    still_holding: bool = False
    consistency: ConsistencyProfile | None = None
    profile: WalletProfile | None = None

    @property
    def has_cost_basis(self) -> bool:
        """
        Whether this wallet actually bought tokens (has a real cost basis).
        Wallets with $0 invested likely received tokens via transfer, not a trade.
        """
        return self.total_usd_invested > 0

    @property
    def is_early_small_entry(self) -> bool:
        """
        Early entry with small notional (<$500) and massive return (>1000%).
        Excludes wallets with no cost basis (transfer recipients).
        """
        return (
            self.has_cost_basis
            and self.total_usd_invested < 500
            and self.realized_profit_pct >= 1000
        )

    @property
    def is_large_upnl(self) -> bool:
        """
        Wallet still holding with large unrealized PnL (>2000%).
        """
        return (
            self.has_cost_basis
            and self.still_holding
            and self.unrealized_pnl_pct >= 2000
        )

    @property
    def is_whale_trader(self) -> bool:
        """
        Whale entry (>$9000 invested) with strong return (300%-400%).
        """
        return (
            self.total_usd_invested >= 9000
            and 300 <= self.realized_profit_pct <= 400
        )

    @classmethod
    def from_moralis(cls, data: dict[str, Any]) -> "TopTrader":
        """Create TopTrader from Moralis top-gainers response."""
        tokens_bought = float(data.get("total_tokens_bought", 0) or 0)
        tokens_sold = float(data.get("total_tokens_sold", 0) or 0)
        return cls(
            address=data.get("address", ""),
            total_usd_invested=float(data.get("total_usd_invested", 0) or 0),
            total_sold_usd=float(data.get("total_sold_usd", 0) or 0),
            realized_profit_usd=float(data.get("realized_profit_usd", 0) or 0),
            realized_profit_pct=float(data.get("realized_profit_percentage", 0) or 0),
            avg_buy_price_usd=float(data.get("avg_buy_price_usd", 0) or 0),
            avg_sell_price_usd=float(data.get("avg_sell_price_usd", 0) or 0),
            total_tokens_bought=tokens_bought,
            total_tokens_sold=tokens_sold,
            count_of_trades=int(data.get("count_of_trades", 0) or 0),
            still_holding=tokens_bought > tokens_sold,
        )

    @classmethod
    def from_mobula(cls, data: dict[str, Any]) -> "TopTrader":
        """Create TopTrader from Mobula trader-positions response."""
        buys = int(data.get("buys", 0) or 0)
        sells = int(data.get("sells", 0) or 0)
        volume_buy = float(data.get("volumeBuyUSD", 0) or 0)
        volume_sell = float(data.get("volumeSellUSD", 0) or 0)
        realized_pnl = float(data.get("realizedPnlUSD", 0) or 0)
        realized_pct = (realized_pnl / volume_buy * 100) if volume_buy > 0 else 0
        unrealized_pnl = float(data.get("unrealizedPnlUSD", 0) or 0)
        unrealized_pct = (unrealized_pnl / volume_buy * 100) if volume_buy > 0 else 0
        token_amount = float(data.get("tokenAmount", 0) or 0)
        return cls(
            address=data.get("walletAddress", ""),
            total_usd_invested=volume_buy,
            total_sold_usd=volume_sell,
            realized_profit_usd=realized_pnl,
            realized_profit_pct=realized_pct,
            unrealized_pnl_usd=unrealized_pnl,
            unrealized_pnl_pct=unrealized_pct,
            avg_buy_price_usd=float(data.get("avgBuyPriceUSD", 0) or 0),
            avg_sell_price_usd=float(data.get("avgSellPriceUSD", 0) or 0),
            total_tokens_bought=float(data.get("volumeBuyToken", 0) or 0),
            total_tokens_sold=float(data.get("volumeSellToken", 0) or 0),
            count_of_trades=buys + sells,
            still_holding=token_amount > 0,
        )

    @classmethod
    def from_helius_swaps(
        cls,
        wallet_address: str,
        swaps: list[dict[str, Any]],
        token_mint: str,
        current_balance: float = 0.0,
        token_price_usd: float = 0.0,
    ) -> "TopTrader":
        """
        Build a TopTrader from Helius enhanced swap transactions.

        Analyzes swap history to determine buys (token in outputs)
        and sells (token in inputs) of the target token.
        Uses native SOL amounts and a rough SOL/USD price to estimate USD values.
        """
        SOL_DECIMALS = 9
        total_tokens_bought = 0.0
        total_tokens_sold = 0.0
        total_sol_spent = 0.0    # SOL spent buying the token
        total_sol_received = 0.0  # SOL received selling the token
        trade_count = 0

        for tx in swaps:
            swap_event = tx.get("events", {}).get("swap")
            if not swap_event:
                continue

            token_inputs = swap_event.get("tokenInputs", [])
            token_outputs = swap_event.get("tokenOutputs", [])
            native_input = swap_event.get("nativeInput") or {}
            native_output = swap_event.get("nativeOutput") or {}

            # Check if the target token appears in outputs (= BUY)
            for tok_out in token_outputs:
                if tok_out.get("mint") == token_mint:
                    raw = tok_out.get("rawTokenAmount", {})
                    amount = int(raw.get("tokenAmount", "0"))
                    decimals = int(raw.get("decimals", 9))
                    total_tokens_bought += amount / (10 ** decimals)
                    # SOL spent = nativeInput
                    sol_in = int(native_input.get("amount", "0"))
                    total_sol_spent += sol_in / (10 ** SOL_DECIMALS)
                    trade_count += 1

            # Check if the target token appears in inputs (= SELL)
            for tok_in in token_inputs:
                if tok_in.get("mint") == token_mint:
                    raw = tok_in.get("rawTokenAmount", {})
                    amount = int(raw.get("tokenAmount", "0"))
                    decimals = int(raw.get("decimals", 9))
                    total_tokens_sold += amount / (10 ** decimals)
                    # SOL received = nativeOutput
                    sol_out = int(native_output.get("amount", "0"))
                    total_sol_received += sol_out / (10 ** SOL_DECIMALS)
                    trade_count += 1

        # Estimate USD values using current token price as a rough proxy
        # For buys: invested = SOL spent (use token price * tokens as fallback)
        total_usd_invested = total_tokens_bought * token_price_usd if token_price_usd else 0
        total_sold_usd = total_tokens_sold * token_price_usd if token_price_usd else 0

        # Better estimate: if we have SOL amounts, use SOL price (~$150 rough estimate)
        # We'll use the ratio of SOL in/out as a more accurate measure
        if total_sol_spent > 0 and total_tokens_bought > 0:
            avg_buy_price = total_sol_spent / total_tokens_bought
        else:
            avg_buy_price = 0

        if total_sol_received > 0 and total_tokens_sold > 0:
            avg_sell_price = total_sol_received / total_tokens_sold
        else:
            avg_sell_price = 0

        # Realized profit in SOL terms
        realized_sol = total_sol_received - total_sol_spent
        realized_pct = (realized_sol / total_sol_spent * 100) if total_sol_spent > 0 else 0

        still_holding = current_balance > 0

        return cls(
            address=wallet_address,
            total_usd_invested=total_usd_invested,
            total_sold_usd=total_sold_usd,
            realized_profit_usd=total_sold_usd - total_usd_invested,
            realized_profit_pct=realized_pct,
            avg_buy_price_usd=avg_buy_price,
            avg_sell_price_usd=avg_sell_price,
            total_tokens_bought=total_tokens_bought,
            total_tokens_sold=total_tokens_sold,
            count_of_trades=trade_count,
            still_holding=still_holding,
        )


@dataclass
class TopTradersResult:
    """Result of a top traders query."""
    token_info: TokenInfo | None
    traders: list[TopTrader]
    early_entries: list[TopTrader]     # Small investment, >1000% return
    large_upnl: list[TopTrader]       # Still holding, >2000% unrealized PnL
    whale_traders: list[TopTrader]     # >$9000 invested, 300-400% return
    search_time_ms: int = 0


@dataclass
class VerificationResult:
    """Result of wallet verification using two holdings."""
    primary_query: HoldingQuery
    verification_query: HoldingQuery
    confirmed_wallets: list[str]
    primary_candidates: list[WalletMatch]
    verification_candidates: list[WalletMatch]

    @property
    def verified(self) -> bool:
        return len(self.confirmed_wallets) == 1

    @property
    def wallet(self) -> str | None:
        return self.confirmed_wallets[0] if self.verified else None

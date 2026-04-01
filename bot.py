"""Discord bot for the Multi-Chain Wallet Tracker (Solana, Base, BNB)."""

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent / ".env")

from wallet_tracker.api.fomo import FomoClient, FOMO_NETWORK_IDS, FOMO_NETWORK_IDS_REVERSE
from wallet_tracker.api.helius import HeliusClient
from wallet_tracker.api.mobula import MobulaClient
from wallet_tracker.config import Config
from wallet_tracker.matcher import WalletMatcher
from wallet_tracker.models import (
    ConsistencyProfile,
    HoldingQuery,
    SearchResult,
    TokenInfo,
    TopTrader,
    TopTradersResult,
    VerificationResult,
    WalletPositionHit,
    WalletProfile,
    WalletTopHolding,
    CHAIN_NAMES,
    CHAIN_ICONS,
)
from wallet_tracker.token_resolver import TokenResolver

logger = logging.getLogger("wallet_tracker_bot")

# Address format detection
SOLANA_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$", re.IGNORECASE)

# Keep old name for backward compatibility
MINT_RE = SOLANA_MINT_RE


def detect_chain_from_address(address: str) -> str | None:
    """
    Detect chain type from address format.

    Returns:
        'solana' for Solana addresses
        'evm' for EVM addresses (Base/BNB - needs further disambiguation)
        None if unknown format
    """
    if SOLANA_MINT_RE.match(address):
        return "solana"
    if EVM_ADDRESS_RE.match(address):
        return "evm"  # Could be Base or BNB
    return None


def get_chain_display(chain: str) -> str:
    """Get display string for a chain (icon + name)."""
    icon = CHAIN_ICONS.get(chain, "")
    name = CHAIN_NAMES.get(chain, chain.upper())
    return f"{icon} {name}"


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def build_search_embed(result: SearchResult) -> discord.Embed:
    """Build a Discord embed from a SearchResult."""
    if not result.token_info:
        return discord.Embed(
            title="Token Not Found",
            description=f"Could not resolve token: `{result.query.ticker}`",
            color=discord.Color.red(),
        )

    token = result.token_info
    chain_display = get_chain_display(token.chain)

    if result.unique_match:
        color = discord.Color.green()
        title = "Wallet Found"
    elif result.found:
        color = discord.Color.gold()
        title = f"Found {len(result.candidates)} Candidate(s)"
    else:
        color = discord.Color.red()
        title = "No Matches"

    embed = discord.Embed(title=title, color=color)

    embed.add_field(name="Token", value=f"**{token.symbol}** — {token.name}", inline=True)
    embed.add_field(name="Chain", value=chain_display, inline=True)
    embed.add_field(name="Price", value=f"${token.price_usd:.10f}", inline=True)
    embed.add_field(
        name="Market Cap / Liquidity",
        value=f"${token.market_cap:,.0f} / ${token.liquidity_usd:,.0f}",
        inline=True,
    )
    # Use appropriate label based on chain
    address_label = "Mint" if token.chain == "solana" else "Contract"
    embed.add_field(name=address_label, value=f"`{token.mint_address}`", inline=False)

    embed.add_field(
        name="Search",
        value=f"Scanned **{result.total_holders_scanned:,}** holders in **{result.search_time_ms / 1000:.1f}s**",
        inline=False,
    )

    if result.unique_match:
        wallet = result.candidates[0]
        embed.add_field(
            name="Wallet",
            value=f"```\n{wallet.address}\n```",
            inline=False,
        )
        amt = list(wallet.holdings.values())[0] if wallet.holdings else 0
        embed.add_field(name="Balance", value=f"`{amt:,.6f}`", inline=True)
    elif result.found:
        lines = []
        for i, m in enumerate(result.candidates[:10], 1):
            amt = list(m.holdings.values())[0] if m.holdings else 0
            lines.append(f"{i}. {m.address}  ({amt:,.2f})")
        wallet_list = "\n".join(lines)
        if len(result.candidates) > 10:
            wallet_list += f"\n... and {len(result.candidates) - 10} more"
        embed.add_field(
            name="Candidates",
            value=f"```\n{wallet_list}\n```",
            inline=False,
        )
        embed.set_footer(text="Use /verify with a second token to narrow results.")
    else:
        embed.add_field(
            name="Result",
            value="No wallets matched the specified token amount.\nTry adjusting the amount or check the ticker.",
            inline=False,
        )

    return embed


def _format_profile_line(t: TopTrader) -> str:
    """Format wallet profile info (Helius enrichment) as a sub-line."""
    if not t.profile:
        return ""
    parts = []
    if t.profile.identity_name:
        parts.append(f"ID: **{t.profile.identity_name}** ({t.profile.identity_type or 'unknown'})")
    if t.profile.funder_name:
        parts.append(f"Funded by: **{t.profile.funder_name}**")
    elif t.profile.funded_by:
        parts.append(f"Funded by: `{t.profile.funded_by[:12]}...`")
    if t.profile.total_usd_value > 0:
        parts.append(f"Portfolio: **${t.profile.total_usd_value:,.0f}**")
    if not parts:
        return ""
    return "\n   " + " | ".join(parts)


def _format_trader_line(i: int, t: TopTrader) -> str:
    """Format a single trader line for embeds."""
    held = " (holding)" if t.still_holding else ""
    badge = ""
    if t.consistency and t.consistency.is_consistent:
        badge = f" | CONSISTENT ({t.consistency.hit_count} hits)"
    elif t.consistency and t.consistency.hit_count == 1:
        badge = " | 1 other hit"
    profile = _format_profile_line(t)
    return (
        f"{i}. `{t.address}`"
        f"\n   In: **${t.total_usd_invested:,.0f}** | Out: **${t.total_sold_usd:,.0f}**"
        f" | PnL: **{t.realized_profit_pct:,.0f}%** (${t.realized_profit_usd:,.0f})"
        f"{held}{badge}{profile}"
    )


def _format_upnl_line(i: int, t: TopTrader) -> str:
    """Format a trader line showing unrealized PnL."""
    return (
        f"{i}. `{t.address}`"
        f"\n   In: **${t.total_usd_invested:,.0f}**"
        f" | uPnL: **{t.unrealized_pnl_pct:,.0f}%** (${t.unrealized_pnl_usd:,.0f})"
        f" | rPnL: **{t.realized_profit_pct:,.0f}%** (${t.realized_profit_usd:,.0f})"
    )


def build_top_traders_embeds(result: TopTradersResult) -> list[discord.Embed]:
    """Build Discord embeds from a TopTradersResult. Returns a list of embeds."""
    embeds: list[discord.Embed] = []

    if not result.token_info:
        embeds.append(discord.Embed(
            title="Token Not Found",
            color=discord.Color.red(),
        ))
        return embeds

    token = result.token_info
    chain_display = get_chain_display(token.chain)

    # --- Main summary embed ---
    summary = discord.Embed(
        title=f"Top Traders — {token.symbol}",
        description=(
            f"{chain_display} | **{token.name}**\n"
            f"Price: ${token.price_usd:.10f} | MCap: ${token.market_cap:,.0f}\n"
            f"Analyzed **{len(result.traders)}** top traders in **{result.search_time_ms / 1000:.1f}s**"
        ),
        color=discord.Color.blue(),
    )
    embeds.append(summary)

    # --- Top 10 by PnL embed ---
    if result.traders:
        top_by_pnl = sorted(result.traders, key=lambda t: t.realized_profit_usd, reverse=True)[:10]
        lines = [_format_trader_line(i, t) for i, t in enumerate(top_by_pnl, 1)]
        top_embed = discord.Embed(
            title="Top 10 by Realized Profit",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        embeds.append(top_embed)

    # --- Early entries: small investment, >1000% return ---
    if result.early_entries:
        lines = [_format_trader_line(i, t) for i, t in enumerate(result.early_entries[:10], 1)]
        early_embed = discord.Embed(
            title=f"Early Small Entries — >1000% Return ({len(result.early_entries)} found)",
            description=(
                "Wallets that entered with a small dollar amount and achieved >1000% return.\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.purple(),
        )
        if len(result.early_entries) > 10:
            early_embed.set_footer(text=f"... and {len(result.early_entries) - 10} more")
        embeds.append(early_embed)
    else:
        embeds.append(discord.Embed(
            title="Early Small Entries — >1000% Return",
            description="No wallets found matching this criteria.",
            color=discord.Color.light_grey(),
        ))

    # --- Consistent Early Entries detail ---
    consistent_early = [t for t in result.early_entries[:10]
                        if t.consistency and t.consistency.is_consistent]
    if consistent_early:
        lines = []
        for i, t in enumerate(consistent_early[:5], 1):
            hits_str = ", ".join(
                f"{h.token_symbol} (${h.invested_usd:,.0f} -> {h.realized_return_pct:,.0f}%)"
                for h in t.consistency.qualifying_hits[:3]
            )
            line = f"{i}. `{t.address}`\n   Past gems: {hits_str}"
            line += _format_profile_line(t)
            lines.append(line)
        embeds.append(discord.Embed(
            title=f"Consistent Early Entries ({len(consistent_early)} found)",
            description=(
                "Early entries with 2+ other early gems (<$500 in, 500%+ return):\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.dark_purple(),
        ))

    # --- Large unrealized PnL: still holding, >2000% uPnL ---
    if result.large_upnl:
        lines = [_format_upnl_line(i, t) for i, t in enumerate(result.large_upnl[:10], 1)]
        upnl_embed = discord.Embed(
            title=f"Large Unrealized PnL — >2000% uPnL ({len(result.large_upnl)} found)",
            description=(
                "Wallets still holding with >2000% unrealized profit.\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.teal(),
        )
        if len(result.large_upnl) > 10:
            upnl_embed.set_footer(text=f"... and {len(result.large_upnl) - 10} more")
        embeds.append(upnl_embed)
    else:
        embeds.append(discord.Embed(
            title="Large Unrealized PnL — >2000% uPnL",
            description="No wallets found matching this criteria.",
            color=discord.Color.light_grey(),
        ))

    # --- Whale traders: >$9000 invested, 300-400% return ---
    if result.whale_traders:
        lines = [_format_trader_line(i, t) for i, t in enumerate(result.whale_traders[:10], 1)]
        whale_embed = discord.Embed(
            title=f"Whale Traders — $9K+ In, 300-400% Return ({len(result.whale_traders)} found)",
            description=(
                "Wallets that entered with >$9,000 and sold for a 300%-400% return.\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.gold(),
        )
        if len(result.whale_traders) > 10:
            whale_embed.set_footer(text=f"... and {len(result.whale_traders) - 10} more")
        embeds.append(whale_embed)
    else:
        embeds.append(discord.Embed(
            title="Whale Traders — $9K+ In, 300-400% Return",
            description="No wallets found matching this criteria.",
            color=discord.Color.light_grey(),
        ))

    # --- Consistent Whales detail ---
    consistent_whales = [t for t in result.whale_traders[:10]
                         if t.consistency and t.consistency.is_consistent]
    if consistent_whales:
        lines = []
        for i, t in enumerate(consistent_whales[:5], 1):
            hits_str = ", ".join(
                f"{h.token_symbol} (${h.invested_usd:,.0f} -> {h.realized_return_pct:,.0f}%)"
                for h in t.consistency.qualifying_hits[:3]
            )
            line = f"{i}. `{t.address}`\n   Past wins: {hits_str}"
            line += _format_profile_line(t)
            lines.append(line)
        embeds.append(discord.Embed(
            title=f"Consistent Whales ({len(consistent_whales)} found)",
            description=(
                "Whales with 2+ other big wins ($5K+ in, 300%+ return):\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.dark_gold(),
        ))

    return embeds


def build_verification_embed(result: VerificationResult) -> discord.Embed:
    """Build a Discord embed from a VerificationResult."""
    if result.verified:
        embed = discord.Embed(title="Wallet Confirmed", color=discord.Color.green())
        embed.add_field(
            name="Wallet",
            value=f"```\n{result.wallet}\n```",
            inline=False,
        )
        embed.add_field(
            name="Method",
            value="This wallet holds both specified token amounts.",
            inline=False,
        )
    elif result.confirmed_wallets:
        embed = discord.Embed(
            title=f"Multiple Matches ({len(result.confirmed_wallets)})",
            color=discord.Color.gold(),
        )
        lines = "\n".join(result.confirmed_wallets[:10])
        if len(result.confirmed_wallets) > 10:
            lines += f"\n... and {len(result.confirmed_wallets) - 10} more"
        embed.add_field(name="Wallets", value=f"```\n{lines}\n```", inline=False)
    else:
        embed = discord.Embed(title="Verification Failed", color=discord.Color.red())
        embed.add_field(
            name="Result",
            value="No wallet found holding both specified token amounts.",
            inline=False,
        )

    embed.add_field(
        name="Primary",
        value=f"`{result.primary_query.ticker}` — {result.primary_query.token_amount:,.6f} tokens ({len(result.primary_candidates)} candidates)",
        inline=True,
    )
    embed.add_field(
        name="Verification",
        value=f"`{result.verification_query.ticker}` — {result.verification_query.token_amount:,.6f} tokens ({len(result.verification_candidates)} candidates)",
        inline=True,
    )

    return embed


# ---------------------------------------------------------------------------
# Token disambiguation view (Select dropdown)
# ---------------------------------------------------------------------------

class TokenSelectView(discord.ui.View):
    """Dropdown for selecting from multiple token matches."""

    def __init__(
        self,
        candidates: list[TokenInfo],
        amount: float,
        config: Config,
        *,
        original_interaction: discord.Interaction,
    ):
        super().__init__(timeout=60.0)
        self.amount = amount
        self.config = config
        self.candidates = candidates
        self.original_interaction = original_interaction

        options = []
        for i, token in enumerate(candidates[:25]):
            chain_icon = CHAIN_ICONS.get(token.chain, "")
            mcap = f"MCap: ${token.market_cap:,.0f}" if token.market_cap else "MCap: N/A"
            liq = f"Liq: ${token.liquidity_usd:,.0f}" if token.liquidity_usd else "Liq: N/A"
            options.append(
                discord.SelectOption(
                    label=f"{chain_icon} {token.symbol} — {token.name[:40]}",
                    description=f"{mcap} | {liq} | {token.mint_address[:20]}...",
                    value=str(i),
                )
            )

        select = discord.ui.Select(
            placeholder="Select the correct token...",
            options=options,
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()

        idx = int(interaction.data["values"][0])
        selected = self.candidates[idx]

        query = HoldingQuery(
            ticker=selected.symbol,
            token_amount=self.amount,
            chain=selected.chain,
            mint_address=selected.mint_address,
        )

        matcher = WalletMatcher(self.config)
        try:
            result = await asyncio.to_thread(matcher.find_candidates, query)
        finally:
            matcher.close()

        embed = build_search_embed(result)
        await interaction.followup.send(embed=embed)
        self.stop()

    async def on_timeout(self):
        # Disable the dropdown after timeout
        for child in self.children:
            child.disabled = True
        try:
            await self.original_interaction.edit_original_response(view=self)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

class WalletTrackerBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.config = Config.load()

    async def setup_hook(self):
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands synced to guild %s", guild_id)
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally (may take up to 1 hour to propagate)")

    async def on_ready(self):
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)


bot = WalletTrackerBot()


# ---------------------------------------------------------------------------
# Helper: resolve token input to HoldingQuery
# ---------------------------------------------------------------------------

async def _resolve_query(token: str, amount: float) -> HoldingQuery | None:
    """Resolve a token string to a HoldingQuery. Auto-picks highest liquidity."""
    token = token.strip()
    address_type = detect_chain_from_address(token)

    if address_type == "solana":
        return HoldingQuery(
            ticker=token[:8] + "...",
            token_amount=amount,
            chain="solana",
            mint_address=token,
        )

    if address_type == "evm":
        # EVM address - look up to determine chain
        resolver = TokenResolver()
        try:
            token_info = await asyncio.to_thread(resolver.get_by_mint_address, token)
        finally:
            resolver.close()

        if not token_info:
            return None

        return HoldingQuery(
            ticker=token_info.symbol,
            token_amount=amount,
            chain=token_info.chain,
            mint_address=token,
        )

    # Ticker search - search all chains
    resolver = TokenResolver()
    try:
        candidates = await asyncio.to_thread(resolver.search_by_ticker, token.upper())
    finally:
        resolver.close()

    if not candidates:
        return None

    best = candidates[0]  # Sorted by liquidity (highest first)
    return HoldingQuery(
        ticker=best.symbol,
        token_amount=amount,
        chain=best.chain,
        mint_address=best.mint_address,
    )


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="find", description="Find wallets holding a specific amount of a token")
@app_commands.describe(
    token="Token ticker symbol (e.g. BONK) or contract address",
    amount="Exact token amount held",
)
async def cmd_find(interaction: discord.Interaction, token: str, amount: float):
    await interaction.response.defer()

    try:
        token_input = token.strip()
        address_type = detect_chain_from_address(token_input)

        if address_type == "solana":
            # Solana mint address
            query = HoldingQuery(
                ticker=token_input[:8] + "...",
                token_amount=amount,
                chain="solana",
                mint_address=token_input,
            )
            matcher = WalletMatcher(bot.config)
            try:
                result = await asyncio.to_thread(matcher.find_candidates, query)
            finally:
                matcher.close()

            embed = build_search_embed(result)
            await interaction.followup.send(embed=embed)

        elif address_type == "evm":
            # EVM contract address - need to check which chain(s) it exists on
            resolver = TokenResolver()
            try:
                token_info = await asyncio.to_thread(
                    resolver.get_by_mint_address, token_input
                )
            finally:
                resolver.close()

            if not token_info:
                embed = discord.Embed(
                    title="Token Not Found",
                    description=f"Could not find token at address `{token_input}`",
                    color=discord.Color.red(),
                )
                await interaction.followup.send(embed=embed)
                return

            query = HoldingQuery(
                ticker=token_info.symbol,
                token_amount=amount,
                chain=token_info.chain,
                mint_address=token_input,
            )
            matcher = WalletMatcher(bot.config)
            try:
                result = await asyncio.to_thread(matcher.find_candidates, query)
            finally:
                matcher.close()

            embed = build_search_embed(result)
            await interaction.followup.send(embed=embed)

        else:
            # Resolve ticker — search across all chains, may need disambiguation
            resolver = TokenResolver()
            try:
                candidates = await asyncio.to_thread(
                    resolver.search_by_ticker, token_input.upper()
                )
            finally:
                resolver.close()

            if not candidates:
                embed = discord.Embed(
                    title="Token Not Found",
                    description=f"No tokens found for `{token_input.upper()}` on Solana, Base, or BNB",
                    color=discord.Color.red(),
                )
                await interaction.followup.send(embed=embed)
                return

            if len(candidates) == 1:
                selected = candidates[0]
                query = HoldingQuery(
                    ticker=selected.symbol,
                    token_amount=amount,
                    chain=selected.chain,
                    mint_address=selected.mint_address,
                )
                matcher = WalletMatcher(bot.config)
                try:
                    result = await asyncio.to_thread(matcher.find_candidates, query)
                finally:
                    matcher.close()

                embed = build_search_embed(result)
                await interaction.followup.send(embed=embed)
            else:
                # Multiple matches — show dropdown with chain indicators
                view = TokenSelectView(
                    candidates, amount, bot.config,
                    original_interaction=interaction,
                )
                # Count chains
                chains_found = set(c.chain for c in candidates)
                chain_str = ", ".join(CHAIN_NAMES.get(c, c) for c in chains_found)
                embed = discord.Embed(
                    title=f"Multiple tokens found for '{token_input.upper()}'",
                    description=f"Found on: {chain_str}\nSelect the correct token from the dropdown below.",
                    color=discord.Color.gold(),
                )
                await interaction.followup.send(embed=embed, view=view)

    except Exception as e:
        logger.exception("Error in /find")
        embed = discord.Embed(
            title="Error",
            description=f"```\n{str(e)[:3900]}\n```",
            color=discord.Color.red(),
        )
        await interaction.followup.send(embed=embed)


@bot.tree.command(
    name="verify",
    description="Verify a wallet by matching two different token holdings",
)
@app_commands.describe(
    token1="First token ticker or contract address",
    amount1="First token amount held",
    token2="Second token ticker or contract address",
    amount2="Second token amount held",
)
async def cmd_verify(
    interaction: discord.Interaction,
    token1: str,
    amount1: float,
    token2: str,
    amount2: float,
):
    await interaction.response.defer()

    try:
        q1 = await _resolve_query(token1, amount1)
        q2 = await _resolve_query(token2, amount2)

        if q1 is None or q2 is None:
            missing = []
            if q1 is None:
                missing.append(token1)
            if q2 is None:
                missing.append(token2)
            embed = discord.Embed(
                title="Token Not Found",
                description=f"Could not resolve: {', '.join(f'`{t}`' for t in missing)}",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed)
            return

        matcher = WalletMatcher(bot.config)
        try:
            result = await asyncio.to_thread(
                matcher.verify_with_second_holding, q1, q2
            )
        finally:
            matcher.close()

        embed = build_verification_embed(result)
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.exception("Error in /verify")
        embed = discord.Embed(
            title="Error",
            description=f"```\n{str(e)[:3900]}\n```",
            color=discord.Color.red(),
        )
        await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Helper: fetch top traders for a token
# ---------------------------------------------------------------------------

# Map our internal chain IDs to Mobula's blockchain identifiers
_MOBULA_CHAIN_MAP = {
    "solana": "solana",
    "base": "base",
    "bsc": "56",
}


def _scan_wallet_consistency(
    wallet_address: str,
    current_token_address: str,
    mobula_chain: str,
    api_key: str,
    category: str,
) -> ConsistencyProfile:
    """
    Scan a wallet's positions to find qualifying trades for consistency.

    Args:
        wallet_address: The wallet to scan.
        current_token_address: Exclude this token from results.
        mobula_chain: Mobula blockchain identifier.
        api_key: Mobula API key.
        category: "whale" (>=$5K in, >=300% return) or "early" (<=$500 in, >=500% return).
    """
    profile = ConsistencyProfile(wallet_address=wallet_address)

    try:
        mobula = MobulaClient(api_key)
        try:
            positions = mobula.get_wallet_positions(wallet_address, blockchain=mobula_chain)
        finally:
            mobula.close()
    except Exception as e:
        profile.scan_error = str(e)
        return profile

    profile.total_positions_scanned = len(positions)

    for pos in positions:
        token = pos.get("token", {})
        token_address = token.get("address", "")

        # Skip the token we already know about
        if token_address.lower() == current_token_address.lower():
            continue

        volume_buy = float(pos.get("volumeBuy", 0) or 0)
        realized_pnl = float(pos.get("realizedPnlUSD", 0) or 0)

        if volume_buy <= 0:
            continue

        realized_return_pct = (realized_pnl / volume_buy) * 100

        qualifies = False
        if category == "whale" and volume_buy >= 5000 and realized_return_pct >= 300:
            qualifies = True
        elif category == "early" and volume_buy <= 500 and realized_return_pct >= 500:
            qualifies = True

        if qualifies:
            profile.qualifying_hits.append(WalletPositionHit(
                token_symbol=token.get("symbol", "???"),
                token_address=token_address,
                invested_usd=volume_buy,
                realized_pnl_usd=realized_pnl,
                realized_return_pct=realized_return_pct,
            ))

    # Sort hits by return percentage descending
    profile.qualifying_hits.sort(key=lambda h: h.realized_return_pct, reverse=True)
    return profile


def _enrich_wallet_profile(
    wallet_address: str,
    helius_api_key: str,
) -> WalletProfile:
    """
    Enrich a Solana wallet with identity, funding source, and portfolio data.
    Uses Helius Wallet API (identity, funded-by, balances).
    """
    profile = WalletProfile()
    helius = HeliusClient(helius_api_key)

    try:
        # Identity (known exchange, protocol, etc.)
        identity = helius.get_wallet_identity(wallet_address)
        if identity:
            profile.identity_name = identity.get("name")
            profile.identity_type = identity.get("type")

        # Funding source
        funded = helius.get_wallet_funded_by(wallet_address)
        if funded:
            profile.funded_by = funded.get("funder")
            profile.funder_name = funded.get("funderName")
            profile.funder_type = funded.get("funderType")
            profile.funded_amount_sol = float(funded.get("amount", 0) or 0)
            profile.funded_date = funded.get("date")

        # Portfolio balances (top 20 holdings)
        balances = helius.get_wallet_balances(wallet_address, limit=20)
        if balances:
            profile.total_usd_value = float(balances.get("totalUsdValue", 0) or 0)
            for tok in balances.get("balances", [])[:5]:
                usd_val = float(tok.get("usdValue", 0) or 0)
                if usd_val > 0:
                    profile.top_holdings.append(WalletTopHolding(
                        symbol=tok.get("symbol", "???"),
                        name=tok.get("name", ""),
                        balance=float(tok.get("balance", 0) or 0),
                        usd_value=usd_val,
                    ))
    except Exception:
        pass  # Profile enrichment is best-effort
    finally:
        helius.close()

    return profile


def _fetch_top_traders(token_info: TokenInfo, config: Config) -> TopTradersResult:
    """Fetch top traders via Mobula, categorize, scan for consistency, and enrich."""
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    start_time = time.time()

    if not config.mobula_api_key:
        raise ValueError("MOBULA_API_KEY required for /toptraders. Sign up at https://admin.mobula.io")

    mobula_chain = _MOBULA_CHAIN_MAP.get(token_info.chain, token_info.chain)

    mobula = MobulaClient(config.mobula_api_key)
    try:
        raw_traders = mobula.get_all_trader_positions(
            token_info.mint_address,
            blockchain=mobula_chain,
            max_pages=5,
            per_page=100,
        )
    finally:
        mobula.close()

    traders = [TopTrader.from_mobula(t) for t in raw_traders if t.get("walletAddress")]

    # Filter out wallets with no cost basis (received tokens via transfer, not bought)
    traders = [t for t in traders if t.has_cost_basis]

    # Categorize
    early_entries = sorted(
        [t for t in traders if t.is_early_small_entry],
        key=lambda t: t.realized_profit_pct,
        reverse=True,
    )
    large_upnl = sorted(
        [t for t in traders if t.is_large_upnl],
        key=lambda t: t.unrealized_pnl_pct,
        reverse=True,
    )
    whale_traders = sorted(
        [t for t in traders if t.is_whale_trader],
        key=lambda t: t.realized_profit_usd,
        reverse=True,
    )

    # --- Phase 2: Scan wallets for consistency + enrich Solana profiles ---
    wallets_to_scan: list[tuple[TopTrader, str]] = []
    for t in whale_traders[:10]:
        wallets_to_scan.append((t, "whale"))
    for t in early_entries[:10]:
        wallets_to_scan.append((t, "early"))

    if wallets_to_scan:
        with ThreadPoolExecutor(max_workers=5) as executor:
            # Consistency scans (all chains)
            consistency_futures = {}
            for trader, category in wallets_to_scan:
                future = executor.submit(
                    _scan_wallet_consistency,
                    trader.address,
                    token_info.mint_address,
                    mobula_chain,
                    config.mobula_api_key,
                    category,
                )
                consistency_futures[future] = trader

            # Helius wallet enrichment (Solana only)
            profile_futures = {}
            if token_info.chain == "solana" and config.helius_api_key:
                for trader, _ in wallets_to_scan:
                    future = executor.submit(
                        _enrich_wallet_profile,
                        trader.address,
                        config.helius_api_key,
                    )
                    profile_futures[future] = trader

            # Collect consistency results
            for future in as_completed(consistency_futures):
                trader = consistency_futures[future]
                try:
                    trader.consistency = future.result(timeout=15)
                except Exception as e:
                    trader.consistency = ConsistencyProfile(
                        wallet_address=trader.address,
                        scan_error=str(e),
                    )

            # Collect profile results
            for future in as_completed(profile_futures):
                trader = profile_futures[future]
                try:
                    trader.profile = future.result(timeout=15)
                except Exception:
                    pass  # Profile enrichment is best-effort

        # Re-sort: consistent wallets first
        whale_traders.sort(key=lambda t: (
            -(t.consistency.hit_count if t.consistency else 0),
            -t.realized_profit_usd,
        ))
        early_entries.sort(key=lambda t: (
            -(t.consistency.hit_count if t.consistency else 0),
            -t.realized_profit_pct,
        ))

    elapsed = int((time.time() - start_time) * 1000)

    return TopTradersResult(
        token_info=token_info,
        traders=traders,
        early_entries=early_entries,
        large_upnl=large_upnl,
        whale_traders=whale_traders,
        search_time_ms=elapsed,
    )


@bot.tree.command(
    name="toptraders",
    description="Find top traders by PnL for a token (Solana, Base, BNB)",
)
@app_commands.describe(
    token="Token ticker symbol (e.g. BONK, DEGEN) or contract address",
)
async def cmd_toptraders(interaction: discord.Interaction, token: str):
    await interaction.response.defer()

    try:
        token_input = token.strip()
        address_type = detect_chain_from_address(token_input)

        # Resolve token
        token_info: TokenInfo | None = None

        if address_type == "solana":
            resolver = TokenResolver()
            try:
                token_info = await asyncio.to_thread(
                    resolver.get_by_mint_address, token_input, "solana"
                )
            finally:
                resolver.close()

        elif address_type == "evm":
            resolver = TokenResolver()
            try:
                token_info = await asyncio.to_thread(
                    resolver.get_by_mint_address, token_input
                )
            finally:
                resolver.close()

        else:
            # Ticker search — search all chains
            resolver = TokenResolver()
            try:
                candidates = await asyncio.to_thread(
                    resolver.search_by_ticker, token_input.upper()
                )
            finally:
                resolver.close()

            if not candidates:
                embed = discord.Embed(
                    title="Token Not Found",
                    description=f"No tokens found for `{token_input.upper()}` on Solana, Base, or BNB Chain",
                    color=discord.Color.red(),
                )
                await interaction.followup.send(embed=embed)
                return

            if len(candidates) == 1:
                token_info = candidates[0]
            else:
                # Multiple matches — show dropdown
                view = TopTradersSelectView(
                    candidates, bot.config,
                    original_interaction=interaction,
                )
                chains_found = set(c.chain for c in candidates)
                chain_str = ", ".join(CHAIN_NAMES.get(c, c) for c in chains_found)
                embed = discord.Embed(
                    title=f"Multiple tokens found for '{token_input.upper()}'",
                    description=f"Found on: {chain_str}\nSelect the correct token from the dropdown below.",
                    color=discord.Color.gold(),
                )
                await interaction.followup.send(embed=embed, view=view)
                return

        if not token_info:
            embed = discord.Embed(
                title="Token Not Found",
                description=f"Could not find token `{token_input}`",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed)
            return

        # Check that Mobula API key is available
        if not bot.config.mobula_api_key:
            embed = discord.Embed(
                title="Missing API Key",
                description="MOBULA_API_KEY is required for /toptraders. Sign up at https://admin.mobula.io",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed)
            return

        result = await asyncio.to_thread(_fetch_top_traders, token_info, bot.config)
        embeds = build_top_traders_embeds(result)
        await interaction.followup.send(embeds=embeds)

    except Exception as e:
        logger.exception("Error in /toptraders")
        embed = discord.Embed(
            title="Error",
            description=f"```\n{str(e)[:3900]}\n```",
            color=discord.Color.red(),
        )
        await interaction.followup.send(embed=embed)


class TopTradersSelectView(discord.ui.View):
    """Dropdown for selecting a token for top traders lookup."""

    def __init__(
        self,
        candidates: list[TokenInfo],
        config: Config,
        *,
        original_interaction: discord.Interaction,
    ):
        super().__init__(timeout=60.0)
        self.config = config
        self.candidates = candidates
        self.original_interaction = original_interaction

        options = []
        for i, token in enumerate(candidates[:25]):
            chain_icon = CHAIN_ICONS.get(token.chain, "")
            mcap = f"MCap: ${token.market_cap:,.0f}" if token.market_cap else "MCap: N/A"
            liq = f"Liq: ${token.liquidity_usd:,.0f}" if token.liquidity_usd else "Liq: N/A"
            options.append(
                discord.SelectOption(
                    label=f"{chain_icon} {token.symbol} — {token.name[:40]}",
                    description=f"{mcap} | {liq} | {token.mint_address[:20]}...",
                    value=str(i),
                )
            )

        select = discord.ui.Select(
            placeholder="Select the correct token...",
            options=options,
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()

        idx = int(interaction.data["values"][0])
        selected = self.candidates[idx]

        result = await asyncio.to_thread(_fetch_top_traders, selected, self.config)
        embeds = build_top_traders_embeds(result)
        await interaction.followup.send(embeds=embeds)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.original_interaction.edit_original_response(view=self)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Fomo helpers
# ---------------------------------------------------------------------------

# Explorer URL builders per chain
def _wallet_explorer_url(wallet: str, network_id: int) -> str:
    if network_id == FOMO_NETWORK_IDS["solana"]:
        return f"https://solscan.io/account/{wallet}"
    elif network_id == FOMO_NETWORK_IDS["base"]:
        return f"https://basescan.org/address/{wallet}"
    else:
        return f"https://bscscan.com/address/{wallet}"


def _token_dexscreener_url(token_address: str, network_id: int) -> str:
    chain_slug = {
        FOMO_NETWORK_IDS["solana"]: "solana",
        FOMO_NETWORK_IDS["base"]: "base",
        FOMO_NETWORK_IDS["bsc"]: "bsc",
    }.get(network_id, "solana")
    return f"https://dexscreener.com/{chain_slug}/{token_address}"


def _fomo_client_from_config(config) -> FomoClient | None:
    if not config.fomo_bearer_token:
        return None
    return FomoClient(
        bearer_token=config.fomo_bearer_token,
        installation_id=config.fomo_installation_id,
        refresh_token=getattr(config, "fomo_refresh_token", None),
    )


def _fetch_fomo_user(username: str, config) -> tuple[dict, list[dict]]:
    """
    Search for a fomo user by handle/name, then fetch their full summary.
    Returns (summary_dict, search_candidates).
    Runs in thread.
    """
    fomo = _fomo_client_from_config(config)
    if not fomo:
        raise ValueError("FOMO_BEARER_TOKEN not configured.")
    try:
        candidates = fomo.search_users(username)
        if not candidates:
            return {}, []
        # Use the best match (first result)
        profile = candidates[0]
        summary = fomo.get_user_summary(profile["id"], profile=profile)
        return summary, candidates
    finally:
        fomo.close()


def _fetch_fomo_holders(token_address: str, network_id: int, config) -> tuple[list[dict], int]:
    """Fetch top 10 fomo holders for a token with PnL. Runs in thread.
    Returns (holders, actual_network_id) — falls back to other chains if the
    primary network_id returns no results."""
    fomo = _fomo_client_from_config(config)
    if not fomo:
        raise ValueError("FOMO_BEARER_TOKEN not configured.")
    # Fomo API requires lowercase EVM addresses; TokenResolver returns checksummed ones
    if token_address.startswith("0x"):
        token_address = token_address.lower()
    try:
        holders = fomo.get_token_holders_with_pnl(token_address, network_id, top_n=10)
        if holders:
            return holders, network_id
        # Fallback: try other supported chains
        for chain, nid in FOMO_NETWORK_IDS.items():
            if nid == network_id:
                continue
            holders = fomo.get_token_holders_with_pnl(token_address, nid, top_n=10)
            if holders:
                return holders, nid
        return [], network_id
    finally:
        fomo.close()


def build_fomo_user_embeds(summary: dict) -> list[discord.Embed]:
    """Build Discord embeds for a fomo user profile."""
    embeds = []

    user_id = summary.get("userId", "")
    sol = summary.get("solana_wallet")
    evm = summary.get("evm_wallet")
    realized = summary.get("totalRealizedPnlUsd", 0)
    unrealized = summary.get("totalUnrealizedPnlUsd", 0)
    pfp = summary.get("profilePictureLink")
    display = summary.get("displayName") or summary.get("userHandle") or user_id[:12]
    handle = summary.get("userHandle")
    description = summary.get("description")
    followers = summary.get("followers", 0)
    following = summary.get("following", 0)
    volume = summary.get("totalVolume", 0)

    # --- Profile embed ---
    desc_parts = []
    if description:
        desc_parts.append(f"*{description}*")
    if sol:
        desc_parts.append(f"**Solana:** [Solscan]({_wallet_explorer_url(sol, FOMO_NETWORK_IDS['solana'])})\n`{sol}`")
    if evm:
        desc_parts.append(f"**EVM:** [Explorer]({_wallet_explorer_url(evm, FOMO_NETWORK_IDS['base'])})\n`{evm}`")
    if handle:
        desc_parts.append(f"**Twitter:** [@{handle}](https://x.com/{handle})")
    desc_parts.append(f"**Followers:** {followers:,} | **Following:** {following:,}")

    pnl_color = discord.Color.green() if realized >= 0 else discord.Color.red()
    profile_embed = discord.Embed(
        title=f"Fomo Profile — {display}",
        description="\n".join(desc_parts) if desc_parts else "No wallet data found.",
        color=pnl_color,
        url=f"https://fomo.family/trader/{user_id}",
    )
    if pfp:
        profile_embed.set_thumbnail(url=pfp)

    profile_embed.add_field(name="Realized PnL", value=f"**${realized:,.2f}**", inline=True)
    profile_embed.add_field(name="Unrealized PnL", value=f"**${unrealized:,.2f}**", inline=True)
    profile_embed.add_field(
        name="Trades",
        value=f"{summary.get('active_count', 0)} active / {summary.get('closed_count', 0)} closed",
        inline=True,
    )
    profile_embed.add_field(name="Total Volume", value=f"**${volume:,.2f}**", inline=True)
    embeds.append(profile_embed)

    # --- Top holdings embed ---
    top_holdings = summary.get("top_holdings", [])
    if top_holdings:
        lines = []
        for t in top_holdings:
            sym = t.get("tokenMetadata", {}).get("symbol", "???")
            net = t.get("networkId", FOMO_NETWORK_IDS["solana"])
            addr = t.get("tokenAddress", "")
            cost = float(t.get("totalCostBasis") or 0)
            upnl = float(t.get("unrealizedPnlUsd") or 0)
            ds_url = _token_dexscreener_url(addr, net)
            lines.append(f"• [{sym}]({ds_url}) — In: **${cost:,.0f}** | uPnL: **${upnl:,.0f}**")
        holdings_embed = discord.Embed(
            title="Current Holdings",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embeds.append(holdings_embed)

    # --- Top closed trades embed ---
    top_closed = summary.get("top_closed", [])
    if top_closed:
        lines = []
        for t in top_closed:
            sym = t.get("tokenMetadata", {}).get("symbol", "???")
            net = t.get("networkId", FOMO_NETWORK_IDS["solana"])
            addr = t.get("tokenAddress", "")
            cost = float(t.get("totalCostBasis") or 0)
            rpnl = float(t.get("realizedPnlUsd") or 0)
            pct = (rpnl / cost * 100) if cost > 0 else 0
            ds_url = _token_dexscreener_url(addr, net)
            lines.append(f"• [{sym}]({ds_url}) — In: **${cost:,.0f}** | PnL: **${rpnl:,.0f}** ({pct:,.0f}%)")
        closed_embed = discord.Embed(
            title="Best Closed Trades",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embeds.append(closed_embed)

    return embeds


def build_fomo_holders_embeds(
    token_address: str,
    network_id: int,
    token_meta: dict,
    holders: list[dict],
) -> list[discord.Embed]:
    """Build Discord embeds for fomo token holders."""
    chain_name = FOMO_NETWORK_IDS_REVERSE.get(network_id, "unknown")
    chain_icon = CHAIN_ICONS.get(chain_name, "")
    sym = token_meta.get("symbol", token_address[:8])
    ds_url = _token_dexscreener_url(token_address, network_id)
    token_image = token_meta.get("imageLargeUrl") or token_meta.get("thumbhash")

    summary_embed = discord.Embed(
        title=f"Fomo Holders — [{sym}]({ds_url})",
        description=(
            f"{chain_icon} **{chain_name.upper()}** | "
            f"[View on DexScreener]({ds_url})\n"
            f"Top {len(holders)} traders from fomo.family"
        ),
        color=discord.Color.og_blurple(),
        url=ds_url,
    )
    if token_image and token_image.startswith("http"):
        summary_embed.set_thumbnail(url=token_image)

    lines = []
    for i, h in enumerate(holders, 1):
        display = h.get("displayName") or f"User {i}"
        handle = h.get("userHandle")
        net = h.get("networkId", network_id)
        wallet = h.get("solana_wallet") or h.get("evm_wallet")

        # Build name part with links
        if handle:
            x_url = f"https://x.com/{handle}"
            name_part = f"[{display}]({x_url})"
        else:
            name_part = f"**{display}**"

        # Wallet explorer link + full address for copy-paste
        if wallet:
            explorer_url = _wallet_explorer_url(wallet, net)
            wallet_part = f"[Explorer]({explorer_url})\n`{wallet}`"
        else:
            wallet_part = "*no wallet*"

        rpnl = h.get("realizedPnlUsd", 0)
        upnl = h.get("unrealizedPnlUsd", 0)
        cost = h.get("totalCostBasis", 0)
        holding = " (holding)" if h.get("stillHolding") else ""
        pnl_sign = "+" if rpnl >= 0 else ""

        lines.append(
            f"{i}. {name_part} | {wallet_part}\n"
            f"   In: **${cost:,.0f}** | rPnL: **{pnl_sign}${rpnl:,.0f}** | uPnL: **${upnl:,.0f}**{holding}"
        )

    holders_embed = discord.Embed(
        title=f"Top {len(holders)} Fomo Traders",
        description="\n".join(lines) if lines else "No traders found.",
        color=discord.Color.purple(),
    )
    return [summary_embed, holders_embed]


# ---------------------------------------------------------------------------
# Fomo slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="fomouser",
    description="Look up a fomo.family user by name — wallets, holdings, PnL and Twitter",
)
@app_commands.describe(username="Fomo username or display name (e.g. 'kramer', 'nosanity')")
async def cmd_fomouser(interaction: discord.Interaction, username: str):
    await interaction.response.defer()
    try:
        if not bot.config.fomo_bearer_token:
            await interaction.followup.send(embed=discord.Embed(
                title="Missing Config",
                description="FOMO_BEARER_TOKEN not set in .env — capture it from the app via HTTP Toolkit.",
                color=discord.Color.red(),
            ))
            return

        summary, candidates = await asyncio.to_thread(_fetch_fomo_user, username.strip(), bot.config)

        if not summary:
            await interaction.followup.send(embed=discord.Embed(
                title="User Not Found",
                description=f"No fomo users found matching `{username}`",
                color=discord.Color.red(),
            ))
            return

        # If multiple candidates, show a note with alternatives
        embeds = build_fomo_user_embeds(summary)

        if len(candidates) > 1:
            alt_names = ", ".join(
                f"`{c.get('userHandle') or c.get('displayName')}`"
                for c in candidates[1:4]
            )
            embeds[0].set_footer(text=f"Showing best match. Other results: {alt_names}")

        await interaction.followup.send(embeds=embeds)

    except Exception as e:
        logger.exception("Error in /fomouser")
        await interaction.followup.send(embed=discord.Embed(
            title="Error", description=f"```\n{str(e)[:3900]}\n```", color=discord.Color.red(),
        ))


@bot.tree.command(
    name="fomoholders",
    description="Top 10 fomo.family holders for a token with PnL and wallet links",
)
@app_commands.describe(token="Token contract address or ticker")
async def cmd_fomoholders(interaction: discord.Interaction, token: str):
    await interaction.response.defer()
    try:
        if not bot.config.fomo_bearer_token:
            await interaction.followup.send(embed=discord.Embed(
                title="Missing Config",
                description="FOMO_BEARER_TOKEN not set in .env — capture it from the app via HTTP Toolkit.",
                color=discord.Color.red(),
            ))
            return

        token_input = token.strip()
        address_type = detect_chain_from_address(token_input)

        # Resolve token info for metadata (symbol, image, chain)
        token_info = None
        if address_type in ("solana", "evm"):
            resolver = TokenResolver()
            try:
                token_info = await asyncio.to_thread(resolver.get_by_mint_address, token_input)
            finally:
                resolver.close()
        else:
            resolver = TokenResolver()
            try:
                candidates = await asyncio.to_thread(resolver.search_by_ticker, token_input.upper())
            finally:
                resolver.close()
            if candidates:
                token_info = candidates[0]

        if not token_info:
            await interaction.followup.send(embed=discord.Embed(
                title="Token Not Found",
                description=f"Could not resolve `{token_input}`",
                color=discord.Color.red(),
            ))
            return

        network_id = FOMO_NETWORK_IDS.get(token_info.chain)
        if not network_id:
            await interaction.followup.send(embed=discord.Embed(
                title="Unsupported Chain",
                description=f"Chain `{token_info.chain}` is not supported by fomo.family.",
                color=discord.Color.red(),
            ))
            return

        holders, actual_network_id = await asyncio.to_thread(
            _fetch_fomo_holders, token_info.mint_address, network_id, bot.config
        )

        token_meta = {
            "symbol": token_info.symbol,
            "imageLargeUrl": getattr(token_info, "image_url", None),
        }
        embeds = build_fomo_holders_embeds(
            token_info.mint_address, actual_network_id, token_meta, holders
        )
        await interaction.followup.send(embeds=embeds)

    except Exception as e:
        logger.exception("Error in /fomoholders")
        await interaction.followup.send(embed=discord.Embed(
            title="Error", description=f"```\n{str(e)[:3900]}\n```", color=discord.Color.red(),
        ))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN not set in .env file")
        print("Get a bot token at https://discord.com/developers/applications")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bot.run(token)


if __name__ == "__main__":
    main()

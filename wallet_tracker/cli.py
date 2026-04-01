"""Command-line interface for the wallet tracker."""

import os
import sys

# Fix Windows encoding issues
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt
from rich.table import Table

from .config import Config
from .matcher import WalletMatcher
from .models import HoldingQuery, SearchResult, TokenInfo, VerificationResult
from .token_resolver import TokenResolver


console = Console(force_terminal=True)


def print_banner():
    """Print the application banner."""
    banner = (
        "\n[bold cyan]"
        "+-----------------------------------------------------------+\n"
        "|           SOLANA WALLET TRACKER                           |\n"
        "|     Find wallets by token holdings                        |\n"
        "+-----------------------------------------------------------+"
        "[/bold cyan]\n"
    )
    console.print(banner)


def _is_mint_address(value: str) -> bool:
    """Check if a string looks like a Solana mint address (base58, 32-44 chars)."""
    import re
    return bool(re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', value))


def _select_token(ticker: str) -> TokenInfo | None:
    """
    Search for a ticker and let the user pick the correct token
    when multiple matches exist.

    Returns:
        Selected TokenInfo, or None if not found
    """
    resolver = TokenResolver()
    try:
        candidates = resolver.search_by_ticker(ticker)

        if not candidates:
            console.print(f"[red]No tokens found for ticker: {ticker}[/red]")
            return None

        if len(candidates) == 1:
            return candidates[0]

        # Multiple matches — show table and let user pick
        console.print(f"\n[yellow]Multiple tokens found for '{ticker}':[/yellow]\n")

        table = Table()
        table.add_column("#", style="cyan", width=4)
        table.add_column("Symbol", width=10)
        table.add_column("Name", width=25)
        table.add_column("Mint Address", width=46)
        table.add_column("Market Cap", width=14)
        table.add_column("Liquidity", width=14)

        for i, token in enumerate(candidates, 1):
            table.add_row(
                str(i),
                token.symbol,
                token.name[:25],
                token.mint_address,
                f"${token.market_cap:,.0f}" if token.market_cap else "N/A",
                f"${token.liquidity_usd:,.0f}" if token.liquidity_usd else "N/A",
            )

        console.print(table)
        console.print()

        choice = IntPrompt.ask(
            f"  Select token (1-{len(candidates)})",
            default=1,
        )

        if 1 <= choice <= len(candidates):
            return candidates[choice - 1]
        else:
            console.print("[red]Invalid selection, using first result.[/red]")
            return candidates[0]
    finally:
        resolver.close()


def get_holding_input(label: str = "holding") -> HoldingQuery:
    """
    Interactively get holding details from user.

    Accepts either a ticker symbol or a mint address directly.
    When a ticker matches multiple tokens, prompts user to select.

    Args:
        label: Label for this holding (e.g., "PRIMARY", "VERIFICATION")

    Returns:
        HoldingQuery with user input
    """
    console.print(f"\n[bold yellow]Enter {label} holding details:[/bold yellow]")
    console.print("  [dim]Enter a ticker symbol or paste a mint address[/dim]")

    token_input = Prompt.ask("  Token").strip()
    token_amount = FloatPrompt.ask("  Exact token amount held")

    query = HoldingQuery(ticker=token_input.upper(), token_amount=token_amount)

    # If it looks like a mint address, set it directly
    if _is_mint_address(token_input):
        query.mint_address = token_input
        query.ticker = token_input[:8] + "..."
    else:
        # Search by ticker and let user disambiguate
        token = _select_token(token_input.upper())
        if token:
            query.mint_address = token.mint_address
            query.ticker = token.symbol

    return query


def display_search_result(result: SearchResult):
    """Display search results in a formatted table."""
    console.print()

    if result.token_info:
        token = result.token_info
        console.print(Panel(
            f"[bold]{token.symbol}[/bold] - {token.name}\n"
            f"Mint: [dim]{token.mint_address}[/dim]\n"
            f"Current Price: ${token.price_usd:.10f}\n"
            f"Market Cap: ${token.market_cap:,.0f}\n"
            f"Liquidity: ${token.liquidity_usd:,.0f}",
            title="Token Found",
            border_style="green",
        ))
    else:
        console.print(Panel(
            f"Could not find token: {result.query.ticker}",
            title="Token Not Found",
            border_style="red",
        ))
        return

    console.print(
        f"\n[dim]Scanned {result.total_holders_scanned} holders "
        f"in {result.search_time_ms}ms[/dim]"
    )

    if not result.candidates:
        console.print(Panel(
            "No matching wallets found.\n"
            "Try adjusting the token amount or check the ticker.",
            title="No Matches",
            border_style="yellow",
        ))
        return

    # Create results table
    table = Table(title=f"Found {len(result.candidates)} Candidate Wallet(s)")

    table.add_column("Rank", style="cyan", width=6)
    table.add_column("Wallet Address", style="green")
    table.add_column("Token Balance", style="yellow", width=20)

    for i, match in enumerate(result.candidates[:20], 1):  # Top 20
        # Get the balance from holdings dict
        balance = "N/A"
        if match.holdings:
            amt = list(match.holdings.values())[0]
            balance = f"{amt:,.6f}"

        table.add_row(
            str(i),
            match.address,
            balance,
        )

    console.print(table)


def display_verification_result(result: VerificationResult):
    """Display verification results."""
    console.print()

    if result.verified:
        console.print(Panel(
            f"[bold green]WALLET CONFIRMED[/bold green]\n\n"
            f"[bold]{result.wallet}[/bold]\n\n"
            f"This wallet holds both specified token amounts.",
            title="Verification Successful",
            border_style="green",
        ))
    elif result.confirmed_wallets:
        console.print(Panel(
            f"[bold yellow]MULTIPLE MATCHES[/bold yellow]\n\n"
            f"Found {len(result.confirmed_wallets)} wallets matching both holdings:\n\n" +
            "\n".join(f"  - {w}" for w in result.confirmed_wallets),
            title="Multiple Results",
            border_style="yellow",
        ))
    else:
        console.print(Panel(
            f"[bold red]NO MATCHES[/bold red]\n\n"
            f"No wallet found holding both specified token amounts.\n\n"
            f"Primary holding candidates: {len(result.primary_candidates)}\n"
            f"Verification holding candidates: {len(result.verification_candidates)}",
            title="Verification Failed",
            border_style="red",
        ))


def interactive_search():
    """Run interactive wallet search."""
    try:
        config = Config.load()
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return

    matcher = WalletMatcher(config)

    try:
        # Get primary holding
        primary = get_holding_input("PRIMARY")

        console.print("\n[bold]Searching holders...[/bold]")
        result = matcher.find_candidates(primary)

        display_search_result(result)

        # Check if verification needed
        if result.candidates and len(result.candidates) > 1:
            console.print()
            if Confirm.ask("Multiple candidates found. Add verification holding?"):
                verification = get_holding_input("VERIFICATION")

                console.print("\n[bold]Verifying...[/bold]")
                verify_result = matcher.verify_with_second_holding(
                    primary, verification
                )

                display_verification_result(verify_result)

        elif result.candidates and len(result.candidates) == 1:
            console.print(Panel(
                f"[bold green]LIKELY WALLET[/bold green]\n\n"
                f"[bold]{result.candidates[0].address}[/bold]\n\n"
                f"[dim]Add a verification holding to confirm.[/dim]",
                title="Single Match Found",
                border_style="green",
            ))

    finally:
        matcher.close()


def test_token_resolution(ticker: str):
    """Test token resolution for a ticker."""
    from .token_resolver import TokenResolver

    console.print(f"\n[bold]Testing token resolution for: {ticker}[/bold]\n")

    resolver = TokenResolver()
    try:
        tokens = resolver.search_by_ticker(ticker)

        if not tokens:
            console.print(f"[red]No tokens found for ticker: {ticker}[/red]")
            return

        table = Table(title=f"Found {len(tokens)} token(s)")
        table.add_column("Symbol")
        table.add_column("Name")
        table.add_column("Mint Address")
        table.add_column("Market Cap")
        table.add_column("Liquidity")

        for token in tokens:
            table.add_row(
                token.symbol,
                token.name[:30],
                token.mint_address,
                f"${token.market_cap:,.0f}" if token.market_cap else "N/A",
                f"${token.liquidity_usd:,.0f}" if token.liquidity_usd else "N/A",
            )

        console.print(table)
    finally:
        resolver.close()


def main():
    """Main entry point."""
    print_banner()

    # Check for command line arguments
    if len(sys.argv) > 1:
        if sys.argv[1] == "--test-token" and len(sys.argv) > 2:
            test_token_resolution(sys.argv[2])
            return
        elif sys.argv[1] == "--help":
            console.print("""
[bold]Usage:[/bold]
  python -m wallet_tracker.cli              Interactive mode
  python -m wallet_tracker.cli --test-token TICKER    Test token resolution
  python -m wallet_tracker.cli --help       Show this help

[bold]How it works:[/bold]
  1. Enter a ticker symbol and the exact amount of tokens held
  2. The tool searches all holders of that token for a matching balance
  3. Optionally verify with a second token + amount to narrow results

[bold]Setup:[/bold]
  1. Copy .env.example to .env
  2. Get your free Helius API key at https://helius.dev
  3. Add it to .env: HELIUS_API_KEY=your_key_here
""")
            return

    # Run interactive search
    interactive_search()


if __name__ == "__main__":
    main()

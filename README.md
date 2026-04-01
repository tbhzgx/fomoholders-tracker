# Solana Wallet Tracker

A tool to find and identify Solana wallets by their token holdings. Supports both an interactive CLI and a Discord bot interface.

## Features

- **Find wallets** holding an exact amount of any Solana token
- **Verify wallets** by cross-referencing two different token holdings
- **Search by ticker** (e.g. `BONK`) or direct mint address
- **Token disambiguation** — automatically handles multiple tokens sharing the same ticker, ranked by liquidity
- **Tolerance-based matching** — configurable tolerance (default 0.1%) for amount comparisons
- **Discord bot** with `/find` and `/verify` slash commands

## Prerequisites

- Python 3.10+
- A free [Helius](https://helius.dev) API key (1M credits/month on the free tier)
- A Discord bot token (if using the bot)

## Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/<your-username>/wallet-tracker.git
   cd wallet-tracker
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and fill in your keys:

   ```env
   HELIUS_API_KEY=your_helius_api_key_here
   DISCORD_BOT_TOKEN=your_discord_bot_token_here   # only needed for the bot
   ```

4. **Adjust settings** (optional)

   Edit `config.json` to change defaults:

   ```json
   {
       "tolerances": {
           "token_amount_pct": 0.003
       },
       "max_holder_pages": 50
   }
   ```

   | Setting | Default | Description |
   |---|---|---|
   | `token_amount_pct` | `0.001` (0.1%) | Tolerance when matching token amounts |
   | `max_holder_pages` | `50` | Max pages of holders to scan per query |

## Usage

### CLI

```bash
# Interactive mode
python -m wallet_tracker

# Test token resolution
python -m wallet_tracker --test-token BONK

# Help
python -m wallet_tracker --help
```

### Discord Bot

```bash
python bot.py
```

**Slash commands:**

| Command | Parameters | Description |
|---|---|---|
| `/find` | `token`, `amount` | Find wallets holding an exact amount of a token |
| `/verify` | `token1`, `amount1`, `token2`, `amount2` | Verify a wallet by matching two token holdings |

## Project Structure

```
wallet_tracker/
├── __init__.py          # Package exports
├── __main__.py          # Module entry point
├── cli.py               # Interactive CLI
├── config.py            # Configuration loader
├── models.py            # Data models (TokenInfo, HoldingQuery, etc.)
├── matcher.py           # Core wallet matching engine
├── token_resolver.py    # Ticker → mint address resolution
└── api/
    ├── base.py          # Base HTTP client with retry/rate limiting
    ├── helius.py        # Helius API client (token holders)
    ├── dexscreener.py   # DexScreener API client (token search)
    └── solana_rpc.py    # Solana JSON-RPC client
bot.py                   # Discord bot
config.json              # User settings
.env.example             # Environment variable template
requirements.txt         # Python dependencies
```

## How It Works

1. **Resolve** a ticker symbol to a Solana mint address via DexScreener
2. **Fetch** token decimals from the Helius RPC
3. **Paginate** through all token holders using the Helius `getTokenAccounts` endpoint
4. **Aggregate** amounts by owner wallet (a single wallet can have multiple token accounts)
5. **Match** holders whose balance falls within the configured tolerance of the target amount

For verification, the process runs for two tokens and intersects the candidate sets.

## APIs Used

| API | Purpose | Rate Limit |
|---|---|---|
| [Helius](https://helius.dev) | Token holders, supply, RPC | 1M credits/month (free) |
| [DexScreener](https://dexscreener.com) | Token search and market data | 60 req/min |
| Solana RPC | Fallback blockchain queries | Public node limits |

## License

MIT
# FomoHoldersTracker

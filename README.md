# bet-analysis

MCP (Model Context Protocol) server for **pre-match sports betting analysis** powered by a debate between **Claude, GPT-4, and Gemini**.

> **Status:** v0.1 — skeleton. Tools return stubs. Data providers, aggregator, and debate orchestrator land in subsequent commits per the implementation order in `docs/spec.md` (and the project README of this repo's design).

## What it does

`analyze_match` collects all the relevant pre-match data (form, H2H, injuries, lineups, referee, weather, market odds + movement) and runs a 3-round debate between three AI models with distinct roles:

- **Claude — The Skeptic** — finds reasons not to bet
- **GPT — The Statistician** — numbers, EV, market inefficiencies
- **Gemini — The Contextualist** — motivation, narrative, fixture congestion

Round 3 synthesizes into a single `FinalRecommendation` with a consensus pick, confidence, and explicit no-bet conditions.

## Install

Requires Python 3.11+. We use [`uv`](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/ereencaan/bet-analysis.git
cd bet-analysis
uv sync
cp .env.example .env
# fill in API keys in .env
```

## Run (stdio)

```bash
uv run bet-analysis
```

## Wire up to Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "bet-analysis": {
      "command": "uv",
      "args": ["--directory", "C:/Users/ereen/source/repos/Bet Analysis", "run", "bet-analysis"]
    }
  }
}
```

Restart Claude Desktop. The `analyze_match` tool should appear.

## Tools

| Tool | Purpose |
|---|---|
| `analyze_match` | Full pipeline: data fetch → context build → 3-AI debate → recommendation |
| `get_team_form` | Quick form lookup (debug / power user) |
| `clear_cache` | Invalidate cached data by key prefix |

## API keys you'll need

- **API-Football** (RapidAPI) — football data
- **The Odds API** — market odds
- **OpenWeather** — weather at venue
- **Anthropic / OpenAI / Google** — the three debaters

Optional later: Sportradar (multi-sport).

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check src/
```

## License

MIT.

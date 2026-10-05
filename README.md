# Touchline Tipster v4.0

## Environment variables

Required:
- TELEGRAM_BOT_TOKEN

Recommended:
- OPENFOOT_API_KEY (or FOOTBALL_API_KEY)
- SPORTYBET_REGION=gh
- SPORTYBET_API_BASE_URL=https://www.sportybet.com

Render automatically supplies PORT.

## Main examples

Analyze:
`/analyze Chelsea vs Arsenal`

Natural language:
- Analyze Chelsea vs Arsenal
- Analyze this SportyBet code ABC123
- Pick the best 5 matches
- Pick the best 5 odds
- Pick 5 matches around 10 odds
- Pick 10 matches, 20 odds, balanced
- Make this code safer

## Odds interpretation

- `5 odds` = 5.00 combined odds
- `5 matches` = 5 selections
- `5 matches around 10 odds` = 5 selections targeting about 10.00 combined odds

Supported target range: 1.50 to 100,000,000.00.

## Important

SportyBet's booking-code interface is an undocumented web interface and can change. The bot never fabricates selections when a code cannot be resolved.

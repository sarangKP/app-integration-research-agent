# MCP Review Notes

Review of flagged fields in the data files, checked against vendor docs and search results (24 Nov 2026).

## Summary

8 apps, 13 flagged fields checked.

- **Wrong: 6** (Copper MCP, and the 5 "unknown" MCP entries)
- **Unproven or weak: 3** (Close `paid_plan_required`, Close `bearer_token`, HubSpot beta quote)
- **Held up: 4** (HubSpot MCP official, Close MCP official, Copper auth, HubSpot `bearer_token`)


---

## 1. HubSpot

| Field | Data said | What I found | Link |
|---|---|---|---|
| MCP official | Yes, no URL stored | Page is on developers.hubspot.com and describes a remote HubSpot MCP server and a local Developer MCP server. No server URL on the page (setup links may have one, not checked). Looks official since HubSpot publishes it. | [ai-tools/mcp](https://developers.hubspot.com/ai-tools/mcp) |
| Beta quote | One quote used for 3 fields | Changelog is titled "available in Public Beta". It describes an NPM package using a private app access token. It is an old post and may not describe the current remote server. | [changelog](https://developers.hubspot.com/changelog/mcp-server-beta) |
| Auth | Includes `bearer_token` | Docs list OAuth, static auth tokens, service keys, developer API keys and client credentials. OAuth, static tokens and service keys are all sent as a Bearer header, so `bearer_token` fits, but it is not a separate method. | [auth overview](https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/overview) |

## 2. Close

| Field | Data said | What I found | Link |
|---|---|---|---|
| MCP official | Yes, no URL stored | MCP tools page is on developer.close.com, published by Close. No server URL on the page. Official looks right. | [mcp/tools](https://developer.close.com/mcp/tools) |
| Access | `paid_plan_required` | No free plan, but a 14-day free trial with no credit card. FAQ "Does Close include API access?" starts with "Yes" (full answer not captured). MCP is listed on the cheapest Solo plan. "High-volume API access" is a plan feature, not the only API access. So `paid_plan_required` is unproven. | [pricing](https://close.com/pricing) |
| Auth | `bearer_token` | API key page says HTTP Basic auth: key as username, empty password. No mention of bearer on that page. Looks doubtful for API keys. OAuth page not checked, so unproven. | [API key auth](https://developer.close.com/api/overview/api-key-authentication) |

## 3. Copper

| Field | Data said | What I found | Link |
|---|---|---|---|
| MCP | None | **Wrong.** copper.com/mcp says Copper is launching an official MCP server, built and hosted by Copper. Beta launches November 2026, read-only, Claude only. Page is marked "Coming soon" with a waitlist, so it is not publicly available yet. Third-party servers also exist (Pipedream, Composio, GitHub repos). | [copper.com/mcp](https://www.copper.com/mcp) |
| Auth | API key and OAuth2 | Matches. Docs list API keys (token plus user email in header) and OAuth 2.0. | [authentication](https://developer.copper.com/introduction/authentication.html) |

## 4. Apps marked "unknown"

All five have an MCP server, so "unknown" was wrong for each.

| App | What I found | Link |
|---|---|---|
| GitHub | Official. `github/github-mcp-server`. Search snippet says GitHub hosts a remote server. URL `https://api.githubcopilot.com/mcp/` appears only in Google's AI Overview, so not confirmed on a GitHub page. | [search](https://www.google.com/search?q=GitHub+official+MCP+server) |
| Notion | Official. Notion docs give remote URL `https://mcp.notion.com/mcp`. The older `makenotion/notion-mcp-server` repo is self-hosted and no longer actively maintained. | [search](https://www.google.com/search?q=Notion+official+MCP+server) |
| Jira | Official. Atlassian publishes the Rovo MCP server (`atlassian/atlassian-mcp-server` and Atlassian support docs). `sooperset/mcp-atlassian` is community. | [search](https://www.google.com/search?q=Atlassian+Jira+official+MCP+server) |
| Linear | Official. linear.app/docs/mcp. A third-party article gives the URL `https://mcp.linear.app/mcp`. | [search](https://www.google.com/search?q=Linear+official+MCP+server) |
| Clay | Official. clay.com/mcp is Clay's own page, but it gave no readable text. URL `https://api.clay.com/v3/mcp` appears only in Google's AI Overview, so it is unconfirmed. Some results (PulseMCP, mcp.so) are for a different "Clay" personal-CRM product. | [search](https://www.google.com/search?q=Clay+MCP+server) |

## Caveats

- For the five "unknown" apps the evidence is Google result pages, not the vendors' own pages. That shows an official server exists, but check URLs on the vendor page before storing them.
- Google AI Overview text is not a primary source. It was the only source for the GitHub and Clay URLs.
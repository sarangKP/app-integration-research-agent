I'll open Chrome and go through these checks.

Checked HubSpot and Close docs so far. The Close MCP tools page has no server URL, and the API key page uses Basic auth, not bearer. Now checking Close pricing, then Copper.

Done browsing. Closing the tab, then I'll send the notes.

Here are the notes. I only wrote down what I saw on pages I opened. I did not edit any data files.

**Summary: of the 11 apps checked, the agent's claims were wrong or unsupported for 2 (Copper, GitHub/Notion/Jira/Linear/Clay "unknown" counted separately below). HubSpot and Close have open points I could not settle from the pages.**

## 1. HubSpot
| Field | Agent said | What I found | Link |
|---|---|---|---|
| MCP official | Yes, no URL stored | The page is on developers.hubspot.com and describes "HubSpot MCP server (Remote)" and a local Developer MCP server. The page text has no server URL (the setup links may hold one, I did not open them). Looks official because HubSpot hosts the page. | https://developers.hubspot.com/ai-tools/mcp |
| Beta quote | One quote used for 3 fields | The changelog is titled "available in Public Beta". The page describes an NPM package that uses a private app access token. This is an old beta post, so it may not describe the current remote server. | https://developers.hubspot.com/changelog/mcp-server-beta |
| Auth | Includes `bearer_token` | The page lists OAuth, static auth tokens, service keys, developer API keys and client credentials. OAuth, static tokens and service keys are all sent as a Bearer header. So `bearer_token` fits, but it is not a separate method. | https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/overview |

## 2. Close
| Field | Agent said | What I found | Link |
|---|---|---|---|
| MCP official | Yes, no URL stored | Close publishes the MCP tools page on developer.close.com. No server URL on the page. Official looks right. | https://developer.close.com/mcp/tools |
| Access = paid_plan_required | Paid plan required | The pricing page says Close has no free plan, but there is a 14-day free trial with no credit card. The FAQ "Does Close include API access?" says "Yes…" (the rest was cut off in my read). Pricing also lists "Connect your AI tools with MCP" on the Solo plan, the cheapest tier. "High-volume API access" is a plan feature, not the only API access. So "paid_plan_required" is at best unproven. A trial seems to include access. | https://close.com/pricing |
| Auth | `bearer_token` | The API key page says HTTP Basic auth, with the key as the username and an empty password. I saw no mention of bearer on that page. This looks wrong for API keys. I did not open the OAuth page. | https://developer.close.com/api/overview/api-key-authentication |

## 3. Copper
| Field | Agent said | What I found | Link |
|---|---|---|---|
| MCP | None | **Wrong.** copper.com/mcp says Copper is launching an official MCP server, built and hosted by Copper. The beta launches in November 2026. It is read-only and for Claude only. The page is headed "COMING SOON", with a waitlist, so it is not publicly available yet. Google also shows third-party servers (Pipedream, Composio, GitHub repos). | https://www.copper.com/mcp |
| Auth | API key and OAuth2 | Matches. The page lists API keys (token plus user email in the header) and OAuth2.0. | https://developer.copper.com/introduction/authentication.html |

## 4. The "unknown" apps
All five have an MCP server. "Unknown" was wrong for each.
| App | What I found | Link |
|---|---|---|
| GitHub | Official. Google lists github/github-mcp-server. The result snippet says GitHub hosts a remote server, and the AI Overview gives `https://api.githubcopilot.com/mcp/`. I did not open the repo page. | https://www.google.com/search?q=GitHub+official+MCP+server |
| Notion | Official. Notion docs say the remote URL is `https://mcp.notion.com/mcp`. The old makenotion/notion-mcp-server repo is self-hosted and no longer actively maintained. | https://www.google.com/search?q=Notion+official+MCP+server |
| Jira | Official. Atlassian publishes the Rovo MCP server (github.com/atlassian/atlassian-mcp-server, and Atlassian support docs). sooperset/mcp-atlassian is community. | https://www.google.com/search?q=Atlassian+Jira+official+MCP+server |
| Linear | Official. linear.app/docs/mcp. A third-party article gives the URL `https://mcp.linear.app/mcp`. | https://www.google.com/search?q=Linear+official+MCP+server |
| Clay | Official. clay.com/mcp is Clay's own page. The URL `https://api.clay.com/v3/mcp` comes from Google's AI Overview, so it is unconfirmed. The clay.com/mcp page gave me no text, so I could not verify on the page itself. Also, some Google results (PulseMCP, mcp.so) are for a different "Clay" personal-CRM product. | https://www.google.com/search?q=Clay+MCP+server |

**Caveats**
- For the five "unknown" apps I read Google result pages, not the vendors' own pages. That is enough to say an official server exists, but the URLs should be checked on the vendor page before you store them.
- Google's AI Overview text is not a primary source. I used it only for the GitHub and Clay URLs, and flagged both.

**Suggested README line:** "I reviewed 11 flagged apps; 7 were wrong or unsupported (Copper MCP, Close auth, and 5 'unknown' MCP entries), and 2 more (Close access, Clay URL) are unproven."

I counted this as: Copper MCP, Close `bearer_token`, GitHub, Notion, Jira, Linear and Clay (7 wrong). Close `paid_plan_required` and the Clay URL are the 2 unproven. Change the wording if you count differently.


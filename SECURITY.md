# Security

EvoTrader can place real orders in a real brokerage account, so security
problems matter more here than in most hobby projects. Thank you for reporting
them responsibly.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Email
**adamc@immersialabs.com** instead, with:

- what the problem is and what an attacker could do with it;
- the steps to reproduce it, and the version or commit you tested;
- any suggested fix, if you have one.

Leave out your own keys, tokens, account numbers and trade data — describe
them instead. This is a small project maintained by one person: expect an
acknowledgement within about a week. Once a fix is released you'll be credited
in the changelog, unless you prefer not to be.

Only the latest release on the `main` branch receives security fixes.

## Keeping your own setup safe

These are the things most likely to hurt you. None of them needs a bug.

### Never share your keys or broker login tokens

- **`.env`** — in your data folder and/or the project folder — holds your AI
  provider keys, the market-data key and the console password. Never commit it,
  paste it into an issue, or share it in a screenshot, not even in a private
  repository.
- **`~/.evotrader/oauth/`** holds the token Robinhood issued when you signed
  in. Anyone who has these files can use your Robinhood account the way the
  app can, including placing orders, until the token expires or is revoked.
  Never copy, upload or share this folder.
- If either may have leaked: stop the app, create new API keys with each
  provider and delete the old ones, delete `~/.evotrader/oauth/` (you will
  sign in to Robinhood again on the next start), and check your Robinhood
  account for activity you don't recognise.

### Protect the web console

- **The console has no login unless you set `DASHBOARD_PASSWORD`**
  (`./run.sh setup` asks for it). Without a password, anyone who can reach the
  console can see your account, start trading cycles and approve orders.
- **Only this computer can reach the console by default**: the server listens
  on `127.0.0.1`, port 8080 (or `EVOTRADER_PORT`). To reach it from other
  devices, set `EVOTRADER_HOST=0.0.0.0`; the app then refuses to start
  without `DASHBOARD_PASSWORD`.
- **Never expose the console to the internet.** No router port forwarding, no
  open cloud firewall rule, no public tunnel. To use it from elsewhere, go
  through a VPN you control or an SSH tunnel, for example
  `ssh -L 8080:127.0.0.1:8080 you@your-server`, then open
  http://127.0.0.1:8080 locally.
- **Set a password even on a laptop.** Other websites cannot read the console
  (it grants no cross-origin access unless you list origins in
  `EVOTRADER_CORS_ORIGINS`), but a web page can still *send* requests to
  `127.0.0.1`; the password is what stops them.
- The console uses plain HTTP, so the password crosses the network
  unencrypted. On anything but your own machine, use it through the tunnel.
- The browser remembers the password in its local storage. On a shared
  computer, use **Lock Console** (the lock icon) when you are done.
- **Known limitation:** the console's live event stream (`/api/events`: log
  lines and status updates, which can include balances and orders) is not
  behind the password — a browser cannot attach one to that kind of
  connection. Other websites cannot read it, but anyone who can reach the port
  can. So if you set `EVOTRADER_HOST`, do it only on a network you trust (a
  VPN such as Tailscale), never on a public one.

### Keep the safety settings on until you trust them

Practice mode, the approval gate (`require_trade_approval`) and your limits in
`constitution.yaml` are the last line of defence against a bad decision by a
model or a bug. See **Safety** and **Practice mode and going live** in the
[README](README.md).

### Treat what the agents read as untrusted

The agents read news articles and other text from the internet. Text written
to manipulate an AI (a *prompt injection*) could try to steer a decision. This
is why orders are checked by code — the constitution, the risk gate, and your
approval — rather than trusted to a model's judgement.

# Changelog

Notable changes to EvoTrader are recorded here, newest first. While the
version starts with 0, any release may change behaviour, settings or the data
folder layout; each entry says what you need to do.

## Unreleased

### Added — in the console

- **The combined signal on the account chart.** A **Signal** button on the
  Account / P&L chart splits it into two panes on the same dates: the curve on
  top, each day lightly tinted green or red by the way the day's average signal
  leaned (stronger days darker), and below it the day's average as a bar with a
  thin line for its range. Hovering a day shows the value, its change from the
  day before, the signal's average and lean, its range, how many readings it
  had and the instrument. A strip under the chart says how often a
  long-leaning day was followed by a gain by the next day the system ran, how
  often a short-leaning day was followed by a loss, and the correlation
  between the two, with the number of days; over a few weeks that describes the
  period, it is not evidence of an edge. The choice is remembered in the
  browser. The daily figures come from a new endpoint,
  `/api/chart/signal-daily?period=…`, one row per US Eastern date, the same
  calendar the equity curve uses.
- **The account chart is easier to read with or without the overlay:** a
  smoother line over a soft fill, points only on hover and on deposit days,
  short date labels, round price levels, and one tooltip for the whole day
  with a guide line through it.

### Added — in what the agents read

- **The strategy agent reads the live channel record every cycle.** It had
  no read of the calibration record, so every base rate it reasoned from
  (which channel's calls have been right how often) was typed into its
  instructions by hand: it changed only when they did, and a count copied
  once could outlive the record it came from. Each cycle now renders that
  record from the same aggregation as the evolution agent's
  `get_signal_calibration` (by author, by channel and the final call by side,
  to the session's close and to the next session's close, for the primary
  instrument over the last 30 days, in about 2 KB at most), puts it in the
  strategy agent's prompt beside the trading handoff on either runtime, and
  keeps a copy in the thought log. A row resting on fewer than 5 independent
  days is marked (days), one with |t| under 2 (chance): neither is a base
  rate.
- **Which algorithm version made each call.** Attribution rows record
  `algo_version` (migration 0031), filled after each cycle from the session's
  stored market snapshot, past rows too. `get_signal_calibration` takes
  `algo_version` to score one version alone, and the channel record says how
  many calls the active version has scored. Authors in the calibration report
  now carry independent days and t like every other row.
- **Which way momentum's fast reads point, whatever its slow read says.**
  `fast_dissent` is the two fast reads (price beyond EMA 9 by
  `fast_dissent_min_atr` daily ATRs, the daily MACD histogram outside its
  neutral band) pointing AGAINST the moving-average term, so it turns false
  the moment that term crosses to their side. After a rally SMA 20 stays
  above SMA 50 for weeks and cancels a price far under SMA 20, and the MA term
  sits just below zero while price falls: the fast reads point down as hard
  as they ever do and `fast_dissent` reads false, so a rule that reads it to
  close a long cannot fire where the tape is most bearish. The momentum vote
  now reports `fast_read` (+1, -1 or 0: the fast reads' own direction) and
  `fast_read_agrees_value` (whether they point the way the vote does; null
  when either is neutral), both also kept with the vote in the attribution
  record, and `ma_signal_parts`, the MA term's three weighted parts (EMA 9
  against EMA 21, price against SMA 20, SMA 20 against SMA 50), which sum to
  it. `fast_dissent` is the same condition as before; no vote changes.
- **How far past its gate a grind is.** trend_persistence's strength starts
  from zero at its displacement gate, so a grind that has just cleared
  `min_cum_atr` reads `bearish_trigger: true` next to a value near zero. Its
  vote now reports `gate_margin_atr`: the displacement past the gate in daily
  ATRs, negative when short of it.
- **Which instruction version wrote what.** Each attribution row records the
  strategy instruction version it was made under
  (`strategy_instruction_version`, migration 0030; filled by the app, not the
  agent), and `get_signal_calibration` takes `instruction_version` to score
  one version alone. The trading handoff is stamped with the version it was
  written under; read under another version, it opens with a line saying so,
  and that a rule it quotes is void where it differs. Agent learnings
  (`store_learning`) are cut at 800 characters and stamped with their agent
  and instruction version; `query_user_notes` returns the stamp, with a note
  when the version differs.
- **A one-sided record is read against the window's base rate.** The
  calibration record's baseline is each sample's own majority direction, so
  a channel that only ever calls one side showed `edge_over_baseline` 0.0
  whenever it was right more than half the time: exactly the channels that
  choose their moments. `get_signal_calibration` now also gives, for every
  witness, side and channel, `n_long` and `n_short`,
  `expected_hit_unconditional` (what the same calls would hit at the window's
  up-share: that share for a long call, one minus it for a short) and
  `binomial_p_unconditional` (the exact two-sided probability of a hit count
  no more likely than the actual one; unclustered, read it with the number of
  independent days).
- **Whether a resting order works after hours.** The broker says per order
  whether it works outside the regular session, and the order book dropped
  it, so a cycle after the close could not tell whether a take-profit would
  act overnight. Each resting order now carries `extended_hours` (true, false,
  or null when its record does not say), in the book and in the coverage
  block's orders.
- **How each cycle started, in its own record.** A cycle started from the
  console, and one whose start-up steps (the order sync with the broker) ran
  long, both read like a scheduled cycle that started late. Each now says who
  started it, when not the schedule, and how long start-up steps over a minute
  took. After a restart, a scheduled minute still inside its grace with no
  run on record is recorded as a SKIPPED run saying the app was not running.
- **A sell refused for want of shares names the orders holding them.** The
  broker refuses a sell with "Not enough shares to sell" when the shares are
  held for working sell orders, a stop or a take-profit: the refusal says the
  protection is there. The executor's `place_equity_order` answer now carries
  those working orders (`reserved_by`) and a note to read the book again
  rather than cancel them to make room.
- **The executor is handed its account number.** It reads the agentic
  account's number with its instructions instead of recalling one, and an
  order sent for any other account is refused by the order gate
  (`WRONG_ACCOUNT`, with the right number in the fix).
- **Calls scored at their own session's close.** The evolution agent's
  calibration record measured every call at the next session's close and the
  fifth's, which suits the daily channels; the intraday ones aim at the same
  day's close and, measured a day later, read the next session's move. A
  third horizon, `forward_return_close` (migration 0029), is the return from
  the decision's price to the close of its own session, written once that
  session's daily bar is out (a session before the 1-day horizon, and never
  for a decision made after the close). `get_signal_calibration` takes
  `horizon="close"`.
- **A limit's distance from the market, at the order review.** The executor
  reviews every order with `review_equity_order`, whose answer carried the
  quote but said nothing about the limit's distance from it, so an empty
  `order_checks` read as clean while a buy limit sat well under the ask and
  filled only when the price came back down. The review now also carries
  `limit_vs_market`: the distance in basis points from the ask for a buy (the
  bid for a sell), and whether the order is marketable. Nothing is refused.
- **The loss streak names its scope.** The account limits' `consecutive_losses`
  is today's session only, by design (a Friday streak does not block Monday),
  while the performance analysis counts across days, so the morning after a
  losing day the two disagreed and read like a bug. The limits now say
  `streak_scope: "session"` and carry `consecutive_losses_all_time` beside it.
- **Where a stop may sit, and the stop checked in code.** The constitution
  caps a stop's distance from the entry (`max_stop_loss_pct`), and only the
  risk manager's reading of the constitution enforced it: `check_risk_limits`
  took no stop and answered APPROVED, with no violations, to proposals the same
  review then refused on the stop. `check_risk_limits` now takes an optional
  `stop_price`: for an entry, a stop further from `price` than the cap, or on
  the wrong side of it, is a violation, and the context reports the distance
  and the widest permitted stop (`context.stop`). `gather_market_data` and
  `get_ticker_snapshot` report `stop_geometry` for the instrument: its ATR as a
  percent of the price, the cap, the cap in ATRs (`cap_stop_atr`), the stop at
  the cap and at the configured ATR multiple
  (`position_sizing.default_stop_loss_atr_multiplier`, read for the first time)
  for a long and a short, and `cap_binding` when the ATR stop is wider than the
  cap. On a fund whose daily ATR is a large share of its price the cap binds:
  the stop goes at the cap, and the size follows from that distance.
- **A refused proposal reaches the next cycle.** The risk manager's verdict
  lived only in its report, and the next cycle starts from a new session, so a
  refused order read as one that had vanished at the broker and was proposed
  again unchanged. After a cycle whose last risk review was REJECTED, a
  `SYSTEM:` line in the trading handoff names the trade, the time, that nothing
  was sent to the broker, and the reasons: the check's own violations, else the
  report's required-modification section. `get_open_positions` returns
  `last_risk_verdict` (within 18 hours): the verdict, the trade checked, the
  check's violations and the report's reasons. Both are read back from the
  thought log; nothing new is stored. A verdict is taken only where the report
  labels it ("Verdict:", "Result:", "Decision:").
- **When a loss-streak pause ends.** The account-limits report
  (`account_rails`, in `get_open_positions` and `check_risk_limits`) adds
  `loss_streak_limit`, `last_loss_at` and `pause_expires_at`, so an agent can
  tell a streak at the limit that still binds from one whose pause has lapsed.
- **Protective coverage, computed.** `get_open_positions` returns
  `protective_coverage`: per ticker, the shares held (every open lot summed)
  against the shares under resting stop orders, with `uncovered_qty` and a
  `status` of `full`, `unknown` (covered on quantity, but a stop lacks its
  trigger price or time in force), `partial`, `none` or `flat`. A take-profit
  is listed but not counted as cover. The same block is in the
  `gather_market_data` snapshot, in `reconcile_pending_orders` (whose
  `order_book.orders` now lists each working order's quantity and levels), and
  in `record_trade`'s response as `coverage_after`, so the executor sees a
  shortfall in the same call that created it. Until now the agents worked this
  out by hand, and shares added to a position could run without a stop until a
  later cycle redid the arithmetic. Each journal row also keeps the figure from
  the moment it was recorded, in a new `coverage_at_record` column written by
  the code, never by an agent; the agent's reasoning text is not edited. The
  column is added automatically at the next start (migration 0026).
- **Where the upside stands, computed.** `get_open_positions` and the
  `gather_market_data` snapshot carry `take_profit_coverage` per ticker:
  whether a take-profit rests (`status`), its quantity, the shares without one,
  and each level (in the snapshot, also its distance in daily ATRs from the
  blended entry and from the price). Each held lot in the snapshot reports
  `gain_atr` (where it stands against its entry), `mfe_atr` (the best move
  since the entry: the sessions after it, plus today's bars after it when it
  was bought today) and, when a target is set, `t1_distance_atr`. The target is
  a new setting, `position_sizing.target_atr_multiplier`: set it to the first
  target your strategy instruction states (the starter settings use 1.5, as the
  starter instruction does). Unset, no target distance is reported.

### Added — off until you switch it on

- **A bitcoin-proxy channel, recording only.** `btc_lead_residual` reads the
  traded ticker against a second instrument named in `asset.reference_ticker`
  (for a bitcoin-holding stock, a spot bitcoin fund): the ticker's return
  since its prior close minus beta times the reference's over the same
  interval, in the ticker's ATR, with beta from 20 sessions of daily returns
  (clamped to 1..3; it abstains when R squared is under 0.3). Past 0.35 ATR it
  votes toward convergence, long when the ticker lags and short when it
  leads, halved at the session's extremes. It has weight 0 in every regime:
  it is recorded and scored, and moves nothing. With a reference set, the
  market-data tool fetches its quote and daily closes each cycle; without
  one, the channel abstains (`no_reference_feed`).

- **Protection for an entry that fills after its cycle.** The executor places
  an entry and its protection in one turn; an entry accepted but not yet filled
  when the turn ended left its approved stop and take-profit in prose, and the
  new shares sat unprotected until the next cycle. An entry's `record_trade` may
  now carry `protection_plan` (`stop_price`, `stop_qty`, `tp_limit_price`,
  `tp_qty`, gtc), kept in a new `protection_plans` table (migration 0028); the
  response says `plan_recorded` or `plan_problems`. With
  `protection_followup.enabled`, after a cycle that leaves a plan waiting, a
  follow-up checks every `interval_seconds` (300) for up to `max_minutes` (60)
  and, once the entry has filled, places its stop and take-profit for the
  shares still under no order. It only adds orders, never cancels one (a
  resting sell reserves its shares, and cancelling first leaves the position
  with no stop); it waits while a cycle runs and outside regular hours; it
  refuses, with a SYSTEM line in the trading handoff, when the stop breaks the
  constitution's cap from the fill, when the price is already through the
  stop, when every order needs approval in the console, or when the risk gate
  objects; it journals both orders against the entry. It also runs once at
  the start of every cycle, before the agents read the book, so an entry that
  filled before the open (a stop cannot rest then) is protected as the first
  regular-hours cycle starts; the two take turns, so a plan is placed once.
  Off by default: it places real orders without an agent.

### Added — off unless an algorithm version sets them

- **`composite.vote_scales`: a channel's full-vote magnitude.** The composite
  is a weighted mean of the channels' votes, and the call's author is the
  largest weight times vote, but the votes are not on one scale: a
  mean-reversion read is a few hundredths at mid-RSI by construction (its
  Bollinger and IBS parts are damped, and its trend guard decays a
  counter-trend read again) while a momentum read is a few tenths, so no
  weight short of most of the ensemble let mean reversion author a call.
  `composite.vote_scales` maps a channel to a scale in (0, 1]: its vote is
  divided by the scale and clipped to +/-1 before aggregation, and the raw
  emission stays in the vote's metadata (`raw_value`, `vote_scale`), as for
  an inverted channel. A read below the vote threshold stays a non-vote.
  Absent, every composite is unchanged.
- **`mean_reversion.trend_guard_decay`.** One setting, `trend_fade_decay`,
  did two jobs: it damped the Bollinger and IBS parts of the channel on every
  cycle, and it was the factor the trend guard applied to a vote against the
  daily stack. Raising it to let the guard speak louder also raised the two
  oscillators against RSI on every cycle, so the channel became a different
  mix rather than the same channel with a lighter guard. `trend_guard_decay`
  sets the guard's factor alone (0 to 1); unset, it is `trend_fade_decay` and
  every vote is unchanged. Votes record the factor their guard used
  (`trend_guard_decay`).
- **A `swing_failure_fade` channel, in shadow.** `swing_failure_reversal`
  fires long once a session low has held: higher lows after the flush and a
  close back above the flush bar's high. Nothing read the other side, so a
  session high that had failed went unrecorded, and a sell-leg firing of the
  VWAP stretch at the session high had no confirmation to wait for. The new
  channel is the same rule reflected: a flush high stretched above session
  VWAP, lower highs since, a close back below the flush bar's low and no new
  high on the last bar. It votes short, with the long leg's gate, strength and
  freshness, and records the daily MA stack it fired in (`stack_context`, read
  by `stack_mode`). It ships at weight 0 in every regime of the starter
  algorithm: it runs and its firings are recorded, but it does not move the
  combined signal until promoted on that record. The console calls it
  "Failed breakout".
- **`stack_mode` reads the moving-average stack where the regime classifier
  reads it.** Six channels read the daily stack as price above EMA9 above
  EMA21 and SMA20 above SMA50 (or the mirror). After a rally SMA20 stays above
  SMA50 for weeks, so a day falling through every fast average read "no
  stack" while the regime classifier, overriding the stale cross by where
  price sits, already tagged the tape bearish: the VWAP fade's with-trend
  sell leg, mean reversion's trend guard and both reclaim channels could not
  see the downtrend. `stack_mode: location` (on `intraday_vwap_zscore`,
  `mean_reversion`, `vwap_reclaim_fade`, `vwap_reclaim_continuation`,
  `trend_persistence` and `gap_fail_continuation`) reads bull above EMA9,
  EMA21 and SMA20 and bear below all three; `slow`, the default, is the old
  reading, so a version that does not set it is unchanged. Every vote records
  `stack_mode` and `sma_cross`, `trend_persistence` both readings
  (`bull_stack_slow`, `bull_stack_location` and the bear pair), and the
  regime classifier's override uses the same rule.
- **`intraday_vwap_zscore.session_extreme_ibs` and
  `session_extreme_dampener`.** The channel fades the distance from session
  VWAP and never asked where in the session's range the price sat: on a day
  that grinds lower it is below VWAP and at the session low every hour, and
  the buy leg fired each cycle while the dip was still extending. With a
  band set, the emission is scaled by the dampener when the buy leg fires
  with the session's IBS at or below the band, or the sell leg at or above
  one minus it. Votes record `ibs_at_compute`, `session_extreme_applied` and
  `extreme_factor`. Unset (band 0.0, dampener 1.0), every vote is unchanged.
- **`swing_failure_reversal.stretch_anchor_atr`.** The channel's strength was
  measured from its gate (`tanh((stretch - min_stretch_atr) / stretch_scale)`),
  so a confirmed reversal that just cleared the gate passed it, joined the
  voting pool and the participation count, and said next to nothing. Lowering
  the gate had not lowered the ramp. With the anchor set, the strength is
  measured from it instead (from 0 at 0.0); the gate still decides whether the
  channel fires. Unset, the anchor is the gate and every vote is unchanged.
  Votes record `stretch_anchor_atr`. Must lie between 0 and `min_stretch_atr`.
- **`momentum.fast_dissent_decay` and `fast_dissent_min_atr`.** The momentum
  channel's moving-average term reads the stack, which lags a reversal by days:
  after a long advance it keeps reading long through a fall. With a decay below
  1, when price is beyond EMA 9 by at least `fast_dissent_min_atr` daily ATRs
  and the daily MACD histogram is outside its neutral band, both against the
  moving-average read, that read is multiplied by the decay before the 60/40
  blend; at 0 the channel takes the MACD's sign. The default, 1.0, is off. The
  condition is recorded on every momentum vote (`fast_dissent`, and
  `fast_dissent_applied` when a decay changed the value), and the attribution
  record keeps it with the vote, beside `divergence_applied`, so the calls can
  be split by it.
- **A `vwap_reclaim_fade` channel, in shadow.** `vwap_reclaim_continuation`
  votes with the daily trend after an intraday dip through session VWAP is
  reclaimed. The new channel reads the same reclaim the other way: when it
  happened on light volume for the time of day, it votes against it (bearish
  in a bull stack, bullish in a bear stack), on the thesis that a reclaim
  without participation is the end of the bounce rather than the start of a
  move. It abstains at normal volume (relative volume 1.0 or more) and is at
  full strength at 0.6 or less. Both channels now find the dip and the reclaim
  with one shared function (`indicators/vwap_episode.py`), so they can disagree
  about what a reclaim means but never about whether one happened;
  `vwap_reclaim_continuation`'s output is unchanged. It ships at weight 0 in
  every regime of the starter algorithm: it runs and its firings are recorded,
  but it does not move the combined signal until promoted on that record. The
  console calls it "VWAP reclaim fade".
- **A `gap_fail_continuation` channel, in shadow.** The `gap` channel reads
  0.0 the moment an opening gap has filled, so nothing voted on what a failed
  gap does next. The new channel votes in the direction of the fade once price
  is through the prior close, past session VWAP and has held there for the
  last few 5-minute bars (bearish after a failed gap-up, bullish after a failed
  gap-down), from an hour into the session, fading to nothing over the last
  half hour. It ships at weight 0 in every regime of the starter algorithm, so
  it runs and its firings are recorded but it does not move the combined
  signal until promoted on that record. A version whose config predates a
  shadow channel runs it at weight 0 too.
- **`momentum.divergence_window_days` and `divergence_cum_atr`.** The
  divergence guard halves the momentum vote when the market moves hard
  against it, but it looked at one session, so a decline made of small days
  never tripped it. With both set, the move from the close N sessions ago is
  tested too, in daily ATRs, and either test applies the same single decay.
  The anchor session is found by date, so the span is the same whether or not
  the daily list includes today. The vote's metadata says which test fired
  (`divergence_basis`) and from which session (`divergence_window_anchor`).
- **`trend_persistence.counter_stack_scale` and `no_stack_scale`.** A grind
  fired only when the moving averages were stacked the same way, which after a
  long advance silences the channel's opposite side for weeks. With both set,
  the stack scales the vote (1.0 with it, the counter scale against it, the
  no-stack scale otherwise) instead of vetoing it; `leg` records which.

### Fixed

- **An activated instruction version is used from the next cycle.** The
  agents read their instructions once, when the app started, and activating
  a version (in the console, or by the evolution agent) only moved the active
  pointer: the running app kept the old text until a restart while the
  console reported the new version active. Every cycle now re-reads each
  agent's active version before it runs, so a version activated in between
  is live from the next cycle; an agent whose text did not change keeps its
  cached prompt. The cycle's log opens with the versions it runs on
  (`[runtime] instructions: strategy v…, executor v…`) and names any that
  changed, and the console's activation message says when the version takes
  effect.
- **A sell the agents placed and never recorded is journaled.** The order
  sync lists every working order at the broker and the coverage counts them,
  but an order the executor placed without recording it had no journal row:
  when it filled, there was nothing to mark filled, and the position check
  later closed the shares as a sync row. Now a sell the broker says the agents
  placed (`placed_agent` 'agentic'; never an order a person placed in the
  app) with no row for its id is recorded as the executor would have recorded
  it: a stop as STOP_LOSS, a limit sell above the lot's price as TAKE_PROFIT,
  any other sell as CLOSE, against the open lots with the oldest lot's
  signals, in the cycle that placed it, marked `ADOPTED_FROM_BROKER`. A
  working order is recorded once it is ten minutes old, so the executor's own
  record comes first; one that filled first is recorded at its fill, with its
  P&L. The order sync's result lists them under `adopted`.
- **A cycle's run record says when its strategy made no decision.** A cycle
  that reached the strategy stage read SUCCESS whatever the stage answered.
  Now one whose strategy made no decision reads FAILED with the reason, or
  SKIPPED under `on_exhausted: skip`. While the plan is spent under `skip` or
  `fail`, a cycle runs no agent at all: the book is reconciled as at every
  start, the market data and the algorithm's call are recorded (and
  attributed, flagged agent_absent), and no agent is billed for a cycle that
  cannot act.
- **A run left open by a stopped app is closed at the next start.** A cycle
  the app was in the middle of when it stopped (a forced stop, a crash)
  stayed RUNNING for good. At start-up each such run is closed FAILED,
  saying when it started and when it last recorded anything, with a
  `[runtime]` row in its log. A cancelled cycle (the console's cancel, or a
  stop) reads CANCELLED instead of FAILED.
- **The strategy agent's API fallback no longer repeats itself.** When the
  hosted strategy agent could not run (its subscription plan spent, or the
  harness failing), it handed the cycle to its API fallback from inside the
  orchestrator's `strategy` tool call. There, nothing added the fallback's own
  steps to the history it builds each model request from, so every request
  was its first one again: it called its first tools over and over, billed per
  token, without answering, until the app was stopped or the cycle cancelled.
  None of it reached the thought log, so the cycle read as though no fallback
  had run. The fallback now works on a private copy of the cycle's history
  with its steps added as it goes; its thoughts and tool calls are in the
  thought log, a `[runtime]` row says when and why it took over, and the
  `strategy` tool returns its answer, not its steps. The whole strategy stage
  stops after `max_run_seconds` (new, default 1200, harness and fallback
  together), and the fallback after `fallback_max_model_calls` (new, default
  25) model calls without an answer; either way the stage answers "no
  decision", never silence. The harness's own limit notice ("You've hit your
  session limit …") counts as a failed run, not a proposal. The defaults need
  no change in your settings.
- **`quota.on_exhausted` applies to the strategy agent.** Only the evolution
  run read it: a spent plan always sent the strategy stage to the API. `api`
  still does that and is still the default; `skip` and `fail` now make no
  decision until the window resets and call no API. Under them a plan near
  `warn_utilization` keeps answering on the harness, since there is nowhere
  else to send it. The policy is logged at start-up.
- **A filled stop or take-profit closes the lots it sold, with its P&L.** A
  resting stop is one journal row against the lot it was placed for, and when
  the broker reported it filled, that row alone was marked filled. A stop
  larger than its lot closed more shares of that lot than it held, the next
  lot's shares stayed open in the journal until the day's position check
  closed them as a sync row (at its own price, outside the decision P&L), and
  only rows labelled CLOSE were given a realized P&L there, so a stop or
  take-profit that filled from rest had none at all. Now the fill is spread
  over the open lots, oldest first: one row per lot, each with its own P&L
  (the added rows are marked `FIFO_SPLIT_OF <row>`), and a row whose lot was
  already sold moves to the next. A stop's or take-profit's P&L waits for the
  broker's fill price rather than using its trigger or limit. Rows already in
  the journal are not changed.
- **A failed read of the broker's orders is no longer taken for their
  absence.** Reconciliation marked an order CANCELLED ("not found on broker")
  whenever no lookup returned it, and a lookup that failed returned nothing,
  the same as one that found nothing: working protective orders were written
  off, the coverage read `none`, and the agents cancelled the real orders to
  repair a gap that did not exist. Now an order is gone only when a broker
  list that answered, holds orders and reaches back to the order's placement
  does not have it; the whole account's list is asked once when a symbol's
  list fails or comes back empty. Anything less leaves the order PENDING and
  counted as resting: reconciliation reports it in `unverified`, and the
  coverage block reads `unverified` (never `none`) with the order ids and a
  note. `order_book.listing_status` says per ticker whether the list
  answered, and a broker error is logged with its text.
- **A scheduled run waits instead of vanishing.** The scheduler marked a
  minute done before checking whether another task was running, so a cycle
  due while the evolution run was still running was logged "skipped" and
  never retried, and a cycle and the evolution run due at the same minute
  both started, side by side; and it matched only the current minute, so a
  minute the loop never saw (a stalled loop, a host asleep across it) never
  fired at all. Now a due run waits for the running task and starts inside a
  grace window (20 minutes for a cycle, 2 hours for the evolution run, 6 for
  the metrics job), a missed minute starts late, and after a long gap only the
  latest due minute starts, never a backlog. At a shared minute the trading
  cycle goes first and the evolution run follows it; a scheduled cycle is
  claimed before it is started, so two cannot start in one poll. A cycle or an
  evolution run that cannot start inside its grace (another task ran too
  long, or the app was stalled or asleep past it) is recorded as a SKIPPED run
  with the reason, and shows in the run history; a late cycle records in its
  own log when it was due and why it was late. The settings warn when
  `evolution_cron` can fall on a cycle's minute.
- **Runs that time out, are cancelled or are skipped keep that status.** The
  run table accepted RUNNING, SUCCESS and FAILED only, so an evolution run
  that timed out, was cancelled or was skipped for an exhausted subscription
  quota failed its final update and stayed RUNNING. The table is rebuilt with
  every row kept (migration 0027) and also takes SKIPPED, CANCELLED and
  TIMED_OUT; the console shows Skipped and Cancelled badges.
- **The console's files are checked for a newer version on every load.** The
  page names its script modules by fixed paths, and the console sent no
  caching instruction, so a browser reused files it had fetched before for
  hours by its own estimate. After the previous update, a browser that had the
  console open ran the new `js/app.js` with its old `js/api.js`, which dropped
  the chosen instrument from the chart request: the market panel's picker went
  back to the primary every time it was changed. Every console file, the page
  included, is now sent with `Cache-Control: no-cache` (the browser asks each
  load; an unchanged file costs a 304), and the page loads `js/api.js` under a
  new address through an import map, so a browser holding the old copy fetches
  the new one on an ordinary reload, even before the console is restarted.
- **The loss streak counts decisions, not lots.** A sale of a position held as
  several lots is one broker order written as one row per lot, and the streak
  walked rows: one losing sale of four lots read as four consecutive losses,
  enough with one more loss to start the constitution's pause after two
  decisions, and the performance analysis reported the same run. The streak
  behind the pause (`get_consecutive_losses`, `get_session_consecutive_losses`)
  and the analysis's `streak_analysis` now count closing decisions: a real
  broker order with all its rows, else the row itself, lost when its rows sum
  below zero, the grouping the closing-orders count already used.
- **An inverse fund's readings no longer mix into the primary's.** When the
  strategy agent expresses a bearish view through the primary's inverse fund,
  it runs the market-data tool on the fund too, which stores a snapshot with
  the fund's own signal, read on the fund's prices: roughly the mirror image of
  the primary's. Trading decisions never read stored snapshots, but the console
  did: the market panel's chart drew both in one line and zigzagged between a
  reading and its mirror image. The panel now shows one instrument, picked from
  a list beside its title (the primary first and by default, then every other
  instrument with readings), and another instrument's live reading no longer
  lands on it. The account chart's daily signal uses the primary on any day it
  was read. The attribution backfills take the channel votes and participation
  from the row's own instrument, and a cycle with no row gets the primary's
  snapshot. `log_signal_attribution` refuses a row under the primary's inverse
  fund (`asset.inverse_ticker`, or a negative `leverage` in
  `asset.instruments`) and says how to log it under the primary: the scorer
  measures a row on its own ticker's next-day move, so a right bearish call
  logged under the fund reads as wrong. Rows already logged under the fund are
  not changed; to correct one, move it to the primary at the primary's price,
  with its directions in the primary's terms, and clear its forward returns.
  `/api/chart/tech` takes `ticker` and answers with `ticker` and `tickers`;
  `scenarios.from_history.load_snapshots` takes `ticker`.
- **A stop and a take-profit that split a position are not a protection gap.**
  A resting order reserves its shares, so a take-profit on part of a position
  and a stop on the rest are the only way to have both. The post-cycle audit
  counted the take-profit's shares as unprotected and reported a gap every
  cycle, which invites the next agent to cancel the take-profit to "repair"
  it. When the stop and resting take-profits between them hold every share,
  `protective_coverage` now reads `split` (with `tp_only_qty`, the shares
  that have no stop, and `no_order_qty`, the shares under neither) and the
  audit logs it instead of reporting it. Shares under neither order, or a
  take-profit with no stop at all, are still a gap.
- **The signal strip under the account chart fits its card.** Its chips could
  not wrap, so the correlation chip ran past the card's edge whenever the card
  was narrow (a phone, or the account panel beside the others on a desktop);
  the words now wrap inside the chip.
- **One broker order is one journal entry, however often it is recorded.** The
  executor records an order when it places it and again after re-checking it,
  and each call wrote rows: a sale of part of a lot became two exits, a later
  status update made both FILLED with the sale's full realized P&L, the reports
  counted one loss twice, the lot read below zero, and reconciliation added
  shares at the broker's average cost to make up the difference. A second
  `record_trade` for an order already in the journal now updates it instead: a
  fill with its price promotes the waiting row, a cancellation ends it, anything
  else changes nothing, and the tool's response says `deduplicated`. Copies
  written before this are left out of every realized-P&L figure (the daily-loss
  and losing-streak limits included); `get_performance_summary` and the
  performance analysis report how many (`duplicate_order_rows`), and the app
  names them in its log at every start (`JOURNAL DUPLICATE`) until they are
  repaired to the status `DUPLICATE`. `get_performance_summary` also reports
  `closing_orders`, the broker orders behind its rows.
- **A fill confirmed for an order split across lots keeps each lot's
  quantity.** The status update wrote the order's whole quantity onto every row
  sharing its id, so a sale confirmed after it was recorded could close its
  quantity once per lot.
- **The protection audit compares whole positions, not lots.** After every
  cycle the audit checks that each held position is covered by a resting stop.
  It compared each purchase lot on its own with the ticker's whole stop, so a
  position bought in two lots under a stop sized for the first passed: each lot
  alone fit. It now sums the lots per ticker, leaves option lots out
  (contracts, not shares), and names every lot it summed (`trade_ids`).
- **Shadow channels no longer move the combined signal.** The combined signal
  is scaled down when few channels corroborate it: below 0.4 of the channels on
  duty voting, it is multiplied by that share divided by 0.4. The count
  included channels at weight 0, which is how a new channel runs before it is
  promoted, so a silent shadow channel counted as an absent witness and a
  voting one as a corroborating witness. The combined signal then moved from
  one hour to the next with every weighted input unchanged: with the starter
  algorithm's three shadow channels in scope during regular hours, two
  weighted votes read 2 of 8 and were scaled by 0.625. Only channels with
  weight in the current regime are counted now. **Regular-hours readings are
  higher than before** wherever a weight-0 channel was in scope (the scale it
  caused no longer applies), so compare readings across this change with care.
- **A channel still collecting its session bars counts as on duty.** A channel
  that needs 20 five-minute bars has 12 at 10:30 ET; it was left out of the
  count until it had them, so the same votes were scaled differently at 10:30
  and 11:30. A channel that will have its bars before the close now reports
  `warming_up` and is counted (`swing_failure_reversal`,
  `vwap_reclaim_continuation`); one that cannot have them, such as 20 one-hour
  bars in a 6.5-hour session, stays out as before.
- **The scale can be checked from the cycle's own record.** The market-data
  payload reports `participation_numerator` and `participation_denominator`
  next to `participation_scale`, and the combined signal's metadata names the
  channels warming up (`warming_up_signals`).
- **The engine fingerprint covers `algorithms/units.py`,** the move-size rules
  six strategies share. A change to it would have changed readings without the
  engine-change notice.
- **The attribution record carries the participation scale and its counts.**
  `signal_attribution` has had `composite_unattenuated` and
  `participation_scale` columns since migration 0024, but only the strategy
  agent wrote the row and it was never asked for them, so they stayed empty.
  After each cycle they are now filled, with `participation_numerator` and
  `participation_denominator` (migration 0025), from the market-data response
  the cycle keeps in its log. Past rows are filled the same way on the first
  cycle after the update, where the response has them.
- **The option chain reaches the money.** The broker lists an expiry's contracts
  from the lowest strike up, a page at a time, and `gather_option_chain` read the
  next-page link from one place only and gave up after five pages. When an
  underlying has a long run of strikes below its price, the near-the-money
  contracts fall past what was fetched, and the nearest-strike selection then
  mapped every offset onto the one highest strike it had: the strategy agent saw
  a chain of one strike far below the price, cycle after cycle, and concluded
  options were unavailable. The link is now found wherever the server puts it
  (beside the list, at the top level, or as a bare cursor), pages are followed
  until strikes on both sides of the money are in hand (40 at most), and the
  response says in `errors` when the list stops short: a page that could not be
  fetched, a single strike, or no strike within 8% of the price. It also reports
  what was fetched (`strikes_fetched`: pages, whether more remained, the lowest
  and highest strike).
- **Practice mode passes read-only market data through to the broker.** A
  practice cycle on 2026-10-02 was refused `get_earnings_calendar`,
  `get_equity_price_book`, `get_option_historicals` and `get_equity_tax_lots`
  ("not supported in simulation mode"), so the earnings event context, the
  order-book depth check and the IV trend ran blind in practice mode while they
  work live. Every read-only tool that describes the market rather than an
  account now passes through: quotes, bars, order-book depth and indicators;
  option chains, contracts and their quotes and bars; fundamentals, financials,
  earnings dates and results; indexes; instrument search. Tools that read the
  real account are still answered by the simulated broker or refused, never
  passed through: `get_equity_tax_lots` now answers with the practice account's
  own position as one open lot (it records no closed lots), so the wash-sale
  check runs instead of failing. The tradability check's account swap read the
  broker's account list in the wrong shape and so sent the practice account's
  name to the real broker; it now finds the real account as the live path does.
  From the same audit of the option path: a practice option position is priced
  per contract with its multiplier, as the broker prices one (the position sync
  read it at a hundredth of its cost), and a close placed after the agents'
  cache had lost the contract's ticker closes the held position instead of
  opening a second one under "OPTION".
- **The constitution's account limits are enforced in code.** `max_weekly_loss_pct`
  and `max_drawdown_pct` had no enforcing code, and the daily-loss limit, the
  loss-streak pause and the daily order count were checked only inside a tool
  the risk-manager agent is asked to call. All of them are now one verdict
  (`callbacks/account_rails.py`), applied by `check_risk_limits`,
  `check_option_risk_limits` and the pre-order gate in front of the executor's
  order tools, which refuses a new entry past any of them. The drawdown limit
  measures the account against the highest value the metrics job has recorded.
  Exits and protective orders are never blocked. `get_open_positions` reports
  the account's standing against each limit (`account_rails`) every cycle,
  from the journal and the last recorded account value, so a halt is visible
  before an order is tried. A limit that cannot be measured
  (no account value, no recorded peak) is reported as not evaluated.
- **The daily indicators include yesterday's close.** The broker's daily bars
  end at the previous session all day long, and the live price was written
  over that last bar, so every daily reading (moving averages, MACD, RSI,
  Bollinger bands, ATR, IBS, the regime and its ADX) ran without the most
  recent completed session, and ATR stayed frozen whenever the day traded
  inside yesterday's range. Today's bar is now added after yesterday's
  instead: built from the session's 5-minute bars, or from the live price
  alone before the open. Relative volume and the daily VWAP still come from
  the last completed session. A bar with no range yet gives no IBS reading
  (`ibs_source: forming_bar_no_range`) rather than placing today's price in
  yesterday's range, and after the close the gap is read from today's bar
  (it was never found: the bar's date was read a day early). Each snapshot
  records `daily_bar_mode` and `daily_last_completed_date`. **Every daily
  reading shifts from the first cycle that runs this version**, so compare
  signals from before and after it separately; the engine fingerprint changes
  with it. Backtests are unaffected: they read the previous day's completed
  bars only.
- **Algorithm versions show when they were proposed.** Every version read
  "Proposed: Unknown date": saved versions carried no date, and the console's
  fallback to the evolution log called a method that does not exist, inside an
  error handler that hid it. Versions now record their date when saved, the
  fallback works, and dates are sent with their time zone so every browser
  shows the same time. The same lookup now also picks the version a proposal
  is compared against (`diff_against` in the API): the one it was proposed
  from, or its recorded parent, rather than whichever folder sorts before it.
- **Parsing JSON whose text contains a bracket.** A `]` inside a string value,
  such as the interval "(0,5]", closed the top-level array early and the parse
  failed at an unrelated position. Valid JSON is now parsed as it is, and the
  fallback extraction ignores brackets inside strings.

### Changed

- **Telemetry keeps 30 days by default** (`logging.telemetry_retention_days`;
  0 keeps everything). `telemetry.db` holds the full text of every model call
  and nothing ever removed it, so it grew without limit. Older records are
  now deleted at each start. The file is not compacted then (that needs the app stopped), so it
  stops growing rather than shrinking; to reclaim space now, run SQLite's
  `VACUUM` on it with the app stopped. The console's all-time token view covers
  the retained days only.

- **The momentum vote says when its MACD reading is noise.** It now reports the
  daily MACD histogram in ATRs (`macd_hist_atr`) and whether it is inside a
  neutral band of 0.10 ATR (`macd_neutral`, with the band as
  `macd_neutral_atr`). A histogram of 0.05 ATR makes the MACD part of the vote
  read -0.14, which an agent can mistake for dissent on a rising day. Reported
  only: the vote itself is unchanged.
- **Thresholds on the size of a price move can be set in the instrument's own
  units.** Five thresholds were in percent of price, which silently tunes them
  to one instrument: 0.7% is two thirds of a normal day on SPY and a tenth of
  one on a volatile single stock, where the gate then fires on noise. Each now
  has an optional form in daily ATRs (average true range, the typical size of
  a day's move): `gap.min_gap_atr` and `gap_fade_threshold_atr`,
  `range_break_continuation.min_break_atr`, `trend_persistence.counter_day_atr`
  and `event_window_timing.min_overextension_atr`. All five use one shared rule
  (`algorithms/units.py`), and each strategy records which basis decided.
- **The starter algorithm (`v001_initial`) uses the ATR forms**, set to match
  the old percent values on SPY, so on SPY it behaves as before and on another
  ticker the thresholds scale with it. **If you run your own algorithm
  version, nothing changes:** the ATR forms default to off, and a version that
  does not set them keeps its percent thresholds. To opt in, add them to that
  version's `config.yaml`.

- **Every cycle sees the latest position sync.** The daily sync runs between
  trading cycles, so what it found reached no cycle's record. It now keeps its
  latest result in the data folder (`trading/last_position_sync.json`), and
  `get_open_positions` shows it as `last_position_sync`: when it ran, its
  status and its cost-basis check.

### Fixed

- **The gap signal can vote at the first cycle of the session.** That cycle
  runs seconds after the open, before the first 5-minute bar exists, so the
  opening gap was unreadable exactly when a gap fade is still ahead, and read
  an hour later, after the gap had filled. In the session's first 15 minutes,
  while there is no bar yet, the gap is now read from the quote
  (`gap_source: quote_at_open`). The starter algorithm gives the gap signal no
  weight, so this makes it observable without changing any trade.
- **A swing-failure vote ages from when the reversal was confirmed**, not from
  the low: from the first close above the flush bar's high, and never from
  earlier than `min_confirm_bars`. A reversal that reclaims at once reads as
  before; one that chopped below the flush high first is no longer discounted
  for that wait. Its votes now report `bars_since_reclaim` and
  `bars_since_confirmed`.
- **The daily position sync no longer rewrites a lot's price to the
  position's average.** After a buy at a different price, each lot differs
  from the broker's average cost by construction, and the sync rewrote such
  lots, broker-confirmed prices included, on every run: each lot's unrealized
  P&L moved, and the difference would have been booked as realized P&L when
  the lot closed. A lot is now checked only against its own order's fill
  price. The lots' average is still compared with the broker's, but a
  disagreement is only reported (`cost_basis_check.sources_disagree` on the
  sync result), never written. A pending buy the sync marks filled takes its
  own order's price too. Lots the old check rewrote are put back from their
  orders on the next sync.
- **A cancelled good-till-cancelled order is labelled cancelled, not
  "expired".** The label was decided by when an order was placed, so a gtc
  stop cancelled to resize it days later read "Day order expired ... (no
  time_in_force)" — the message that once led an agent to sell a position
  instead of re-placing its stop. A day order now reads expired only when the
  broker dropped it at or after its session's close. Wrong labels already
  written stay until corrected by hand.
- **Rows a data repair closed out keep the repair's label** when later news
  arrives for the broker order they share.
- **A stop resized to cover a buy placed in the same cycle** is no longer
  flagged OVER_COVER while that buy awaits the broker's confirmation.

## 0.2.0 — first public release — 2026-09-26

The first public version. Development before this happened in a private
repository, and that history is not included.

The project was called **Gold Digger** in private development; it was renamed
EvoTrader for this release. If you ran a private copy: the package is now
`evotrader` (was `gold_digger`), environment variables start `EVOTRADER_`
(were `GOLD_DIGGER_`), the databases are `evotrader*.db` (were
`gold_digger*.db`), the broker sign-in cache is `~/.evotrader/` (was
`~/.gold_digger/`), and strategy module paths in `algorithms/*.yaml` start
`evotrader.`. Rename those once, with the app stopped.

### What's in it

- **Trading cycle.** A team of agents built on Google's Agent Development Kit:
  an orchestrator, a news agent, a strategy agent, a risk manager and an
  executor. Each agent's model is set in `settings.yaml`; Google Gemini is the
  default, Anthropic Claude is supported, OpenAI is experimental.
- **Algorithm.** A composite engine that combines ten rule-based strategies with
  weights that depend on the market regime, lets a strategy abstain without
  diluting the others, and keeps its parameters in numbered algorithm versions.
- **Hard limits.** A constitution the agents cannot change, enforced by the
  risk manager's checks and by a risk gate in code on every order; protective
  stop orders are forced to good-till-cancelled; exits are never blocked by
  rules meant for new positions.
- **Approval gate.** Optionally, every order waits for your approval in the
  web console.
- **Practice mode**, the default: a simulated broker that fills orders against
  live Robinhood quotes, with its own journal and memory.
- **Robinhood** through its official MCP server, with sign-in on Robinhood's
  own page; the app never sees your password.
- **Signal attribution.** Every cycle records what the algorithm, the news and
  the strategy agent each said, and scores them against the price one and five
  trading days later.
- **Evolution agent**, on a schedule you set. It writes strategy proposals, code
  reviews, carry-forward notes, and new algorithm and instruction versions that
  stay inactive until you approve them. It can run on the API or on a Claude
  subscription through Claude Code, as can the strategy agent.
- **Skills for coding agents.** `self-evolve` for implementing approved
  proposals with tests, plus `signal-validation`, `offline-iteration`,
  `mcp-debugger` and `fresh-start`.
- **Web console.** Dashboard, trade approval, the reasoning of every cycle,
  token usage, review of proposals and versions, and your notes to the agents.
- **Your own data folder**, separate from the code (`EVOTRADER_DATA_DIR` or
  `--data-dir`), created from `starter_data/` by the first-run setup
  (`scripts/setup.sh`, `./run.sh setup`).
- **Offline tools.** An algorithm-only backtester and a scenario harness for
  testing agent instructions.
- **Cost controls.** Prompt caching for Anthropic models, trimming of oversized
  data-provider responses, and per-agent model choice.
- Apache License 2.0, continuous integration, and contributor documentation.

### Fixed before release

Fixes from trying the first public version the way a newcomer would.

### Changed — check these if you set things up by hand

- **Your shell now wins over `.env` files.** A variable already set in the
  environment beats the same variable in a `.env` (the data folder's `.env`
  still beats the project's), and an empty value (`KEY=`) means "not set".
  Before, the data folder's `.env` won, so `EVOTRADER_PORT=8081 ./run.sh` was
  ignored when the file named a port, and an empty `EVOTRADER_MOCK_TIME=`
  cancelled `--mock-time`. The app warns when the two differ.
- **An option the app doesn't know stops it** with an error. It used to be
  ignored, so a typo such as `--mock_time` ran in the mode the settings say.

### Backtesting

- **`--validate`** judges a backtest instead of just scoring it: the
  statistical checks in `backtest/validation.py`, a placebo (the version's own
  signal shifted in time so it predicts nothing, replayed 40 times) and a
  positive control (a signal that cheats by reading the next bar, which must
  win, or the harness cannot see an edge on that data). The report gains a
  Validation table and a verdict; exit code 3 means the run worked and nothing
  survived. `--variants-tested N` raises the bar for the number of things tried.
- **`--daily-csv`** joins `--intraday-csv`: with both, a backtest needs no
  network and works for any instrument you have bars for.
- **A strategy can live outside the package**: name its module in the
  manifest and put it on `PYTHONPATH`. The new `backtest` skill walks an agent
  through testing an idea end to end.
- **The loader warns when a strategy has a weight but no entry in
  `composite.regime_weights`.** With regime weighting on, such a strategy
  counted as 0 in every regime: it ran and looked active, but never moved a
  trade.
- The out-of-sample check no longer fails when trade times and the split date
  differ in having a time zone (it raised an error on a run with no trades).

### Setup

- **Easier to read.** Each part of `./run.sh setup` has a heading, choices and
  defaults stand out, a key that works gets a green tick, and long
  explanations are broken at the terminal's width (a command stays on one
  line, to copy). No colour in a pipe or a log, or when `NO_COLOR` is set.
  `scripts/setup.sh` colours its checks too.
- **The Claude subscription is mentioned where the API key is asked for.**
  API keys are paid per use; the strategy and evolution agents, which cost
  the most, can run on a Claude Pro or Max subscription instead. Easy mode now
  says so and sets nothing up: to switch later, run `./run.sh setup` again and
  choose Detailed. Detailed mode says the same when Claude Code isn't
  installed (with it installed, it asks).

### Skills

- All skills now live in `.agents/skills/`, the shared folder of the Agent
  Skills standard (GitHub Copilot, Cursor, OpenCode and others read it), with
  `.claude/skills` a link to it for Claude Code; the `skills/` folder and its
  duplicate of `self-evolve` are gone.
- The instructions for coding agents are in `AGENTS.md`, the cross-tool
  standard; `CLAUDE.md` imports it for Claude Code.
- `fresh-start` is removed: it deleted journals, memory and the broker
  sign-in. To start over, point the app at a new data folder
  (`./run.sh setup --data-dir PATH`).
- The MCP debugger refuses tools that place, change or cancel orders (they go
  through the app's risk checks and approval), and refuses to run while the app
  runs (they would share the broker sign-in). `scripts/discover_mcp_tools.py`
  is now its `dump` command; two stale helper scripts are removed.

### Fixed

- Ctrl+C stops the app within seconds, even with a console tab open; it used
  to hang until killed.
- `./run.sh setup --data-dir PATH` sets up that folder (it set up `./data`),
  and a relative folder is found where you typed it, not inside the project.
- Quitting setup takes away the data folder it had just made, so the app no
  longer starts from a half-made folder with no password and no key. The app
  now also warns at start-up when the console password or an AI key is missing.
- Running setup again starts from your current choices (easy or detailed, the
  provider, each agent's model, the port): pressing Enter throughout changes
  nothing.
- Setup checks a new AI key with its provider (a free, read-only request), so
  a mistyped key shows during setup instead of at the first trading cycle.
- Setup recognises a Gemini key saved as `GOOGLE_API_KEY`, queries model names
  such as `gpt-5` that lack a provider in front, and asks for at least $1 of
  play money.
- The Robinhood sign-in files are readable only by you, and no part of the
  token is printed any more. The tests never touch your real `~/.evotrader`.
- A console port already in use gets a clear message; `--sim-deposit` checks
  the amount; `./run.sh cli-dashboard` works again; the console's sign-in window
  can be closed; start-up and shutdown print far less noise.
- Only one EvoTrader runs on a data folder, and on a broker sign-in: a second
  copy stops with a message instead of trading the same account.
- A second Ctrl+C no longer hangs while a broker sign-in is pending, and a
  third stops the app at once.
- `./run.sh offline` shows the Robinhood sign-in link (it was held back in a
  buffer, so a first sign-in from offline mode was impossible).
- After **Refresh Broker Token** in the console, the next sign-in is saved
  again; it failed until a restart. A sign-in that times out prints one line,
  not a hundred.
- Without a console password, the console no longer waits minutes on
  "CONNECTING", and its portfolio doesn't wait on a pending sign-in.
- Setup keeps practice mode's own models (`model: sim`) unless you say
  otherwise, says truthfully which agents stay on a Claude subscription and
  offers to move them, uses a key already exported in your shell, and doesn't
  treat a hand-made `.env` as a detailed setup.
- Unreadable `settings.yaml` or `--mock-time` values, and mistyped options, get
  a plain message with a suggestion instead of a traceback. The backtester
  replays your data folder's algorithm version (it named a private one), and
  advice from setup, the app and the tools names your data folder.

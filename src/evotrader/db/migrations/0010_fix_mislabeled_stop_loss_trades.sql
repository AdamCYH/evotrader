-- Migration 0010: Fix trades mislabeled as OPEN/SHORT when they were
-- actually stop-loss orders protecting long positions.
--
-- Root cause: record_trade's Phase 2 mapped side="sell" → direction=SHORT
-- before Phase 3 could infer the correct classification from open positions.
-- This resulted in stop-loss sell orders being stored as action=OPEN,
-- direction=SHORT instead of action=STOP_LOSS, direction=LONG.
--
-- Detection: Match on order_type being a stop variant, OR reasoning text
-- containing stop-loss intent indicators (STOP_MARKET, stop $, stop-loss,
-- protective stop, etc.)

UPDATE trades
SET action = 'STOP_LOSS', direction = 'LONG'
WHERE action IN ('OPEN', 'CLOSE')
  AND direction = 'SHORT'
  AND (
    order_type IN ('stop', 'stop_limit', 'trailing_stop')
    OR reasoning LIKE '%stop loss%'
    OR reasoning LIKE '%Stop loss%'
    OR reasoning LIKE '%Stop Loss%'
    OR reasoning LIKE '%stop_loss%'
    OR reasoning LIKE '%stop price%'
    OR reasoning LIKE '%STOP_MARKET%'
    OR reasoning LIKE '%stop_market%'
    OR reasoning LIKE '%protective stop%'
    OR reasoning LIKE '%Protective stop%'
    OR reasoning LIKE '%protective STOP%'
    OR reasoning LIKE '%Consolidated protective%'
    OR reasoning LIKE '%stop $%'
    OR reasoning LIKE '%Stop $%'
  );

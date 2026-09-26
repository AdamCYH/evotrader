/* ═══════════════════════════════════════════════════════════════════════
   EvoTrader — Technical Indicator & Portfolio Chart Module (ES6 Module)
   ═══════════════════════════════════════════════════════════════════════ */

import { etLong, etShort, signalLean } from "./components/signal_display.js";

/** Dot colour for a combined-signal reading, on the strategy's own thresholds. */
function leanColor(value, regime) {
    const tone = signalLean(value, regime).tone;
    if (tone === "bull") return '#15803d';
    if (tone === "bull-soft") return '#4ade80';
    if (tone === "bear") return '#b91c1c';
    if (tone === "bear-soft") return '#f87171';
    return '#0369a1';
}

export class TechnicalChart {
    constructor(canvasId) {
        this.canvasId = canvasId;
        this.chart = null;
        this.mode = 'signals'; // 'signals' (market panel) or 'portfolio' (equity curve)
    }

    /**
     * Initialise the Chart.js line plot with custom warm theme styling
     */
    init() {
        const canvasEl = document.getElementById(this.canvasId);
        if (!canvasEl) {
            console.warn(`Canvas element with ID '${this.canvasId}' not found.`);
            return;
        }

        const ctx = canvasEl.getContext('2d');
        this.chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: Array.from({ length: 20 }, (_, i) => i + 1),
                datasets: [
                    {
                        label: 'RSI Trends',
                        borderColor: '#0369a1', // Muted slate blue/cyan
                        backgroundColor: 'rgba(3, 105, 161, 0.05)',
                        borderWidth: 2,
                        pointRadius: 3.5,
                        pointBackgroundColor: '#0369a1',
                        tension: 0.35,
                        data: Array(20).fill(50),
                        yAxisID: 'y',
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false }
                },
                scales: {
                    x: {
                        grid: { color: '#e3dfd0' }, // Tan divider lines
                        ticks: { color: '#6b6b66', font: { size: 9 } }
                    },
                    y: {
                        type: 'linear',
                        position: 'right',
                        min: 0,
                        max: 100,
                        grid: { color: '#e3dfd0' },
                        ticks: { color: '#0369a1', font: { size: 9 } }
                    }
                }
            }
        });
    }

    /**
     * Add a single new data point to the active chart view
     */
    updatePoint(rsiValue, compositeSignal = null, meta = null) {
        if (!this.chart) return;
        
        if (this.mode === 'signals') {
            if (compositeSignal === undefined || compositeSignal === null) return;
            const dataset = this.chart.data.datasets[0];
            const regime = meta && meta.regime && typeof meta.regime === 'object'
                ? meta.regime.regime : (meta ? meta.regime : null);
            const ts = (meta && meta.timestamp) || new Date().toISOString();
            // Labels, values and per-point context move together; pushing a
            // value alone shifted every label one reading off.
            dataset.data.push(Number(compositeSignal));
            this.chart.data.labels.push(etShort(ts));
            this.signalMeta = [...(this.signalMeta || []), { ts, regime }];
            while (dataset.data.length > 20) {
                dataset.data.shift();
                this.chart.data.labels.shift();
                this.signalMeta.shift();
            }
            const colors = dataset.data.map((v, i) => leanColor(v, (this.signalMeta[i] || {}).regime));
            dataset.pointBackgroundColor = colors;
            dataset.pointBorderColor = colors;
            this.chart.update();
            return;
        }
    }

    /**
     * Set chart view to Algorithm Composite Signals History
     */
    showSignalsContext(techPoints = []) {
        if (!this.chart) return;
        this.mode = 'signals';

        // Market time with the day: the viewer's own clock showed the 08:30 ET
        // cycle as "05:30" and ran several days together with no date.
        const valid = (techPoints || [])
            .filter(pt => pt.composite_signal !== undefined && pt.composite_signal !== null)
            .slice(-20);
        const dataPoints = valid.map(pt => Number(pt.composite_signal));
        const labels = valid.map(pt => etShort(pt.timestamp));
        this.signalMeta = valid.map(pt => ({ ts: pt.timestamp, regime: pt.regime }));
        const n = dataPoints.length;

        // Dot colour follows the same lean as the panel's badge (strong, leaning, neutral).
        const pointColors = dataPoints.map((v, i) => leanColor(v, this.signalMeta[i].regime));

        const dataset = this.chart.data.datasets[0];
        dataset.type = 'line';
        dataset.data = dataPoints;
        dataset.label = 'Composite Signal';
        dataset.borderColor = '#0369a1';
        dataset.backgroundColor = (context) => {
            const ctx = context.chart.ctx;
            const gradient = ctx.createLinearGradient(0, 0, 0, 160);
            gradient.addColorStop(0, 'rgba(21, 128, 61, 0.15)');
            gradient.addColorStop(0.5, 'rgba(3, 105, 161, 0.05)');
            gradient.addColorStop(1, 'rgba(185, 28, 28, 0.15)');
            return gradient;
        };
        dataset.fill = true;
        // One reading per cycle: straight segments, no invented curve between them.
        dataset.tension = 0;
        dataset.pointBackgroundColor = pointColors;
        dataset.pointBorderColor = pointColors;
        dataset.pointBorderWidth = Array(n).fill(0);
        dataset.pointRadius = Array(n).fill(4);
        dataset.pointStyle = Array(n).fill('circle');

        // Compute adaptive dynamic Y-axis range based on active values
        const maxAbs = Math.max(0.02, ...dataPoints.map(v => Math.abs(v)));
        let yLimit = 0.15;
        if (maxAbs > 0.50) yLimit = 1.0;
        else if (maxAbs > 0.30) yLimit = 0.60;
        else if (maxAbs > 0.18) yLimit = 0.40;
        else if (maxAbs > 0.08) yLimit = 0.25;
        else yLimit = 0.15;

        const isFineScale = yLimit <= 0.25;

        this.chart.data.labels = labels;
        // A handful of level labels reads; twenty rotated ones do not.
        this.chart.options.scales.x.ticks = {
            ...this.chart.options.scales.x.ticks,
            autoSkip: true, maxTicksLimit: 6, maxRotation: 0, minRotation: 0,
        };
        this.chart.options.scales.y.position = 'right';
        this.chart.options.scales.y.min = -yLimit;
        this.chart.options.scales.y.max = yLimit;
        this.chart.options.scales.y.ticks.color = '#0284c7';
        this.chart.options.scales.y.ticks.font = { family: 'JetBrains Mono', size: 10 };
        this.chart.options.scales.y.ticks.callback = function(value) {
            if (Math.abs(value) < 0.0001) return '0.00';
            return (value > 0 ? '+' : '') + value.toFixed(isFineScale ? 2 : 1);
        };

        const chartRef = this;
        this.chart.options.plugins.tooltip = {
            callbacks: {
                title: function(items) {
                    const meta = (chartRef.signalMeta || [])[items[0].dataIndex] || {};
                    return meta.ts ? etLong(meta.ts) : '';
                },
                label: function(context) {
                    const val = Number(context.raw || 0);
                    const meta = (chartRef.signalMeta || [])[context.dataIndex] || {};
                    return `Signal ${(val >= 0 ? '+' : '') + val.toFixed(4)} — ${signalLean(val, meta.regime).label}`;
                }
            }
        };

        this.chart.update();
    }

    /**
     * Set chart view to Equity Curve (Account or P&L).
     *
     * Account mode: capital-normalized — starts at total invested, ends
     *   at current broker value.  Gains/losses are the visible gap.
     * P&L mode: starts at $0, shows cumulative realized P&L.
     *
     * Each history point carries cumulative_deposits and cumulative_pnl
     * so all normalization is computed from the data itself.
     *
     * @param {Array} history - daily data points from /api/portfolio-metrics
     * @param {Array} adjustments - cash adjustment records (for marker styling)
     * @param {'account'|'pnl'} mode
     * @param {number} startingValue - broker value at period start (0 if all-time)
     */
    showEquityCurve(history, adjustments = [], mode = 'account', startingValue = 0) {
        if (!this.chart) return;
        this.mode = 'portfolio';

        if (!history || history.length === 0) {
            history = [{ date: new Date().toLocaleDateString(), portfolio_value: 10000.0 }];
        }

        const labels = history.map(h => h.date);

        let values;
        if (mode === 'pnl') {
            // Use the backend-provided cumulative_pnl directly.
            // For period views, offset so the chart starts at $0.
            const basePnl = history[0].cumulative_pnl || 0;
            values = history.map(h => (h.cumulative_pnl || 0) - basePnl);
        } else {
            // Account: show capital-normalized portfolio value.
            // For each day, add back deposits not yet made so the chart
            // starts at "total invested" and converges to the real broker
            // value after all deposits are complete.  This makes actual
            // gains/losses immediately visible as the gap between the
            // starting total and the current value.
            const totalDeposits = history[history.length - 1].cumulative_deposits || 0;
            values = history.map(h => {
                const remainingDeposits = totalDeposits - (h.cumulative_deposits || 0);
                return h.portfolio_value + remainingDeposits;
            });
        }

        // Build adjustment marker set for point styling
        const adjDates = new Set(adjustments.map(a => a.date));

        // Point styling: larger amber markers on adjustment dates
        const pointRadii = labels.map(d => adjDates.has(d) ? 7 : 3.5);
        const pointStyles = labels.map(d => adjDates.has(d) ? 'rectRot' : 'circle');
        const pointBorderWidths = labels.map(d => adjDates.has(d) ? 2 : 0);

        // Color scheme: emerald for account view, cyan for P&L view
        const lineColor = mode === 'pnl' ? '#06b6d4' : '#10b981';
        const lineColorAlpha = mode === 'pnl' ? 'rgba(6, 182, 212, 0.08)' : 'rgba(16, 185, 129, 0.05)';
        const pointColors = labels.map(d => adjDates.has(d) ? '#f59e0b' : lineColor);
        const pointBorderColors = labels.map(d => adjDates.has(d) ? '#d97706' : lineColor);

        // Re-configure datasets
        const dataset = this.chart.data.datasets[0];
        dataset.label = mode === 'pnl' ? 'Trading P&L ($)' : 'Account Value ($)';
        dataset.borderColor = lineColor;
        dataset.backgroundColor = lineColorAlpha;
        dataset.fill = mode === 'pnl';  // Fill to zero-line in P&L mode
        dataset.pointBackgroundColor = pointColors;
        dataset.pointRadius = pointRadii;
        dataset.pointStyle = pointStyles;
        dataset.pointBorderWidth = pointBorderWidths;
        dataset.pointBorderColor = pointBorderColors;
        dataset.data = values;

        // Re-configure scales
        this.chart.data.labels = labels;
        this.chart.options.scales.y.position = 'left';
        
        // Dynamic axis scaling with padding
        const minVal = Math.min(...values);
        const maxVal = Math.max(...values);

        if (mode === 'pnl') {
            // Always include $0 in the P&L view for context
            const effectiveMin = Math.min(0, minVal);
            const effectiveMax = Math.max(0, maxVal);
            const pad = (effectiveMax - effectiveMin) * 0.15 || 100.0;
            this.chart.options.scales.y.min = Math.floor(effectiveMin - pad);
            this.chart.options.scales.y.max = Math.ceil(effectiveMax + pad);
        } else {
            const pad = (maxVal - minVal) * 0.1 || 100.0;
            this.chart.options.scales.y.min = Math.floor(minVal - pad);
            this.chart.options.scales.y.max = Math.ceil(maxVal + pad);
        }

        this.chart.options.scales.y.ticks.color = lineColor;
        
        // Format tick labels as currency (with sign for P&L)
        this.chart.options.scales.y.ticks.callback = function(value) {
            if (mode === 'pnl') {
                const sign = value >= 0 ? '+' : '-';
                return sign + '$' + Math.abs(Math.round(value)).toLocaleString();
            }
            return '$' + Math.round(value).toLocaleString();
        };

        // Custom tooltip to show adjustment info
        const adjByDate = {};
        adjustments.forEach(a => {
            if (!adjByDate[a.date]) adjByDate[a.date] = [];
            adjByDate[a.date].push(a);
        });

        this.chart.options.plugins.tooltip = {
            callbacks: {
                afterBody: (items) => {
                    if (!items || items.length === 0) return '';
                    const date = labels[items[0].dataIndex];
                    const dateAdjs = adjByDate[date];
                    if (!dateAdjs || dateAdjs.length === 0) return '';
                    return dateAdjs.map(a => {
                        const sign = a.amount > 0 ? '▼ Deposit' : '▲ Withdrawal';
                        const val = '$' + Math.abs(a.amount).toLocaleString(undefined, {minimumFractionDigits: 2});
                        return `${sign}: ${val}${a.note ? ' — ' + a.note : ''}`;
                    }).join('\n');
                }
            }
        };

        // P&L mode: add annotation plugin config for zero line if available
        if (this.chart.options.plugins.annotation) {
            if (mode === 'pnl') {
                this.chart.options.plugins.annotation = {
                    annotations: {
                        zeroLine: {
                            type: 'line',
                            yMin: 0, yMax: 0,
                            borderColor: 'rgba(255, 255, 255, 0.2)',
                            borderWidth: 1,
                            borderDash: [4, 4],
                        }
                    }
                };
            } else {
                this.chart.options.plugins.annotation = { annotations: {} };
            }
        }

        this.chart.update();
    }

}

/* ═══════════════════════════════════════════════════════════════════════
   EvoTrader — Technical Indicator & Portfolio Chart Module (ES6 Module)
   ═══════════════════════════════════════════════════════════════════════ */

import { etLong, etShort, signalLean } from "./components/signal_display.js";
import { daySide, equityValues, isStrong, signalAxisLimit } from "./components/equity_overlay.js";

// ── Signal overlay on the account chart ──────────────────────────────────
// The lean colours of the market panel (emerald, crimson) and a slate for a
// neutral day, as RGB so they can be blended at any opacity.
const SIDE_RGB = { long: [21, 128, 61], short: [185, 28, 28], neutral: [100, 116, 139] };
// With the overlay on, the curve is drawn in ink so green and red mean only
// the signal's lean.
const INK_RGB = [31, 41, 55];
const RANGE_LABEL = 'Signal day range';
const rgba = (rgb, a) => `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${a})`;
const hexRgb = (hex) => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));

/** "Oct 5" for an Eastern 'YYYY-MM-DD' date, read as that calendar day everywhere. */
function shortDay(date) {
    const d = new Date(`${date}T12:00:00Z`);
    return Number.isNaN(d.getTime()) ? String(date)
        : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

/** "Mon, Oct 5, 2026" for the tooltip. */
function longDay(date) {
    const d = new Date(`${date}T12:00:00Z`);
    return Number.isNaN(d.getTime()) ? String(date)
        : d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
}

const signed = (v, digits) => (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(digits);
const money = (v) => '$' + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const signedMoney = (v) => (v >= 0 ? '+' : '−') + money(v);

/** Each day's background in the account pane: a faint tint in the colour of its lean. */
const leanShading = {
    id: 'leanShading',
    beforeDatasetsDraw(chart) {
        const days = chart.$overlay && chart.$overlay.days;
        const { x, y } = chart.scales;
        if (!days || !x || !y) return;
        const step = days.length > 1
            ? Math.abs(x.getPixelForValue(1) - x.getPixelForValue(0))
            : x.right - x.left;
        const { ctx } = chart;
        ctx.save();
        days.forEach((day, i) => {
            const side = daySide(day);
            if (!side || side === 'neutral') return;
            const centre = x.getPixelForValue(i);
            const left = Math.max(x.left, centre - step / 2);
            const right = Math.min(x.right, centre + step / 2);
            ctx.fillStyle = rgba(SIDE_RGB[side], isStrong(day) ? 0.12 : 0.06);
            ctx.fillRect(left, y.top, right - left, y.bottom - y.top);
        });
        ctx.restore();
    },
};

/** The signal pane's zero line, a hairline between the panes, and its label. */
const paneDecor = {
    id: 'paneDecor',
    beforeDatasetsDraw(chart) {
        const equity = chart.$equity;
        const { y, ySignal } = chart.scales;
        const { ctx, chartArea } = chart;
        if (!equity || !chartArea) return;
        ctx.save();
        ctx.lineWidth = 1;
        if (equity.mode === 'pnl' && y) {
            const zero = y.getPixelForValue(0);
            if (zero >= y.top && zero <= y.bottom) {
                ctx.setLineDash([4, 4]);
                ctx.strokeStyle = 'rgba(25, 25, 25, 0.22)';
                ctx.beginPath();
                ctx.moveTo(chartArea.left, zero);
                ctx.lineTo(chartArea.right, zero);
                ctx.stroke();
            }
        }
        if (chart.$overlay && ySignal) {
            ctx.setLineDash([]);
            ctx.strokeStyle = 'rgba(25, 25, 25, 0.10)';
            ctx.beginPath();
            ctx.moveTo(chartArea.left, ySignal.top - 4);
            ctx.lineTo(chartArea.right, ySignal.top - 4);
            ctx.stroke();
            const zero = ySignal.getPixelForValue(0);
            ctx.setLineDash([2, 3]);
            ctx.strokeStyle = 'rgba(25, 25, 25, 0.32)';
            ctx.beginPath();
            ctx.moveTo(chartArea.left, zero);
            ctx.lineTo(chartArea.right, zero);
            ctx.stroke();
        }
        ctx.restore();
    },
    afterDatasetsDraw(chart) {
        const { ySignal } = chart.scales;
        const { ctx, chartArea } = chart;
        if (!chart.$overlay || !ySignal || !chartArea) return;
        const text = 'SIGNAL \u00b7 DAILY AVERAGE';
        ctx.save();
        ctx.font = '600 9px Outfit, sans-serif';
        const width = ctx.measureText(text).width;
        ctx.fillStyle = 'rgba(251, 250, 247, 0.85)';
        ctx.fillRect(chartArea.left + 2, ySignal.top - 1, width + 8, 13);
        ctx.fillStyle = 'rgba(107, 107, 102, 0.95)';
        ctx.textBaseline = 'top';
        ctx.fillText(text, chartArea.left + 6, ySignal.top + 1);
        ctx.restore();
    },
};

/** A vertical guide through every pane at the hovered day. */
const crosshair = {
    id: 'crosshair',
    afterDatasetsDraw(chart) {
        if (!chart.$equity || !chart.tooltip) return;
        const active = chart.tooltip.getActiveElements();
        if (!active || !active.length) return;
        const { ctx, chartArea } = chart;
        const at = active[0].element.x;
        ctx.save();
        ctx.setLineDash([3, 3]);
        ctx.lineWidth = 1;
        ctx.strokeStyle = 'rgba(25, 25, 25, 0.30)';
        ctx.beginPath();
        ctx.moveTo(at, chartArea.top);
        ctx.lineTo(at, chartArea.bottom);
        ctx.stroke();
        ctx.restore();
    },
};

/** A bar fading from the day's lean colour at its tip to almost nothing at zero. */
function barFill(context, days) {
    const day = days[context.dataIndex];
    if (!day) return 'transparent';
    const rgb = SIDE_RGB[daySide(day)];
    const tip = isStrong(day) ? 0.95 : 0.7;
    const { chart } = context;
    const scale = chart.scales.ySignal;
    if (!chart.chartArea || !scale) return rgba(rgb, tip);
    const zero = scale.getPixelForValue(0);
    const end = scale.getPixelForValue(day.mean);
    if (!Number.isFinite(zero) || !Number.isFinite(end) || Math.abs(end - zero) < 1) {
        return rgba(rgb, tip);
    }
    const gradient = chart.ctx.createLinearGradient(0, end, 0, zero);
    gradient.addColorStop(0, rgba(rgb, tip));
    gradient.addColorStop(1, rgba(rgb, 0.18));
    return gradient;
}

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
            // Inert until showEquityCurve marks the chart (chart.$equity, chart.$overlay).
            plugins: [leanShading, paneDecor, crosshair],
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
     * Set chart view to Equity Curve (Account or P&L), optionally with the
     * combined signal overlaid.
     *
     * Account mode: capital-normalized — starts at total invested, ends
     *   at current broker value.  Gains/losses are the visible gap.
     * P&L mode: starts at $0, shows cumulative realized P&L.
     *
     * With ``signalDays`` (alignSignalDays: each history date's signal summary
     * or null) the chart becomes two panes on the same dates: the curve on top,
     * each day tinted by the way its average signal leaned, and the signal's
     * daily average as bars below, with the day's range as a thin line.
     *
     * @param {Array} history - daily data points from /api/metrics/portfolio
     * @param {Array} adjustments - cash adjustment records (for marker styling)
     * @param {'account'|'pnl'} mode
     * @param {number} startingValue - broker value at period start (0 if all-time)
     * @param {Array|null} signalDays - per-date signal summaries, or null for no overlay
     */
    showEquityCurve(history, adjustments = [], mode = 'account', startingValue = 0, signalDays = null) {
        if (!this.chart) return;
        this.mode = 'portfolio';

        if (!history || history.length === 0) {
            history = [{ date: new Date().toLocaleDateString(), portfolio_value: 10000.0 }];
            signalDays = signalDays ? [null] : null;
        }

        const labels = history.map(h => h.date);
        const values = equityValues(history, mode);
        const overlay = Array.isArray(signalDays) && signalDays.length === history.length;
        const chart = this.chart;
        chart.$equity = { mode };
        chart.$overlay = overlay ? { days: signalDays } : null;

        // Build adjustment marker set for point styling
        const adjDates = new Set(adjustments.map(a => a.date));

        // The curve: ink under the overlay, otherwise emerald (account) or cyan (P&L).
        const ownRgb = hexRgb(mode === 'pnl' ? '#0891b2' : '#10b981');
        const lineRgb = overlay ? INK_RGB : ownRgb;
        const lineColor = rgba(lineRgb, 1);

        const dataset = chart.data.datasets[0];
        dataset.type = 'line';
        dataset.label = mode === 'pnl' ? 'Trading P&L ($)' : 'Account Value ($)';
        dataset.data = values;
        dataset.yAxisID = 'y';
        dataset.order = 0;
        dataset.borderColor = lineColor;
        dataset.borderWidth = 2.25;
        dataset.tension = 0.25;
        dataset.cubicInterpolationMode = 'monotone';
        // A soft wash under the curve; P&L fills to zero.
        dataset.fill = mode === 'pnl' ? 'origin' : 'start';
        dataset.backgroundColor = (context) => {
            const y = context.chart.scales.y;
            if (!context.chart.chartArea || !y) return rgba(lineRgb, 0.06);
            const wash = context.chart.ctx.createLinearGradient(0, y.top, 0, y.bottom);
            wash.addColorStop(0, rgba(lineRgb, overlay ? 0.10 : 0.16));
            wash.addColorStop(1, rgba(lineRgb, 0));
            return wash;
        };
        // Points only where a deposit or withdrawal landed (amber diamonds); the
        // rest appear on hover.
        dataset.pointStyle = labels.map(d => adjDates.has(d) ? 'rectRot' : 'circle');
        dataset.pointRadius = labels.map(d => adjDates.has(d) ? 6 : 0);
        dataset.pointBorderWidth = labels.map(d => adjDates.has(d) ? 2 : 0);
        dataset.pointBackgroundColor = labels.map(d => adjDates.has(d) ? '#f59e0b' : lineColor);
        dataset.pointBorderColor = labels.map(d => adjDates.has(d) ? '#d97706' : '#ffffff');
        dataset.pointHoverRadius = 4.5;
        dataset.pointHoverBorderWidth = 2;
        dataset.pointHitRadius = 10;

        chart.data.labels = labels;
        chart.data.datasets.length = 1;
        if (overlay) {
            chart.data.datasets.push(
                {
                    type: 'bar',
                    label: 'Signal (daily average)',
                    yAxisID: 'ySignal',
                    order: 2,
                    grouped: false,
                    data: signalDays.map(d => (d ? d.mean : null)),
                    backgroundColor: (context) => barFill(context, signalDays),
                    borderRadius: 3,
                    barPercentage: 0.7,
                    categoryPercentage: 0.9,
                    maxBarThickness: 26,
                },
                {
                    type: 'bar',
                    label: RANGE_LABEL,
                    yAxisID: 'ySignal',
                    order: 3,
                    grouped: false,
                    data: signalDays.map(d => (d ? [d.min, d.max] : null)),
                    backgroundColor: 'rgba(25, 25, 25, 0.16)',
                    borderRadius: 2,
                    barPercentage: 0.08,
                    categoryPercentage: 0.9,
                    maxBarThickness: 3,
                },
            );
        }

        // ── Scales ──
        const scales = chart.options.scales;
        scales.x.offset = overlay;
        scales.x.grid = { display: false };
        scales.x.border = { display: false };
        scales.x.ticks = {
            color: '#6b6b66',
            font: { family: 'Outfit', size: 10 },
            autoSkip: true,
            maxTicksLimit: 7,
            maxRotation: 0,
            minRotation: 0,
            callback(value) { return shortDay(this.getLabelForValue(value)); },
        };

        const minVal = Math.min(...values);
        const maxVal = Math.max(...values);
        let yMin;
        let yMax;
        if (mode === 'pnl') {
            // Always include $0 in the P&L view for context
            const effectiveMin = Math.min(0, minVal);
            const effectiveMax = Math.max(0, maxVal);
            const pad = (effectiveMax - effectiveMin) * 0.15 || 100.0;
            yMin = Math.floor(effectiveMin - pad);
            yMax = Math.ceil(effectiveMax + pad);
        } else {
            const pad = (maxVal - minVal) * 0.1 || 100.0;
            yMin = Math.floor(minVal - pad);
            yMax = Math.ceil(maxVal + pad);
        }
        scales.y = {
            type: 'linear',
            position: 'left',
            min: yMin,
            max: yMax,
            grid: { color: 'rgba(227, 223, 208, 0.7)', drawTicks: false },
            border: { display: false },
            ticks: {
                color: lineColor,
                font: { family: 'JetBrains Mono', size: 10 },
                padding: 6,
                maxTicksLimit: overlay ? 5 : 6,
                // Round levels only: the exact low and high crowd the pane edges.
                includeBounds: false,
                // Format tick labels as currency (with sign for P&L). Over the
                // signal pane the label on the pane's bottom edge would sit on
                // the signal scale, so it is left out.
                callback(value, index) {
                    if (overlay && index === 0 && value <= this.min) return '';
                    if (mode === 'pnl') {
                        const sign = value >= 0 ? '+' : '-';
                        return sign + '$' + Math.abs(Math.round(value)).toLocaleString();
                    }
                    return '$' + Math.round(value).toLocaleString();
                },
            },
        };
        if (overlay) {
            const limit = signalAxisLimit(signalDays);
            // Two panes on one date axis: the curve over the signal, about
            // three to one. Stacked left axes are laid out heaviest `weight`
            // first, top to bottom, so the curve's axis outweighs the signal's.
            scales.y.stack = 'equity';
            scales.y.stackWeight = 3;
            scales.y.weight = 1;
            scales.ySignal = {
                type: 'linear',
                position: 'left',
                stack: 'equity',
                stackWeight: 1.4,
                weight: 0,
                // Headroom so the end labels sit inside the pane, clear of the curve's.
                min: -limit * 1.2,
                max: limit * 1.2,
                grid: { display: false },
                border: { display: false },
                afterBuildTicks(axis) {
                    axis.ticks = [-limit, 0, limit].map(value => ({ value }));
                },
                ticks: {
                    color: '#949080',
                    font: { family: 'JetBrains Mono', size: 9 },
                    padding: 6,
                    callback(value) { return value === 0 ? '0' : signed(value, limit < 0.5 ? 2 : 1); },
                },
            };
        } else {
            delete scales.ySignal;
        }

        // ── Tooltip: one card per day, the curve and the signal together ──
        const adjByDate = {};
        adjustments.forEach(a => {
            if (!adjByDate[a.date]) adjByDate[a.date] = [];
            adjByDate[a.date].push(a);
        });
        chart.options.interaction = { mode: 'index', intersect: false };
        chart.options.plugins.tooltip = {
            backgroundColor: 'rgba(25, 25, 25, 0.92)',
            titleColor: '#ffffff',
            bodyColor: '#f6f5ef',
            footerColor: '#cfcaa9',
            padding: 10,
            cornerRadius: 8,
            boxPadding: 4,
            usePointStyle: true,
            titleFont: { family: 'Outfit', size: 12, weight: '600' },
            bodyFont: { family: 'JetBrains Mono', size: 11 },
            footerFont: { family: 'Outfit', size: 11 },
            filter: (item) => item.dataset.label !== RANGE_LABEL && item.raw !== null,
            callbacks: {
                title: (items) => (items.length ? longDay(labels[items[0].dataIndex]) : ''),
                label: (item) => {
                    const i = item.dataIndex;
                    if (item.datasetIndex === 0) {
                        const name = mode === 'pnl' ? 'P&L' : 'Account';
                        const shown = mode === 'pnl' ? signedMoney(values[i]) : money(values[i]);
                        const change = i > 0 ? `  (${signedMoney(values[i] - values[i - 1])})` : '';
                        return ` ${name}  ${shown}${change}`;
                    }
                    const day = signalDays && signalDays[i];
                    if (!day) return null;
                    return ` Signal   ${signed(day.mean, 3)}  ${signalLean(day.mean, day.regime).label.toLowerCase()}`;
                },
                labelColor: (item) => {
                    if (item.datasetIndex === 0) {
                        return { borderColor: lineColor, backgroundColor: lineColor };
                    }
                    const day = signalDays && signalDays[item.dataIndex];
                    const color = rgba(SIDE_RGB[daySide(day) || 'neutral'], 1);
                    return { borderColor: color, backgroundColor: color };
                },
                afterLabel: (item) => {
                    if (item.datasetIndex === 0) return '';
                    const day = signalDays && signalDays[item.dataIndex];
                    if (!day) return '';
                    const readings = day.count === 1 ? 'reading' : 'readings';
                    return `           ${signed(day.min, 2)} to ${signed(day.max, 2)} · ${day.count} ${readings}${day.ticker ? ' · ' + day.ticker : ''}`;
                },
                footer: (items) => {
                    if (!items || items.length === 0) return '';
                    const dateAdjs = adjByDate[labels[items[0].dataIndex]];
                    if (!dateAdjs || dateAdjs.length === 0) return '';
                    return dateAdjs.map(a => {
                        const sign = a.amount > 0 ? '▼ Deposit' : '▲ Withdrawal';
                        const val = '$' + Math.abs(a.amount).toLocaleString(undefined, { minimumFractionDigits: 2 });
                        return `${sign}: ${val}${a.note ? ' — ' + a.note : ''}`;
                    }).join('\n');
                },
            },
        };

        chart.update();
    }

}

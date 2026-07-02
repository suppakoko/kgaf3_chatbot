/**
 * afmm_chat — ResultCharts: Plotly 차트 래퍼 (Phase 3)
 * frontend_plan.md §4.7  /  §8
 *
 * 포함:
 *  - renderSminaDistribution : smina_affinity 히스토그램
 *  - renderIptmVsSmina       : ipTM vs smina 산점도 (Pareto 프론티어)
 *  (OpenMM E_interaction 박스플롯은 smina --minimize 재구성에서 제거됨)
 *
 * Dark theme 기준값 (CSS 변수와 일치):
 *  paper_bgcolor '#1a1a2e'  plot_bgcolor '#0f0f1a'  font.color '#e0e0e0'
 *
 * Plotly: CDN 지연 로드 → /static/lib/plotly.min.js 로컬 폴백 (AD-F6)
 */
'use strict';

const PLOTLY_CDN   = 'https://cdn.plot.ly/plotly-latest.min.js';
const PLOTLY_LOCAL = '/static/lib/plotly.min.js';

/** @type {Promise<void>|null} */
let _plotlyLoadPromise = null;

/**
 * Plotly 스크립트를 지연 로드한다.
 * @returns {Promise<void>}
 */
function _ensurePlotly() {
    if (typeof Plotly !== 'undefined') return Promise.resolve();
    if (_plotlyLoadPromise) return _plotlyLoadPromise;

    _plotlyLoadPromise = new Promise((resolve, reject) => {
        /** @param {string} src @returns {Promise<void>} */
        const load = (src) => new Promise((res, rej) => {
            if (document.querySelector(`script[src="${src}"]`)) { res(); return; }
            const s = document.createElement('script');
            s.src  = src;
            s.async = false;
            s.onload  = () => res();
            s.onerror = () => rej(new Error(`Plotly 로드 실패: ${src}`));
            document.head.appendChild(s);
        });

        load(PLOTLY_CDN)
            .then(resolve)
            .catch(() => {
                console.warn('[result_charts] CDN 실패, 로컬 폴백');
                load(PLOTLY_LOCAL).then(resolve).catch(reject);
            });
    });

    return _plotlyLoadPromise;
}

/* ── 공통 라이트 레이아웃 (KIST red accent) ──────────────────────────────── */

/** @returns {Partial<Plotly.Layout>} */
function _darkLayout(extra = {}) {
    // 함수명은 호환을 위해 유지 — 실제 팔레트는 라이트 테마.
    return Object.assign({
        paper_bgcolor : '#ffffff',
        plot_bgcolor  : '#ffffff',
        font          : { color: '#111827', family: "'Noto Sans KR', system-ui, sans-serif", size: 12 },
        margin        : { t: 36, r: 16, b: 48, l: 52 },
        xaxis: {
            gridcolor   : '#e5e7eb',
            zerolinecolor: '#d0d4de',
            tickfont    : { color: '#5f6878' },
        },
        yaxis: {
            gridcolor   : '#e5e7eb',
            zerolinecolor: '#d0d4de',
            tickfont    : { color: '#5f6878' },
        },
        hoverlabel: {
            bgcolor : '#ffffff',
            bordercolor: '#d0d4de',
            font    : { color: '#111827' },
        },
    }, extra);
}

/** @type {Partial<Plotly.Config>} */
const _plotConfig = {
    responsive       : true,
    displayModeBar   : true,
    displaylogo      : false,
    modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'],
};

/* ── 1. Smina Affinity 히스토그램 ──────────────────────────────────────────── */

/**
 * Smina affinity 값의 히스토그램을 렌더링한다.
 * @param {string} containerId
 * @param {number[]} sminaValues - smina_affinity 값 배열 (kcal/mol, 음수 = 강한 결합)
 * @returns {Promise<void>}
 */
async function renderSminaDistribution(containerId, sminaValues) {
    await _ensurePlotly();
    const el = document.getElementById(containerId);
    if (!el || !Array.isArray(sminaValues) || sminaValues.length === 0) return;

    const valid = sminaValues.filter(v => typeof v === 'number' && isFinite(v));

    const trace = {
        type    : 'histogram',
        x       : valid,
        nbinsx  : Math.min(30, Math.ceil(Math.sqrt(valid.length))),
        marker  : { color: '#e63946', opacity: 0.8 },
        name    : 'Smina Affinity',
        hovertemplate: '%{x:.2f} kcal/mol<br>분자 수: %{y}<extra></extra>',
    };

    const layout = _darkLayout({
        title: { text: 'Smina Affinity 분포', font: { size: 13 } },
        xaxis: { title: { text: 'Smina Affinity (kcal/mol)' } },
        yaxis: { title: { text: '분자 수' } },
        bargap: 0.05,
        showlegend: false,
    });

    Plotly.react(el, [trace], layout, _plotConfig);
}

/* ── 2. ipTM vs Smina 산점도 (Pareto 프론티어) ──────────────────────────────── */

/**
 * ipTM vs smina_affinity 산점도를 렌더링한다.
 * Pareto 프론티어 (iptm 최대 + smina 최소)를 빨간 선으로 표시한다.
 * @param {string} containerId
 * @param {Array<{ligand_id:string, iptm:number, smina_affinity:number}>} rows
 * @returns {Promise<void>}
 */
async function renderIptmVsSmina(containerId, rows) {
    await _ensurePlotly();
    const el = document.getElementById(containerId);
    if (!el || !Array.isArray(rows) || rows.length === 0) return;

    const valid = rows.filter(r =>
        typeof r.iptm === 'number' && isFinite(r.iptm) &&
        typeof r.smina_affinity_kcal_mol === 'number' && isFinite(r.smina_affinity_kcal_mol));

    if (valid.length === 0) return;

    // Pareto 프론티어 계산 (ipTM 내림차순 + smina 기준 비지배 선별)
    const sorted = [...valid].sort((a, b) => b.iptm - a.iptm);
    const pareto = [];
    let bestSmina = Infinity;
    for (const r of sorted) {
        const s = r.smina_affinity_kcal_mol;
        if (s < bestSmina) {
            bestSmina = s;
            pareto.push(r);
        }
    }

    const scatter = {
        type : 'scatter',
        mode : 'markers',
        x    : valid.map(r => r.smina_affinity_kcal_mol),
        y    : valid.map(r => r.iptm),
        text : valid.map(r => r.ligand_id ?? ''),
        marker: {
            color  : '#8888aa',
            size   : 7,
            opacity: 0.75,
            line   : { color: '#2a2a4a', width: 1 },
        },
        name            : '전체 분자',
        hovertemplate   : '<b>%{text}</b><br>Smina: %{x:.2f}<br>ipTM: %{y:.3f}<extra></extra>',
    };

    const paretoTrace = {
        type : 'scatter',
        mode : 'markers+lines',
        x    : pareto.map(r => r.smina_affinity_kcal_mol),
        y    : pareto.map(r => r.iptm),
        text : pareto.map(r => r.ligand_id ?? ''),
        marker: { color: '#e63946', size: 10, symbol: 'star' },
        line  : { color: '#e63946', width: 1.5, dash: 'dot' },
        name  : 'Pareto 프론티어',
        hovertemplate: '<b>%{text}</b> [Pareto]<br>Smina: %{x:.2f}<br>ipTM: %{y:.3f}<extra></extra>',
    };

    const layout = _darkLayout({
        title : { text: 'ipTM vs Smina Affinity', font: { size: 13 } },
        xaxis : { title: { text: 'Smina Affinity (kcal/mol)' }, autorange: 'reversed' },
        yaxis : { title: { text: 'ipTM' } },
        legend: { x: 0.01, y: 0.01, bgcolor: 'rgba(22,33,62,0.8)', bordercolor: '#2a2a4a', borderwidth: 1 },
    });

    Plotly.react(el, [scatter, paretoTrace], layout, _plotConfig);
}

/* OpenMM E_interaction 박스플롯은 smina --minimize 재구성에서 제거됨. */

/**
 * afmm_chat — 하단 시스템 바
 * - 연결정보 (host:port)
 * - MCP 상태 (AF3, Smina) — Smina 칩은 smina 를 호스팅하는 MCP(구 OpenMM) 연결 상태
 * - LLM 모델
 * - 활성 잡 수
 * - GPU 사용률 / 메모리
 * - 시계
 *
 * /api/system/info 를 주기적으로 폴링.
 */
'use strict';

(function () {
    const POLL_INTERVAL_MS = 5000;

    const $af3      = document.getElementById('sb-mcp-af3');
    const $openmm   = document.getElementById('sb-mcp-openmm');
    const $graphrag = document.getElementById('sb-graphrag');
    const $llm      = document.getElementById('sb-llm');
    const $jobs     = document.getElementById('sb-jobs');
    const $gpus     = document.getElementById('sb-gpus');
    const $clock    = document.getElementById('sb-clock');

    function _setChip(el, txt, cls) {
        if (!el) return;
        el.textContent = txt;
        el.classList.remove('system-bar__chip--ok','system-bar__chip--err','system-bar__chip--warn');
        if (cls) el.classList.add('system-bar__chip--' + cls);
    }

    function _renderGpus(gpus) {
        if (!$gpus) return;
        if (!gpus || gpus.length === 0) {
            $gpus.innerHTML = '<span class="gpu-pill" title="GPU 없음">GPU: n/a</span>';
            return;
        }
        const parts = gpus.map(g => {
            const memPct = (g.mem_total_mb > 0) ? Math.round((g.mem_used_mb / g.mem_total_mb) * 100) : 0;
            const memGb = (g.mem_used_mb / 1024).toFixed(1);
            const totalGb = (g.mem_total_mb / 1024).toFixed(0);
            const hot = (g.temp_c >= 80) || (g.util_pct >= 95);
            const idle = (g.util_pct < 5);
            const cls = ['gpu-pill', hot && 'gpu-pill--hot', idle && 'gpu-pill--idle']
                .filter(Boolean).join(' ');
            const title = `GPU#${g.index} ${g.name} | util ${g.util_pct}% | mem ${memGb}/${totalGb} GB (${memPct}%) | ${g.temp_c}°C`;
            return `
                <span class="${cls}" title="${_esc(title)}">
                  <span class="gpu-pill__idx">#${g.index}</span>
                  <span class="gpu-pill__util">${g.util_pct}%</span>
                  <span class="gpu-pill__mem">${memGb}/${totalGb}G</span>
                </span>`;
        });
        $gpus.innerHTML = parts.join('');
    }

    function _esc(s) {
        return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                              .replace(/"/g,'&quot;');
    }

    async function poll() {
        try {
            const res = await fetch('/api/system/info', { cache: 'no-store' });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const d = await res.json();

            const af3 = (d.mcp && d.mcp.af3) || {};
            _setChip($af3, `AF3: ${af3.connected ? 'on' : 'off'}`, af3.connected ? 'ok' : 'err');
            if ($af3) $af3.title = af3.url || '';

            // smina 는 구 OpenMM MCP(:8001)에 호스팅됨 → 연결키는 mcp.openmm 재사용, 표기는 Smina
            const om = (d.mcp && d.mcp.openmm) || {};
            _setChip($openmm, `Smina: ${om.connected ? 'on' : 'off'}`, om.connected ? 'ok' : 'err');
            if ($openmm) $openmm.title = om.url || '';

            const gr = d.graphrag || {};
            if (!gr.enabled) {
                _setChip($graphrag, 'GraphRAG: off', 'warn');
            } else {
                _setChip(
                    $graphrag,
                    `GraphRAG: ${gr.connected ? 'on' : 'off'}`,
                    gr.connected ? 'ok' : 'err',
                );
            }
            if ($graphrag) {
                $graphrag.title = (gr.mcp_url || '') +
                    (gr.model ? ` · ${gr.model}` : '');
            }

            const orx = d.openrouter || {};
            const modelShort = (orx.default_model || '-').split('/').pop();
            _setChip($llm, `LLM: ${modelShort}${orx.configured ? '' : ' (no key)'}`,
                     orx.configured ? null : 'warn');

            const jobsCount = (d.jobs && d.jobs.active) || 0;
            _setChip($jobs, `jobs: ${jobsCount}`, jobsCount > 0 ? 'ok' : null);

            _renderGpus(d.gpus || []);
        } catch (e) {
            // 조용히 실패: status chip 만 갱신
            const $st = document.getElementById('status-bar');
            if ($st) { $st.textContent = '연결 실패'; $st.classList.add('system-bar__chip--err'); }
        }
    }

    function _tickClock() {
        if (!$clock) return;
        const d = new Date();
        const pad = (n) => String(n).padStart(2, '0');
        $clock.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }

    document.addEventListener('DOMContentLoaded', () => {
        poll();
        setInterval(poll, POLL_INTERVAL_MS);
        _tickClock();
        setInterval(_tickClock, 1000);
    });

    window.SystemBar = { poll };
})();

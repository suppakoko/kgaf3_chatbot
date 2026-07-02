/**
 * afmm_chat — 작업 진행 상황 패널 (v3: 3단계 sub-stage + 하트비트)
 *
 * 3개의 sub-stage (OpenMM 제거 — smina --minimize 파이프라인):
 *   - af3      : AlphaFold3 MSA + 구조 예측 (per-ligand, 가장 오래 걸림)
 *   - smina    : Smina --minimize (분리 + 로컬 최소화 + binding affinity)
 *   - rank     : Composite ranking + LLM 요약
 *
 * 각 sub-stage 는 자체 status / 진행 카운터 / lastProgressAt 를 가지며,
 * 마지막 진행 신호가 STALE_THRESHOLD_MS 를 넘으면 stale 배지 표시.
 */
'use strict';

(function () {
    const $panel = document.getElementById('status-panel');
    const $body  = document.getElementById('status-panel-body');
    const $close = document.getElementById('btn-close-status-panel');

    /** jobId → state object */
    const jobs = new Map();

    // Stale 임계값. AF3 폴은 30s 마다 heartbeat 가 오므로 5분이면 충분히 안전.
    // 백엔드가 영구히 멈췄을 때만 트리거 — 진짜 작업 중에는 절대 안 뜸.
    const STALE_THRESHOLD_MS = 5 * 60 * 1000;

    // sub-stage 정의 — 표시 순서 + 라벨
    const STAGES = [
        { key: 'af3',    label: '🧬 AF3 (MSA + 구조)' },
        { key: 'smina',  label: '🎯 Smina 최소화' },
        { key: 'rank',   label: '📊 랭킹 + 해석' },
    ];

    function _esc(s) {
        return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    function _show() { if ($panel) $panel.classList.remove('hidden'); }
    function _hideIfEmpty() { if ($panel && jobs.size === 0) $panel.classList.add('hidden'); }
    function _shortId(id) { return (id || '').slice(0, 8); }

    /** 새 sub-stage 초기 상태 */
    function _newSubStage() {
        return {
            status: 'pending',      // pending / running / done / error
            subStage: '',           // 백엔드가 보내는 임의 sub_stage 문자열
            done: 0,
            total: 0,
            lastProgressAt: 0,      // 0 = 아직 시작 안 함
            startedAt: 0,
            lastDetail: '',
        };
    }

    function _ensureRow(jobId) {
        if (!$body) return null;
        let st = jobs.get(jobId);
        if (st) return st;

        st = {
            jobId,
            overallStatus: 'queued',
            startedAt: Date.now(),
            stages: Object.fromEntries(STAGES.map(s => [s.key, _newSubStage()])),
            row: null,
        };

        const row = document.createElement('div');
        row.className = 'status-panel__row';
        row.dataset.jobId = jobId;

        const stagesHtml = STAGES.map(s => `
            <div class="stage-row" data-stage="${s.key}">
              <div class="stage-row__label">
                <span class="stage-row__icon" data-role="icon">⏸</span>
                ${_esc(s.label)}
              </div>
              <div class="stage-row__bar" data-role="bar"><span style="width:0%"></span></div>
              <div class="stage-row__detail" data-role="detail">대기 중</div>
            </div>
        `).join('');

        row.innerHTML = `
            <div class="status-panel__row-head">
                <span class="status-panel__row-id">${_esc(_shortId(jobId))}</span>
                <span class="status-panel__row-stage" data-role="overall">queued</span>
            </div>
            <div class="status-panel__row-stages">
              ${stagesHtml}
            </div>
        `;

        $body.appendChild(row);
        st.row = row;
        jobs.set(jobId, st);
        _show();
        return st;
    }

    function _removeRow(jobId) {
        const st = jobs.get(jobId);
        if (st && st.row && st.row.parentNode) st.row.parentNode.removeChild(st.row);
        jobs.delete(jobId);
        _hideIfEmpty();
    }

    /** sub-stage 의 상태 아이콘 */
    function _stageIcon(sub, stale) {
        if (sub.status === 'done') return '✅';
        if (sub.status === 'error') return '❌';
        if (sub.status === 'running') return stale ? '⚠️' : '🔄';
        return '⏸';  // pending
    }

    /** 단일 sub-stage 렌더 갱신 */
    function _renderSubStage(st, stageKey) {
        const sub = st.stages[stageKey];
        const row = st.row;
        if (!row) return;
        const $el = row.querySelector(`[data-stage="${stageKey}"]`);
        if (!$el) return;

        const $icon = $el.querySelector('[data-role="icon"]');
        const $bar  = $el.querySelector('[data-role="bar"]');
        const $fill = $bar?.querySelector('span');
        const $det  = $el.querySelector('[data-role="detail"]');

        // stale 판정: running 인데 마지막 progress 가 STALE_THRESHOLD_MS 이상 전이면.
        const stale = sub.status === 'running'
            && sub.lastProgressAt > 0
            && (Date.now() - sub.lastProgressAt) > STALE_THRESHOLD_MS;

        if ($icon) {
            $icon.textContent = _stageIcon(sub, stale);
            $icon.classList.toggle('stage-row__icon--spin', sub.status === 'running' && !stale);
        }

        // 진행률 (%) — done/total 우선, 아니면 status 기반 추정
        const pct = (sub.total > 0)
            ? Math.min(100, Math.round((sub.done / sub.total) * 100))
            : (sub.status === 'done' ? 100
              : sub.status === 'running' ? 50
              : sub.status === 'error' ? 100 : 0);
        if ($fill) $fill.style.width = pct + '%';

        if ($bar) {
            $bar.classList.remove(
                'stage-row__bar--running', 'stage-row__bar--done',
                'stage-row__bar--error', 'stage-row__bar--stale');
            if (stale) $bar.classList.add('stage-row__bar--stale');
            else if (sub.status === 'running') $bar.classList.add('stage-row__bar--running');
            else if (sub.status === 'done')    $bar.classList.add('stage-row__bar--done');
            else if (sub.status === 'error')   $bar.classList.add('stage-row__bar--error');
        }

        if ($det) {
            const parts = [];

            if (sub.status === 'pending') {
                parts.push('대기 중');
            } else {
                if (sub.subStage) parts.push(sub.subStage);
                if (sub.total > 0) parts.push(`${sub.done}/${sub.total} ligand`);
                if (sub.startedAt > 0) {
                    const el = Math.floor((Date.now() - sub.startedAt) / 1000);
                    parts.push(`경과 ${_fmtDuration(el)}`);
                }
                if (sub.lastProgressAt > 0) {
                    const sinceMs = Date.now() - sub.lastProgressAt;
                    const sinceSec = Math.floor(sinceMs / 1000);
                    if (stale) {
                        parts.unshift(`⚠️ ${Math.floor(sinceSec / 60)}분간 신호 없음 — 서버 재시작/태스크 손실 의심`);
                    } else if (sub.status === 'running' && sinceSec >= 5) {
                        parts.push(`마지막 신호 ${sinceSec}s 전`);
                    }
                }
                if (sub.lastDetail && parts.indexOf(sub.lastDetail) < 0) {
                    parts.push(sub.lastDetail);
                }
            }
            $det.textContent = parts.join(' · ');
        }
    }

    function _fmtDuration(sec) {
        if (sec < 60) return `${sec}s`;
        const m = Math.floor(sec / 60);
        const s = sec % 60;
        if (m < 60) return `${m}m ${s}s`;
        const h = Math.floor(m / 60);
        return `${h}h ${m % 60}m`;
    }

    /** Overall 상태 라벨 갱신 */
    function _renderOverall(st) {
        const $ov = st.row?.querySelector('[data-role="overall"]');
        if (!$ov) return;
        const order = ['af3', 'smina', 'rank'];
        let active = null;
        for (const key of order) {
            if (st.stages[key].status === 'running') { active = key; break; }
        }
        const allDone = order.every(k => st.stages[k].status === 'done');
        const anyErr  = order.some(k => st.stages[k].status === 'error');

        if (allDone) $ov.textContent = '✅ 완료';
        else if (active) $ov.textContent = `🔄 ${active}`;
        else if (anyErr) $ov.textContent = '❌ 오류 — 일부 단계 실패';
        else $ov.textContent = st.overallStatus;
    }

    /** 한 sub-stage 의 patch 적용 + 렌더 */
    function _patchStage(jobId, stageKey, patch) {
        const st = _ensureRow(jobId);
        if (!st) return;
        const sub = st.stages[stageKey];
        if (!sub) return;
        Object.assign(sub, patch);
        if (sub.status === 'running' && sub.startedAt === 0) sub.startedAt = Date.now();
        _renderSubStage(st, stageKey);
        _renderOverall(st);
    }

    /** 모든 sub-stage 갱신 (heartbeat 타이머용) */
    function _renderAll() {
        for (const st of jobs.values()) {
            for (const s of STAGES) _renderSubStage(st, s.key);
            _renderOverall(st);
        }
    }

    /**
     * 백엔드 event → sub-stage 라우팅.
     * event 문자열을 보고 어느 stage 에 어떤 patch 를 적용할지 결정.
     */
    function _routeEvent(jobId, msg) {
        // event 는 top-level (chat.py _bcast 가 forward) 또는 payload 안에.
        const ev = msg.event || msg.payload?.event || '';
        const subStage = msg.sub_stage || msg.payload?.sub_stage || '';
        const ligIndex = msg.ligand_index ?? msg.payload?.ligand_index;
        const ligTotal = msg.total_ligands ?? msg.payload?.total_ligands;
        const elapsedS = msg.elapsed_s ?? msg.payload?.elapsed_s;
        const nLig     = msg.n_ligands ?? msg.payload?.n_ligands;

        const now = Date.now();
        const detail = elapsedS != null ? `백엔드 경과 ${_fmtDuration(Math.round(elapsedS))}` : '';

        // AF3 sub-stage
        if (ev === 'screening.ligand_af3_started') {
            _patchStage(jobId, 'af3', {
                status: 'running',
                subStage: subStage || 'queueing',
                total: ligTotal ?? 0,
                done: ligIndex ?? 0,
                lastProgressAt: now,
                lastDetail: detail,
            });
            return;
        }
        if (ev === 'screening.ligand_af3_progress') {
            _patchStage(jobId, 'af3', {
                status: 'running',
                subStage: subStage || 'running',
                total: ligTotal ?? jobs.get(jobId)?.stages.af3.total ?? 0,
                done: ligIndex ?? jobs.get(jobId)?.stages.af3.done ?? 0,
                lastProgressAt: now,
                lastDetail: detail,
            });
            return;
        }
        if (ev === 'screening.ligand_af3_done' || ev === 'screening.ligand_af3_failed') {
            const cur = jobs.get(jobId)?.stages.af3;
            _patchStage(jobId, 'af3', {
                status: 'running',
                subStage: subStage || cur?.subStage || 'done',
                done: (cur?.done ?? 0) + 1,
                total: cur?.total ?? 0,
                lastProgressAt: now,
                lastDetail: detail,
            });
            return;
        }

        // Smina stage (use_openmm=False: AF3 직후 바로 smina --minimize)
        if (ev === 'screening.ligand_smina_started') {
            // AF3 마무리 표시 (OpenMM 단계 제거됨 — AF3 → smina 직접 전환)
            const cur = jobs.get(jobId)?.stages.af3;
            if (cur && cur.status === 'running') {
                _patchStage(jobId, 'af3', { status: 'done', lastProgressAt: now });
            }
            _patchStage(jobId, 'smina', {
                status: 'running',
                subStage: subStage || 'minimize',
                total: ligTotal ?? jobs.get(jobId)?.stages.smina.total ?? 0,
                done: ligIndex ?? jobs.get(jobId)?.stages.smina.done ?? 0,
                lastProgressAt: now,
            });
            return;
        }
        if (ev === 'screening.ligand_smina_done') {
            const cur = jobs.get(jobId)?.stages.smina;
            _patchStage(jobId, 'smina', {
                status: 'running',
                done: (cur?.done ?? 0) + 1,
                total: cur?.total ?? 0,
                lastProgressAt: now,
            });
            return;
        }

        // Ranking + LLM
        if (ev === 'screening.ranking_done') {
            // smina 종료 보장
            const sm = jobs.get(jobId)?.stages.smina;
            if (sm && sm.status === 'running') _patchStage(jobId, 'smina',  { status: 'done', lastProgressAt: now });
            _patchStage(jobId, 'rank', {
                status: 'running',
                subStage: 'composite_ranking',
                lastProgressAt: now,
            });
            return;
        }
        if (ev === 'screening.llm_summary_done') {
            _patchStage(jobId, 'rank', {
                status: 'done',
                subStage: 'llm_summary',
                lastProgressAt: now,
            });
            return;
        }

        // stage_change (broad) — 정보용 fallback
        if (ev === 'screening.stage_change') {
            const to = msg.to_stage || msg.payload?.to_stage || '';
            // 'openmm', 'rank' 같은 광역 stage 라벨로 들어옴 — 이미 위에서 더 정교한 이벤트로 처리됨.
            // 여기서는 추가 작업 없음.
            return;
        }
    }

    /* ── Bus 이벤트 진입점 ── */
    Bus.addEventListener('screening:started', (ev) => {
        const d = ev.detail || {};
        const jobId = d.jobId || d.job_id || AppState.activeJobId;
        if (!jobId) return;
        AppState.activeJobId = jobId;
        const st = _ensureRow(jobId);
        if (!st) return;
        st.overallStatus = 'running';
        // AF3 가 첫 stage 이므로 즉시 running 표시 (실제 ligand_af3_started 가 오기 전 plate spinner)
        _patchStage(jobId, 'af3', {
            status: 'running',
            subStage: 'preparing',
            total: d.totalLigands ?? d.total_ligands ?? 0,
            lastProgressAt: Date.now(),
        });
    });

    Bus.addEventListener('screening:progress', (ev) => {
        const d = ev.detail || {};
        const jobId = d.job_id || d.jobId || AppState.activeJobId;
        if (!jobId) return;
        _routeEvent(jobId, d);
    });

    Bus.addEventListener('screening:complete', (ev) => {
        const d = ev.detail || {};
        const jobId = d.job_id || d.jobId || AppState.activeJobId;
        if (!jobId) return;
        const st = _ensureRow(jobId);
        if (!st) return;
        st.overallStatus = 'done';
        // 미종료 sub-stage 들을 done 으로 마무리
        for (const s of STAGES) {
            const sub = st.stages[s.key];
            if (sub.status === 'running') {
                _patchStage(jobId, s.key, { status: 'done', lastProgressAt: Date.now() });
            }
        }
        _renderOverall(st);
        setTimeout(() => _removeRow(jobId), 30000);
    });

    Bus.addEventListener('screening:error', (ev) => {
        const d = ev.detail || {};
        const jobId = d.job_id || d.jobId || AppState.activeJobId;
        if (!jobId) return;
        const st = _ensureRow(jobId);
        if (!st) return;
        st.overallStatus = 'error';
        // 현재 running 인 sub-stage 를 error 로 마킹
        for (const s of STAGES) {
            const sub = st.stages[s.key];
            if (sub.status === 'running') {
                _patchStage(jobId, s.key, { status: 'error', lastDetail: d.message || d.error || '오류' });
                break;
            }
        }
        _renderOverall(st);
    });

    Bus.addEventListener('job:submitted', (ev) => {
        const jobId = ev.detail?.jobId || ev.detail?.job_id;
        if (!jobId) return;
        _ensureRow(jobId);
    });

    /* ── 닫기 ── */
    $close?.addEventListener('click', () => {
        for (const [jid, st] of Array.from(jobs.entries())) {
            if (st.overallStatus === 'done' || st.overallStatus === 'error') _removeRow(jid);
        }
        _hideIfEmpty();
    });

    // 1초마다 모든 sub-stage 재렌더 (경과시간 / stale 판정 갱신)
    setInterval(_renderAll, 1000);

    window.JobsPanel = {
        addJob: (jobId) => _ensureRow(jobId),
        removeJob: _removeRow,
    };
})();

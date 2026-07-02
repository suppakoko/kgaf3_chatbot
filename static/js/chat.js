/**
 * afmm_chat — Phase 1 ChatClient
 * af3_chatbot 패턴 계승 + VS 메시지 타입 확장
 * frontend_plan.md §4.2, §5, §6
 */
'use strict';

class ChatClient {
    constructor() {
        this.ws = null;
        this.sessionId = this._genId();
        this.reconnectInterval = 5000;
        this.reconnectTimer = null;
        this._reconnectCount = 0;
        this._maxReconnect = 10;
        this._processing = false;
        this._activeBubble = null;
        this._activeMsgId = null;
        // ── 모드: "afmm" (기본 가상스크리닝) | "graphrag" (Neo4j 지식그래프) ──
        this._mode = (localStorage.getItem('afmm_chat.mode') === 'graphrag')
            ? 'graphrag' : 'afmm';

        // DOM refs
        this.msgsEl      = document.getElementById('messages');
        this.inputEl     = document.getElementById('chat-input');
        this.sendBtn     = document.getElementById('btn-send');
        this.sbar        = document.getElementById('status-bar');
        this.sDot        = document.getElementById('status-dot');
        this.sTxt        = document.getElementById('status-text');
        this.fileInput   = document.getElementById('file-input');
        this.modelSelect = document.getElementById('model-selector');
        this.modeToggle  = document.getElementById('mode-toggle');
        this.inputContainer = document.getElementById('input-area-container');

        // AppState 동기화
        AppState.sessionId    = this.sessionId;
        AppState.selectedModel = this.modelSelect ? this.modelSelect.value : null;
        AppState.mode = this._mode;

        this._bindEvents();
        this._applyMode(this._mode); // 초기 UI 반영
        this.connect();
    }

    /* ── 세션 전환 / 새 대화 시 호출되는 transient 상태 리셋 ──
       Sidebar 가 session_id 만 갈아끼우고 WS 를 재연결할 때 ChatClient 내부의
       진행중 스트리밍/큐 상태가 그대로 남아 다음 응답이 엉뚱한 버블에 덧붙거나
       _processing=true 잠금 때문에 입력이 disabled 로 유지되는 문제를 방지. */
    _resetTransient() {
        this._activeBubble = null;
        this._activeMsgId = null;
        this._activeBuffer = '';
        this._graphragCards = {};
        this._progressBubble = null;
        this._lastUserQuery = null;
        this._removeTyping();
        this._setProcessing(false);
    }

    /* ── 모드 토글 ── */
    _applyMode(mode) {
        this._mode = (mode === 'graphrag') ? 'graphrag' : 'afmm';
        AppState.mode = this._mode;
        localStorage.setItem('afmm_chat.mode', this._mode);

        // 토글 버튼 상태
        if (this.modeToggle) {
            this.modeToggle.querySelectorAll('.mode-toggle__btn').forEach(btn => {
                const active = btn.dataset.mode === this._mode;
                btn.classList.toggle('mode-toggle__btn--active', active);
                btn.setAttribute('aria-selected', active ? 'true' : 'false');
            });
        }

        // 입력 영역 컨테이너 data-mode
        if (this.inputContainer) this.inputContainer.dataset.mode = this._mode;

        // 입력 placeholder 변경
        if (this.inputEl) {
            this.inputEl.placeholder = (this._mode === 'graphrag')
                ? '지식그래프 질문 입력... (예: ROCK1 에 활성 있는 천연물 상위 20개)'
                : '메시지를 입력하세요... (Enter 전송, Shift+Enter 줄바꿈)';
        }

        // 환영 패널 mode 별 표시 분기
        document.querySelectorAll('[data-welcome-mode]').forEach(el => {
            el.hidden = (el.getAttribute('data-welcome-mode') !== this._mode);
        });

        Bus.dispatchEvent(new CustomEvent('mode:changed', { detail: { mode: this._mode } }));
    }

    /* ── ID 생성 (crypto.randomUUID 미사용: HTTP 컨텍스트 호환) ── */
    _genId() {
        const a = new Uint8Array(8);
        (window.crypto || { getRandomValues: (b) => { for (let i=0;i<8;i++) b[i]=Math.floor(Math.random()*256); return b; }}).getRandomValues(a);
        return Array.from(a, b => b.toString(16).padStart(2,'0')).join('');
    }

    /* ── 이벤트 바인딩 ── */
    _bindEvents() {
        this.sendBtn?.addEventListener('click', () =>
            this._processing ? this._stop() : this._send());

        this.inputEl?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!this._processing) this._send(); }
        });
        this.inputEl?.addEventListener('input', () => {
            this.inputEl.style.height = 'auto';
            this.inputEl.style.height = Math.min(this.inputEl.scrollHeight, 140) + 'px';
        });

        document.getElementById('btn-upload')?.addEventListener('click', () => this.fileInput?.click());
        this.fileInput?.addEventListener('change', (e) => {
            if (e.target.files[0]) this._uploadLibrary(e.target.files[0]);
            e.target.value = '';
        });

        this.modelSelect?.addEventListener('change', (e) => {
            AppState.selectedModel = e.target.value;
            Bus.dispatchEvent(new CustomEvent('model:changed', { detail: { model: e.target.value } }));
        });

        document.getElementById('btn-close-preview')?.addEventListener('click', () => {
            document.getElementById('upload-preview').hidden = true;
        });

        // 모드 토글 버튼들 — data-mode 속성으로 모드 결정
        this.modeToggle?.querySelectorAll('.mode-toggle__btn').forEach(btn => {
            btn.addEventListener('click', () => {
                this._applyMode(btn.dataset.mode);
            });
        });
    }

    /* ── WebSocket ── */
    connect() {
        this._setStatus('connecting');
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        try {
            this.ws = new WebSocket(`${proto}//${location.host}/ws/chat/${this.sessionId}`);
        } catch (e) { this._scheduleReconnect(); return; }

        this.ws.onopen = () => {
            this._reconnectCount = 0;
            clearTimeout(this.reconnectTimer);
            AppState.wsConnected = true;
            this._setStatus('connected');
            Bus.dispatchEvent(new CustomEvent('ws:connected'));
        };
        this.ws.onmessage = (ev) => {
            try { this._route(JSON.parse(ev.data)); }
            catch (e) { console.warn('[chat] parse error', e); }
        };
        this.ws.onclose = () => {
            AppState.wsConnected = false;
            Bus.dispatchEvent(new CustomEvent('ws:disconnected'));
            this._scheduleReconnect();
        };
        this.ws.onerror = (e) => console.error('[chat] ws error', e);
    }

    _scheduleReconnect() {
        if (this._reconnectCount >= this._maxReconnect) { this._setStatus('disconnected'); return; }
        this._reconnectCount++;
        this._setStatus('reconnecting');
        Bus.dispatchEvent(new CustomEvent('ws:reconnecting', {
            detail: { attempt: this._reconnectCount, max: this._maxReconnect }
        }));
        this.reconnectTimer = setTimeout(() => this.connect(), this.reconnectInterval);
    }

    /* ── 메시지 라우팅 ── */
    _route(msg) {
        switch (msg.type) {
            case 'chat.assistant_chunk': this._appendChunk(msg); break;
            case 'chat.assistant_done':  this._finalize(msg); break;
            case 'error': this._showError(msg.message || msg.error || '오류'); break;
            case 'graphrag.stage':
                this._appendGraphragStage(msg);
                break;
            case 'screening.submitted':
                this._handleScreeningSubmitted(msg);
                Bus.dispatchEvent(new CustomEvent('screening:started', { detail: msg }));
                break;
            case 'screening.progress':
                this._appendScreeningProgress(msg);
                Bus.dispatchEvent(new CustomEvent('screening:progress', { detail: msg }));
                break;
            case 'screening.complete':
                this._appendScreeningComplete(msg);
                Bus.dispatchEvent(new CustomEvent('screening:complete', { detail: msg }));
                break;
            case 'screening.error':
                this._showError(`스크리닝 오류: ${msg.message || '-'}`);
                Bus.dispatchEvent(new CustomEvent('screening:error', { detail: msg }));
                break;
            case 'ui.request_upload':   this.fileInput?.click(); break;
            default: console.warn('[chat] unknown type:', msg.type);
        }
    }

    /* ── GraphRAG 단계별 진행 카드 ── */
    _appendGraphragStage(msg) {
        const p = msg.payload || {};
        const step = p.step;
        const status = p.status;
        if (!step) return;

        // 진행 카드 생성/획득 — 메시지(request_id) 단위로 1개
        const reqId = msg.request_id || 'graphrag';
        let card = this._graphragCards?.[reqId];
        if (!card) {
            this._removeTyping(); // 일반 타이핑 인디케이터는 제거
            card = this._createGraphragCard(reqId);
            (this._graphragCards = this._graphragCards || {})[reqId] = card;
        }

        // complete step 은 카드 헤더에 총 소요시간 + 상태 요약을 박는다
        if (step === 'complete') {
            this._finalizeGraphragCard(card, p);
            return;
        }

        // 단계 라인 갱신
        let line = card.lines[step];
        if (!line) {
            line = this._createGraphragLine(step);
            card.linesEl.appendChild(line.row);
            card.lines[step] = line;
        }
        this._updateGraphragLine(line, step, status, p);
        this._scrollBottom();
    }

    _createGraphragCard(reqId) {
        const row = document.createElement('div');
        row.className = 'msg-row msg-row--assistant';
        const bub = document.createElement('div');
        bub.className = 'msg-bubble msg-bubble--assistant graphrag-card';
        bub.dataset.graphragReq = reqId;

        const head = document.createElement('div');
        head.className = 'graphrag-card__head';
        head.innerHTML = '<span class="graphrag-card__icon">🧬</span>'
            + '<span class="graphrag-card__title">GraphRAG 진행 상황</span>'
            + '<span class="graphrag-card__elapsed"></span>';
        bub.appendChild(head);

        const linesEl = document.createElement('div');
        linesEl.className = 'graphrag-card__lines';
        bub.appendChild(linesEl);

        row.appendChild(bub);
        this.msgsEl.appendChild(row);
        this._scrollBottom();

        return { row, bub, head, linesEl, lines: {}, startedAt: Date.now() };
    }

    _createGraphragLine(step) {
        const labels = {
            cypher_gen:   '🧠 Cypher 생성',
            neo4j_exec:   '🔍 Neo4j 실행',
            answer_synth: '📝 답변 합성',
        };
        const row = document.createElement('div');
        row.className = 'graphrag-line';
        row.dataset.step = step;

        const head = document.createElement('div');
        head.className = 'graphrag-line__head';
        head.innerHTML = `
            <span class="graphrag-line__icon">⏳</span>
            <span class="graphrag-line__label">${labels[step] || step}</span>
            <span class="graphrag-line__status">대기 중</span>
            <span class="graphrag-line__duration"></span>
        `.trim();

        const detail = document.createElement('div');
        detail.className = 'graphrag-line__detail';
        detail.hidden = true;

        row.appendChild(head);
        row.appendChild(detail);
        return {
            row,
            iconEl:     head.querySelector('.graphrag-line__icon'),
            statusEl:   head.querySelector('.graphrag-line__status'),
            durationEl: head.querySelector('.graphrag-line__duration'),
            detailEl:   detail,
        };
    }

    _updateGraphragLine(line, step, status, payload) {
        const STATUS = {
            running: { icon: '⏳', label: '진행 중',  cls: 'graphrag-line--running' },
            done:    { icon: '✅', label: '완료',    cls: 'graphrag-line--done' },
            warning: { icon: '⚠️', label: '경고',    cls: 'graphrag-line--warning' },
            error:   { icon: '❌', label: '실패',    cls: 'graphrag-line--error' },
            skipped: { icon: '⏭️', label: '건너뜀',  cls: 'graphrag-line--skipped' },
        };
        const s = STATUS[status] || STATUS.running;

        // 모든 상태 클래스 제거 후 새 상태 적용
        Object.values(STATUS).forEach(v => line.row.classList.remove(v.cls));
        line.row.classList.add(s.cls);
        line.iconEl.textContent = s.icon;
        line.statusEl.textContent = s.label;

        if (payload.duration_s != null) {
            line.durationEl.textContent = `${payload.duration_s}s`;
        }

        // 단계별 상세 정보 렌더
        const detailParts = [];

        if (payload.message) {
            detailParts.push(`<div class="graphrag-line__msg">${this._esc(payload.message)}</div>`);
        }
        if (payload.hint) {
            detailParts.push(`<div class="graphrag-line__hint">💡 ${this._esc(payload.hint)}</div>`);
        }
        if (payload.error) {
            const errType = payload.error_type ? `[${this._esc(payload.error_type)}] ` : '';
            detailParts.push(
                `<div class="graphrag-line__err">${errType}${this._esc(payload.error)}</div>`
            );
        }
        if (step === 'cypher_gen' && payload.cypher) {
            detailParts.push(
                `<pre class="graphrag-line__cypher"><code>${this._esc(payload.cypher)}</code></pre>`
            );
        }
        if (step === 'neo4j_exec' && payload.row_count != null) {
            const n = payload.row_count;
            const stat = n === 0 ? '0 rows (no match)' : `${n} rows`;
            detailParts.push(`<div class="graphrag-line__rows">${stat}</div>`);
            if (Array.isArray(payload.sample) && payload.sample.length) {
                detailParts.push(
                    `<details class="graphrag-line__sample">
                        <summary>샘플 ${payload.sample.length}개 미리보기</summary>
                        <pre><code>${this._esc(JSON.stringify(payload.sample, null, 2))}</code></pre>
                    </details>`
                );
            }
        }
        if (step === 'answer_synth' && (payload.tokens_in || payload.tokens_out)) {
            detailParts.push(
                `<div class="graphrag-line__tokens">tokens: ${payload.tokens_in || 0} in / ${payload.tokens_out || 0} out</div>`
            );
        }

        if (detailParts.length) {
            line.detailEl.innerHTML = detailParts.join('');
            line.detailEl.hidden = false;
        }
    }

    _finalizeGraphragCard(card, payload) {
        const FINAL = {
            ok:             { icon: '✅', label: '완료',           cls: 'graphrag-card--ok' },
            empty:          { icon: 'ℹ️', label: '결과 없음',      cls: 'graphrag-card--empty' },
            cypher_failed:  { icon: '❌', label: 'Cypher 생성 실패', cls: 'graphrag-card--error' },
            neo4j_failed:   { icon: '❌', label: 'Neo4j 실행 실패',  cls: 'graphrag-card--error' },
            synth_failed:   { icon: '❌', label: '답변 합성 실패',   cls: 'graphrag-card--error' },
            error:          { icon: '❌', label: '실패',            cls: 'graphrag-card--error' },
        };
        // 성공은 'ok'/'empty' 만. 그 외 인식 못 하는 status 는 실패로 렌더(초록 오탐 방지).
        const f = FINAL[payload.status]
            || (payload.status === 'ok' ? FINAL.ok : FINAL.error);
        card.bub.classList.add(f.cls);

        const elapsed = card.head.querySelector('.graphrag-card__elapsed');
        if (elapsed) {
            const dt = payload.duration_s != null ? `${payload.duration_s}s` : '';
            elapsed.textContent = `${f.icon} ${f.label}${dt ? ` · ${dt}` : ''}`;
        }
        // 카드 헤더 클릭으로 전체 카드를 접을 수 있게 (성공 시 자동 collapse 까진 안 함)
        card.head.classList.add('graphrag-card__head--collapsible');
        card.head.addEventListener('click', () => {
            card.linesEl.hidden = !card.linesEl.hidden;
            card.bub.classList.toggle('graphrag-card--collapsed', card.linesEl.hidden);
        });
    }

    /* ── 스크리닝 자동 시작 핸들러 ── */
    _handleScreeningSubmitted(msg) {
        AppState.activeJobId = msg.job_id || msg.payload?.job_id;
        AppState.currentJobId = AppState.activeJobId;
        // 결과 패널은 complete 시점에 열림. 여기서는 진행 패널이 jobs_panel.js 가 자동 표시.
        const p = msg.payload || {};
        Bus.dispatchEvent(new CustomEvent('screening:started', {
            detail: {
                job_id: msg.job_id || p.job_id,
                jobId: msg.job_id || p.job_id,
                total_ligands: p.n_ligands,
                stage: 'submitted',
            }
        }));
    }

    _appendScreeningProgress(msg) {
        // 채팅 버블 안에 stage 진행 라인을 inline 으로 추가하여 사용자가 가시적으로 확인.
        const stage = msg.stage || msg.to_stage || msg.payload?.event || '';
        const ligand = msg.ligand_id ? ` · ${String(msg.ligand_id).slice(0,8)}` : '';
        const line = `▸ ${stage}${ligand}`;
        this._ensureProgressBubble();
        const bub = this._progressBubble;
        if (bub) {
            const div = document.createElement('div');
            div.className = 'progress-line';
            div.textContent = line;
            bub.appendChild(div);
            // 너무 많이 쌓이면 오래된 것 제거 (최근 20줄 유지)
            while (bub.children.length > 20) bub.removeChild(bub.firstChild);
            this._scrollBottom();
        }
    }

    _appendScreeningComplete(msg) {
        const status = msg.status || 'unknown';
        const total = msg.total ?? '-';
        this._ensureProgressBubble();
        const bub = this._progressBubble;
        if (bub) {
            const div = document.createElement('div');
            div.className = 'progress-line progress-line--done';
            div.textContent = `✅ 완료 · status=${status} · ${total} ligand`;
            bub.appendChild(div);
        }
        this._progressBubble = null;
        this._scrollBottom();
    }

    _ensureProgressBubble() {
        if (this._progressBubble && document.body.contains(this._progressBubble)) return;
        const row = document.createElement('div');
        row.className = 'msg-row msg-row--assistant';
        const bub = document.createElement('div');
        bub.className = 'msg-bubble msg-bubble--assistant msg-bubble--progress';
        const head = document.createElement('div');
        head.className = 'progress-head';
        head.textContent = '🔄 스크리닝 진행 (실시간)';
        bub.appendChild(head);
        row.appendChild(bub);
        this.msgsEl.appendChild(row);
        this._progressBubble = bub;
    }

    /* ── 전송 / 정지 ── */
    _send() {
        const text = this.inputEl?.value.trim();
        if (!text) return;
        if (!AppState.wsConnected || this.ws?.readyState !== WebSocket.OPEN) {
            this._toast('WebSocket 미연결', 'disconnected'); return;
        }
        this._appendUserBubble(text);
        this.inputEl.value = '';
        this.inputEl.style.height = 'auto';

        // MD export 용 컨텍스트 stash (질문 + 모드 + 모델 + 시각)
        this._lastUserQuery = {
            text,
            mode: this._mode || AppState.mode || 'afmm',
            model: AppState.selectedModel,
            sentAt: new Date().toISOString(),
        };

        this.ws.send(JSON.stringify({
            type: 'chat.user_message',
            payload: {
                text,
                model: AppState.selectedModel,
                mode: this._mode || AppState.mode || 'afmm',
            }
        }));
        this._setProcessing(true);
        this._showTyping();
        Bus.dispatchEvent(new CustomEvent('chat:user_sent', {
            detail: { text, sessionId: this.sessionId }
        }));
    }

    _stop() {
        this.ws?.readyState === WebSocket.OPEN &&
            this.ws.send(JSON.stringify({ type: 'chat.cancel' }));
        this._setProcessing(false);
        this._removeTyping();
    }

    /* ── 마크다운 렌더링 헬퍼 ── */
    _renderMarkdown(text) {
        if (!window.marked || !window.DOMPurify) {
            console.warn('[chat] marked/DOMPurify not loaded — falling back to plain text');
            return null; // 호출자가 null 체크 후 textNode 폴백 처리
        }
        const html = window.marked.parse(text, { gfm: true, breaks: true });
        return window.DOMPurify.sanitize(html);
    }

    /* ── 스트리밍 ── */
    _appendChunk(msg) {
        this._removeTyping();
        // WS envelope ({type, request_id, payload: {delta, msg_id}}) 와
        // 평면 형태 ({delta, msg_id}) 모두 호환. envelope.payload 가 우선.
        const p = msg.payload || {};
        const delta = (p.delta != null ? p.delta : msg.delta) || '';
        const msgId = p.msg_id != null ? p.msg_id : (msg.msg_id != null ? msg.msg_id : msg.request_id);

        if (!this._activeBubble || this._activeMsgId !== msgId) {
            this._activeMsgId = msgId;
            this._activeBuffer = '';
            const row = document.createElement('div');
            row.className = 'msg-row msg-row--assistant';
            const bub = document.createElement('div');
            bub.className = 'msg-bubble msg-bubble--assistant';
            bub.dataset.msgId = msgId || '';
            row.appendChild(bub);
            this.msgsEl.appendChild(row);
            this._activeBubble = bub;
        }
        this._activeBuffer += delta;
        const html = this._renderMarkdown(this._activeBuffer);
        if (html !== null) {
            this._activeBubble.innerHTML = html;
        } else {
            // CDN 로드 실패 폴백: 기존 textNode 방식
            this._activeBubble.appendChild(document.createTextNode(delta));
        }
        this._scrollBottom();
    }

    _finalize(msg) {
        if (this._activeBubble && this._activeBuffer) {
            const html = this._renderMarkdown(this._activeBuffer);
            if (html !== null) {
                this._activeBubble.innerHTML = html;
            }
            // ── MD 저장 버튼 부착 ─────────────────────────────────────────
            // 답변 본문(activeBuffer) + 직전 user query + done 메타(model, tokens)
            // + 같은 message row 직전의 graphrag-card 안의 cypher/rows 를 함께 export.
            const doneMeta = (msg && (msg.payload || msg)) || {};
            this._attachDownloadButton(this._activeBubble, this._activeBuffer, doneMeta);
        }
        this._activeBubble = null; this._activeMsgId = null; this._activeBuffer = '';
        this._setProcessing(false); this._removeTyping(); this._scrollBottom();
    }

    /* ── MD 저장 버튼 ── */
    _attachDownloadButton(bubble, answerMarkdown, doneMeta) {
        if (!bubble || !answerMarkdown) return;
        // 중복 부착 방지
        if (bubble.querySelector('.msg-bubble__actions')) return;

        const ctx = this._lastUserQuery || {};
        const userText = ctx.text || '';
        const mode = ctx.mode || 'afmm';
        const sentAt = ctx.sentAt || new Date().toISOString();
        const model = doneMeta.model || ctx.model || '';
        const tokensIn = doneMeta.tokens_in ?? null;
        const tokensOut = doneMeta.tokens_out ?? null;

        // GraphRAG 진행 카드(직전 형제 행)에서 cypher / row_count / 소요시간 추출
        let cypher = '';
        let rowCount = null;
        let cardDuration = '';
        const prevRow = bubble.closest('.msg-row')?.previousElementSibling;
        const card = prevRow?.querySelector('.graphrag-card');
        if (card) {
            const cypherEl = card.querySelector('.graphrag-line__cypher code');
            if (cypherEl) cypher = cypherEl.textContent || '';
            const rowsEl = card.querySelector('.graphrag-line[data-step="neo4j_exec"] .graphrag-line__rows');
            if (rowsEl) {
                const m = (rowsEl.textContent || '').match(/(\d+)/);
                if (m) rowCount = parseInt(m[1], 10);
            }
            const elapsedEl = card.querySelector('.graphrag-card__elapsed');
            if (elapsedEl) cardDuration = (elapsedEl.textContent || '').trim();
        }

        const actions = document.createElement('div');
        actions.className = 'msg-bubble__actions';

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn--md-save';
        btn.title = '답변을 Markdown 파일로 저장';
        btn.setAttribute('aria-label', '답변을 Markdown 파일로 저장');
        btn.innerHTML = '<span aria-hidden="true">📥</span> MD 저장';

        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const md = this._buildExportMarkdown({
                userText, mode, sentAt, model, tokensIn, tokensOut,
                answerMarkdown, cypher, rowCount, cardDuration,
            });
            const filename = this._buildExportFilename(mode, userText, sentAt);
            this._triggerDownload(filename, md);

            // 시각적 피드백 — 잠깐 "저장됨" 표시
            const orig = btn.innerHTML;
            btn.innerHTML = '<span aria-hidden="true">✅</span> 저장됨';
            btn.disabled = true;
            setTimeout(() => {
                btn.innerHTML = orig;
                btn.disabled = false;
            }, 1500);
        });

        actions.appendChild(btn);
        bubble.appendChild(actions);
    }

    _buildExportMarkdown(o) {
        const lines = [];
        const title = (o.userText || 'Chat answer').split('\n')[0].slice(0, 80);
        lines.push(`# ${title}`);
        lines.push('');

        // 메타 정보 블록
        lines.push('> **모드**: ' + (o.mode === 'graphrag' ? '🧬 GraphRAG' : '🧪 AlphaFold'));
        if (o.model)    lines.push('> **모델**: `' + o.model + '`');
        if (o.sentAt)   lines.push('> **시각**: ' + o.sentAt);
        if (o.tokensIn != null || o.tokensOut != null) {
            lines.push(`> **토큰**: ${o.tokensIn ?? '-'} in / ${o.tokensOut ?? '-'} out`);
        }
        if (o.cardDuration) lines.push('> **소요시간**: ' + o.cardDuration);
        if (o.rowCount != null) lines.push('> **결과 행 수**: ' + o.rowCount);
        lines.push('');

        // 질문
        if (o.userText) {
            lines.push('## 질문');
            lines.push('');
            lines.push(o.userText);
            lines.push('');
        }

        // 답변 본문
        lines.push('## 답변');
        lines.push('');
        lines.push(o.answerMarkdown || '');
        lines.push('');

        // GraphRAG 메타 (Cypher)
        if (o.mode === 'graphrag' && o.cypher) {
            lines.push('---');
            lines.push('');
            lines.push('## 실행된 Cypher');
            lines.push('');
            lines.push('```cypher');
            lines.push(o.cypher);
            lines.push('```');
            lines.push('');
        }

        lines.push('---');
        lines.push('_AlphaFold Virtual Screener / GraphRAG 챗봇 export_');
        return lines.join('\n');
    }

    _buildExportFilename(mode, userText, sentAt) {
        const stamp = (sentAt || new Date().toISOString())
            .replace(/[:.]/g, '-').replace('T', '_').replace('Z', '');
        const slug = (userText || 'chat')
            .replace(/\s+/g, '_')
            // 윈도우/맥/리눅스 모두 안전한 문자만 유지 + 한글 허용
            .replace(/[^\w가-힣_-]/g, '')
            .slice(0, 40) || 'chat';
        const prefix = mode === 'graphrag' ? 'graphrag' : 'afmm';
        return `${prefix}_${stamp.slice(0, 19)}_${slug}.md`;
    }

    _triggerDownload(filename, text) {
        try {
            const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(() => URL.revokeObjectURL(url), 4000);
        } catch (e) {
            console.error('[chat] md download failed', e);
            this._toast('MD 저장 실패', 'error');
        }
    }

    /* ── DOM 헬퍼 ── */
    _appendUserBubble(text) {
        document.getElementById('welcome-screen')?.remove();
        const row = document.createElement('div');
        row.className = 'msg-row msg-row--user';
        const bub = document.createElement('div');
        bub.className = 'msg-bubble msg-bubble--user';
        bub.textContent = text;
        row.appendChild(bub);
        this.msgsEl.appendChild(row);
        this._scrollBottom();
    }
    _showTyping() {
        if (document.getElementById('typing-indicator')) return;
        const row = document.createElement('div');
        row.className = 'msg-row msg-row--assistant'; row.id = 'typing-indicator';
        const bub = document.createElement('div');
        bub.className = 'msg-bubble msg-bubble--assistant';
        bub.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
        row.appendChild(bub); this.msgsEl.appendChild(row); this._scrollBottom();
    }
    _removeTyping() { document.getElementById('typing-indicator')?.remove(); }
    _showError(msg) {
        this._removeTyping();
        const row = document.createElement('div');
        row.className = 'msg-row msg-row--assistant';
        const bub = document.createElement('div');
        bub.className = 'msg-bubble msg-bubble--error';
        bub.textContent = '오류: ' + msg;
        row.appendChild(bub); this.msgsEl.appendChild(row);
        this._setProcessing(false); this._scrollBottom();
    }
    _toast(msg, cls) {
        if (!this.sbar) return;
        const prev = [this.sbar.textContent, this.sbar.className];
        this.sbar.textContent = msg;
        this.sbar.className = 'system-bar__chip system-bar__chip--status status-bar status-bar--' + (cls || 'error');
        setTimeout(() => { [this.sbar.textContent, this.sbar.className] = prev; }, 3000);
    }
    _scrollBottom() { if (this.msgsEl) this.msgsEl.scrollTop = this.msgsEl.scrollHeight; }

    _setProcessing(v) {
        this._processing = v;
        if (this.sendBtn) {
            this.sendBtn.textContent = v ? '정지' : t('btn.send');
            this.sendBtn.setAttribute('aria-label', v ? '생성 정지' : '메시지 전송');
        }
        if (this.inputEl) this.inputEl.disabled = v;
    }
    _setStatus(s) {
        const labels = { connecting: t('status.connecting'), connected: t('status.connected'),
                         disconnected: t('status.disconnected'), reconnecting: t('status.reconnecting') };
        if (this.sDot) this.sDot.className = `status-dot status-dot--${s}`;
        if (this.sTxt) this.sTxt.textContent = labels[s] || s;
        if (this.sbar) {
            this.sbar.textContent = labels[s] || s;
            // system-bar 내부 chip 클래스 보존
            this.sbar.className = `system-bar__chip system-bar__chip--status status-bar status-bar--${s}`;
        }
    }

    /* ── 라이브러리 업로드 ── */
    async _uploadLibrary(file) {
        const preview = document.getElementById('upload-preview');
        const info    = document.getElementById('upload-preview-info');
        document.getElementById('upload-preview-name').textContent = file.name;
        if (info) info.textContent = '업로드 중...';
        if (preview) preview.hidden = false;

        const fd = new FormData();
        fd.append('file', file);
        try {
            const res = await fetch('/api/library', { method: 'POST', body: fd });
            if (!res.ok) {
                const e = await res.text();
                if (info) info.innerHTML = `<span class="err">실패: ${this._esc(e)}</span>`; return;
            }
            const d = await res.json();
            AppState.uploadedLibraryId = d.library_id || d.id || null;
            if (info) info.innerHTML =
                `총 분자: <strong>${d.total_molecules ?? '?'}</strong> / 유효: <strong>${d.valid_molecules ?? '?'}</strong>`;
            Bus.dispatchEvent(new CustomEvent('library:uploaded', {
                detail: { libraryId: AppState.uploadedLibraryId, fileName: file.name,
                          total: d.total_molecules, valid: d.valid_molecules }
            }));
        } catch (e) {
            console.error('[chat] upload error', e);
            if (info) info.innerHTML = `<span class="err">네트워크 오류: ${this._esc(e.message)}</span>`;
        }
    }
    _esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
}

document.addEventListener('DOMContentLoaded', () => {
    window.chatClient = new ChatClient();

    /* ── Phase 2: 결과 카드 인라인 bubble 연결 ── */

    // _activeResultTable: 현재 screening 중인 잡에 해당하는 ResultTable 인스턴스
    // Smina 배지 업데이트 및 차트 렌더링에 사용됨
    let _activeResultTable = null;

    // sidebar.js 등 외부 모듈이 세션 복원 시 _activeResultTable을 주입할 수 있도록 setter export
    window.setActiveResultTable = (rt) => { _activeResultTable = rt; };

    // screening:complete → messages 컨테이너에 결과 카드 bubble 추가
    Bus.addEventListener('screening:complete', (ev) => {
        const detail = ev.detail || {};
        const jobId = detail.job_id || detail.jobId || AppState.activeJobId;
        if (!jobId) return;

        AppState.currentJobId = jobId;

        const msgsEl = document.getElementById('messages');
        if (!msgsEl) return;

        // mountResultCard가 ResultTable 인스턴스를 반환 — Smina 배지 업데이트에 활용
        _activeResultTable = mountResultCard(msgsEl, jobId);
    });

    // screening.ligand_smina_done WS 이벤트 → 현재 활성 결과 카드의 Smina 배지 업데이트
    Bus.addEventListener('screening:progress', (ev) => {
        const detail = ev.detail || {};
        if (detail.sub_type !== 'screening.ligand_smina_done') return;

        // 현재 활성 결과 버블의 sminaBadge 참조를 통해 업데이트
        // (정적 #smina-badge ID 대신 카드별 참조 사용)
        if (!_activeResultTable || !_activeResultTable._containerEl) return;
        const bubble = _activeResultTable._containerEl.closest('.msg-bubble--result');
        if (!bubble || !bubble._sminaBadge) return;

        const badge = bubble._sminaBadge;
        badge.classList.remove('hidden');
        const done = detail.done ?? detail.ligand_index ?? '?';
        const total = detail.total ?? detail.total_ligands ?? '?';
        badge.textContent = '';
        badge.appendChild(document.createTextNode(`Smina ${done}/${total}`));
    });

    /* ── Phase 3: 3D 뷰어 연결 ── */
    let viewer = null;

    /**
     * VSViewer 인스턴스를 지연 생성하고 오버레이를 표시한다.
     * @param {string} jobId
     * @param {string} ligandId
     * @param {string} chainId
     */
    async function openViewer(jobId, ligandId, chainId) {
        const overlay = document.getElementById('vs-viewer-overlay');
        if (!overlay) return;
        overlay.classList.remove('hidden');

        const titleEl = document.getElementById('vs-viewer-title');
        if (titleEl) {
            titleEl.textContent = '';
            titleEl.appendChild(document.createTextNode('3D 구조: ' + ligandId));
        }

        if (!viewer) {
            viewer = new VSViewer('vs-viewer');
        }
        try {
            await viewer.loadComplex(jobId, ligandId, chainId);
        } catch (err) {
            console.error('[Phase3] viewer 로드 오류', err);
        }
    }

    // 결과 테이블 행 클릭 → 3D 뷰어 열기
    Bus.addEventListener('result:row_clicked', (ev) => {
        const row = ev.detail || {};
        const jobId    = row.job_id    || AppState.currentJobId;
        const ligandId = row.ligand_id || row.mol_id;
        const chainId  = row.ligand_chain_id || 'L';
        if (!jobId || !ligandId) return;
        openViewer(jobId, ligandId, chainId);
    });

    // 뷰어 닫기
    document.getElementById('btn-close-viewer')?.addEventListener('click', () => {
        document.getElementById('vs-viewer-overlay')?.classList.add('hidden');
    });

    // 스크린샷 버튼
    document.getElementById('btn-viewer-screenshot')?.addEventListener('click', async () => {
        if (viewer) await viewer.screenshot();
    });

    /* ── Phase 3: 차트 패널 연결 ── */

    // screening:complete → 차트 데이터 준비 (패널은 토글 시 렌더링)
    let _chartRows = [];

    Bus.addEventListener('screening:complete', (ev) => {
        const detail = ev.detail || {};
        _chartRows = detail.top5_preview || [];
    });

    /**
     * 현재 _activeResultTable.data 로 차트를 렌더링한다.
     */
    async function renderCharts() {
        const rows = (_activeResultTable && _activeResultTable.data) ? _activeResultTable.data : _chartRows;
        if (!rows || rows.length === 0) return;

        const sminaVals  = rows.map(r => r.smina_affinity_kcal_mol ?? r.smina_affinity).filter(v => v != null);

        await Promise.all([
            renderSminaDistribution('chart-smina-dist', sminaVals),
            renderIptmVsSmina('chart-iptm-smina', rows),
        ]);
    }

    // 결과 카드 헤더의 차트 버튼 → Bus 이벤트 → 차트 패널 토글
    Bus.addEventListener('result:charts_toggle', async () => {
        const panel = document.getElementById('charts-panel');
        if (!panel) return;
        const isHidden = panel.classList.contains('hidden');
        panel.classList.toggle('hidden', !isHidden);
        if (isHidden) await renderCharts();
    });

    // 차트 패널 닫기
    document.getElementById('btn-close-charts')?.addEventListener('click', () => {
        document.getElementById('charts-panel')?.classList.add('hidden');
    });
});

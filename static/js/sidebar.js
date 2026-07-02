/**
 * afmm_chat — 좌측 사이드바
 * - 대화창 리스트 (최근 사용 순)
 * - 새 대화 시작
 * - 세션 전환 시 메시지 복원
 * - 사이드바 잡 리스트 (전역)
 */
'use strict';

(function () {
    const POLL_INTERVAL_MS = 8000;

    const $list   = document.getElementById('session-list');
    const $newBtn = document.getElementById('btn-new-chat');
    const $toggle = document.getElementById('btn-toggle-sidebar');
    const $sb     = document.getElementById('sidebar');
    const $sessLbl = document.getElementById('active-session-label');

    let pollTimer = null;
    let _busy = false;

    function _esc(s) {
        return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    function _shortId(id) {
        return (id || '').slice(0, 8);
    }

    function _formatRelative(iso) {
        if (!iso) return '';
        const t = new Date(iso).getTime();
        if (!t) return '';
        const sec = Math.max(0, (Date.now() - t) / 1000);
        if (sec < 60) return '방금';
        if (sec < 3600) return `${Math.floor(sec/60)}분 전`;
        if (sec < 86400) return `${Math.floor(sec/3600)}시간 전`;
        return `${Math.floor(sec/86400)}일 전`;
    }

    function _truncate(s, n) {
        if (!s) return '';
        s = String(s).replace(/\s+/g, ' ').trim();
        return s.length > n ? s.slice(0, n - 1) + '…' : s;
    }

    /* ── 세션 목록 ── */
    async function loadSessions() {
        if (_busy) return;
        _busy = true;
        try {
            const res = await fetch('/api/sessions?limit=100');
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const data = await res.json();
            renderSessions(data.sessions || []);
        } catch (e) {
            console.warn('[sidebar] loadSessions failed', e);
            if ($list) $list.innerHTML = '<div class="sidebar__empty">불러오기 실패</div>';
        } finally {
            _busy = false;
        }
    }

    function renderSessions(sessions) {
        if (!$list) return;
        const active = AppState.sessionId;
        if (!sessions.length) {
            $list.innerHTML = '<div class="sidebar__empty">대화가 아직 없습니다</div>';
            return;
        }
        const frag = document.createDocumentFragment();
        for (const s of sessions) {
            const div = document.createElement('div');
            div.className = 'session-item' + (s.session_id === active ? ' session-item--active' : '');
            div.dataset.sid = s.session_id;
            div.role = 'listitem';

            const title = document.createElement('div');
            title.className = 'session-item__title';
            title.textContent = _truncate(s.last_user_message, 42) || `대화 ${_shortId(s.session_id)}`;
            div.appendChild(title);

            const meta = document.createElement('div');
            meta.className = 'session-item__meta';
            const mc = document.createElement('span');
            mc.textContent = `${s.message_count ?? 0} 메시지`;
            const dt = document.createElement('span');
            dt.textContent = _formatRelative(s.last_seen_at);
            meta.appendChild(mc);
            meta.appendChild(dt);
            div.appendChild(meta);

            const del = document.createElement('button');
            del.className = 'session-item__del';
            del.type = 'button';
            del.title = '대화 삭제';
            del.setAttribute('aria-label', '대화 삭제');
            del.textContent = '×';
            del.addEventListener('click', (ev) => {
                ev.stopPropagation();
                _deleteSession(s.session_id);
            });
            div.appendChild(del);

            div.addEventListener('click', () => switchSession(s.session_id));
            frag.appendChild(div);
        }
        $list.innerHTML = '';
        $list.appendChild(frag);
        _updateSessionLabel(sessions);
    }

    function _updateSessionLabel(sessions) {
        if (!$sessLbl) return;
        // 의미 있는 제목(첫 사용자 메시지)이 있을 때만 표시.
        // 세션 UUID 단축형만 나오는 경우는 정보 가치가 없으므로 비워서 숨긴다(CSS :empty).
        const cur = sessions.find(s => s.session_id === AppState.sessionId);
        const t = cur ? _truncate(cur.last_user_message, 28) : '';
        $sessLbl.textContent = t ? `· ${t}` : '';
    }

    async function _deleteSession(sid) {
        if (!confirm('이 대화를 삭제하시겠습니까?')) return;
        try {
            const res = await fetch(`/api/sessions/${encodeURIComponent(sid)}`, { method: 'DELETE' });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            if (sid === AppState.sessionId) {
                _newSession();
            }
            await loadSessions();
        } catch (e) {
            console.warn('[sidebar] delete failed', e);
        }
    }

    /* ── 세션 전환 ── */
    async function switchSession(sid) {
        if (!sid || sid === AppState.sessionId) return;
        AppState.sessionId = sid;

        // 메시지 영역 초기화
        const $msgs = document.getElementById('messages');
        if ($msgs) {
            $msgs.innerHTML = '';
        }

        // ChatClient WS 재연결 (새 session_id) — 재연결 전 transient 상태 리셋
        try {
            if (window.chatClient) {
                if (typeof window.chatClient._resetTransient === 'function') {
                    window.chatClient._resetTransient();
                }
                window.chatClient.sessionId = sid;
                if (window.chatClient.ws) {
                    try { window.chatClient.ws.close(); } catch (_) {}
                }
                window.chatClient.connect();
            }
        } catch (e) { console.warn('[sidebar] ws reconnect failed', e); }

        // 메시지 로드 + 렌더 + 마지막 메시지의 meta.mode 로 모드 복원
        let restoredMode = null;
        try {
            const res = await fetch(`/api/sessions/${encodeURIComponent(sid)}/messages`);
            if (res.ok) {
                const data = await res.json();
                const msgs = data.messages || [];
                _renderHistoricalMessages(msgs);
                // 가장 최근 메시지의 meta_json.mode 를 우선 사용 (assistant > user 순)
                for (let i = msgs.length - 1; i >= 0; i--) {
                    const m = msgs[i];
                    let meta = null;
                    try {
                        meta = typeof m.meta_json === 'string'
                            ? JSON.parse(m.meta_json)
                            : (m.meta_json || null);
                    } catch (_) { meta = null; }
                    const mode = meta && meta.mode;
                    if (mode === 'graphrag' || mode === 'afmm') {
                        restoredMode = mode;
                        break;
                    }
                }
            }
        } catch (e) {
            console.warn('[sidebar] load messages failed', e);
        }

        // 모드 적용 (메시지에서 추론 못하면 'afmm' 기본값)
        if (window.chatClient && typeof window.chatClient._applyMode === 'function') {
            window.chatClient._applyMode(restoredMode || 'afmm');
        }

        // 가장 최근 completed job 결과 카드 복원
        await _restoreLastCompletedJobCard(sid);

        Bus.dispatchEvent(new CustomEvent('session:switched', { detail: { sessionId: sid } }));
        await loadSessions();
    }

    /**
     * 세션의 jobs 목록에서 결과 데이터가 있는 가장 최근 job 을 찾아
     * 결과 카드 bubble 을 messages 컨테이너 끝에 추가한다.
     *
     * 복원 대상 상태:
     *   - completed: 정상 완료 (모든 단계 성공)
     *   - partial:   일부 단계 실패하지만 ranking / smina 결과 존재 가능
     *
     * 미복원 (안내 메시지만):
     *   - abandoned: 서버 재시작으로 좀비 마킹
     *   - failed / cancelled: 데이터 없음
     *   - queued / running: 아직 진행 중
     */
    async function _restoreLastCompletedJobCard(sid) {
        if (typeof mountResultCard !== 'function') return;
        const $msgs = document.getElementById('messages');
        if (!$msgs) return;

        let jobs;
        try {
            const res = await fetch(`/api/sessions/${encodeURIComponent(sid)}/jobs`);
            if (!res.ok) return;
            const data = await res.json();
            jobs = data.jobs || data;
        } catch (e) {
            console.warn('[sidebar] jobs load failed', e);
            return;
        }

        if (!Array.isArray(jobs) || jobs.length === 0) return;

        // 시간순 (ended_at 우선, 없으면 started_at) 정렬 — 모든 잡
        const sortedAll = [...jobs].sort((a, b) => {
            const ta = (a.ended_at || a.started_at) ? new Date(a.ended_at || a.started_at).getTime() : 0;
            const tb = (b.ended_at || b.started_at) ? new Date(b.ended_at || b.started_at).getTime() : 0;
            return tb - ta;
        });

        // 결과 데이터가 있을 가능성이 있는 상태만 복원
        const VIEWABLE = new Set(['completed', 'partial']);
        const viewable = sortedAll.filter(j => VIEWABLE.has(j.status));

        if (viewable.length > 0) {
            const job = viewable[0];
            AppState.currentJobId = job.job_id;
            const rt = mountResultCard($msgs, job.job_id);
            if (rt && typeof window.setActiveResultTable === 'function') {
                window.setActiveResultTable(rt);
            }
            // partial 인 경우 안내
            if (job.status === 'partial' && job.error) {
                _appendStatusNote(
                    $msgs,
                    `⚠️ 이 잡은 *부분 완료(partial)* 상태입니다. 일부 단계가 실패했으나 ` +
                    `랭킹 데이터는 아래 결과 테이블에서 확인 가능합니다.\n사유: ${job.error}`,
                );
            }
            return;
        }

        // 결과 데이터 없는 상태들만 있음 — 가장 최근 잡 1개의 상태 알림
        const latest = sortedAll[0];
        if (!latest) return;
        const stateMessages = {
            abandoned: '⏸ 이 세션의 마지막 잡은 *서버 재시작으로 중단(abandoned)* 되었습니다. ' +
                       '결과 데이터가 없습니다. 동일 쿼리를 새로 제출하면 캐시된 MSA 가 재사용됩니다.',
            failed:    '❌ 이 세션의 마지막 잡이 *실패(failed)* 했습니다. 결과 데이터가 없습니다.',
            cancelled: '🚫 이 세션의 마지막 잡이 *취소(cancelled)* 되었습니다.',
            queued:    '⏳ 이 세션에 *대기 중(queued)* 인 잡이 있습니다. 진행이 시작되지 않았다면 서버 재시작 가능성 확인.',
            running:   '🔄 이 세션에 *실행 중(running)* 인 잡이 있습니다. 진행 패널에서 단계를 확인하세요.',
        };
        const msg = stateMessages[latest.status]
            || `이 세션의 마지막 잡 상태: \`${latest.status}\``;
        _appendStatusNote($msgs, msg + (latest.error ? `\n사유: ${latest.error}` : ''));
    }

    /** messages 컨테이너에 안내용 assistant 버블 추가 (markdown 지원) */
    function _appendStatusNote($msgs, text) {
        if (!$msgs) return;
        const row = document.createElement('div');
        row.className = 'msg-row msg-row--assistant';
        const bub = document.createElement('div');
        bub.className = 'msg-bubble msg-bubble--assistant msg-bubble--note';
        const html = _renderMarkdownSafe(text);
        if (html !== null) {
            bub.innerHTML = html;
        } else {
            bub.textContent = text;
        }
        row.appendChild(bub);
        $msgs.appendChild(row);
    }

    /* ── markdown 렌더 헬퍼 (chat.js _renderMarkdown 동일 로직, 인라인) ── */
    function _renderMarkdownSafe(text) {
        if (!window.marked || !window.DOMPurify) return null;
        try {
            const html = window.marked.parse(text, { gfm: true, breaks: true });
            return window.DOMPurify.sanitize(html);
        } catch (_) { return null; }
    }

    function _renderHistoricalMessages(messages) {
        const $msgs = document.getElementById('messages');
        if (!$msgs) return;
        const wel = document.getElementById('welcome-screen');
        if (wel) wel.remove();

        if (!messages.length) {
            const empty = document.createElement('div');
            empty.className = 'welcome';
            empty.innerHTML = '<p class="welcome__subtitle">이 대화에 메시지가 없습니다</p>';
            $msgs.appendChild(empty);
            return;
        }

        for (const m of messages) {
            const row = document.createElement('div');
            row.className = 'msg-row ' + (m.role === 'user' ? 'msg-row--user' : 'msg-row--assistant');
            const bub = document.createElement('div');
            bub.className = 'msg-bubble ' + (m.role === 'user' ? 'msg-bubble--user' : 'msg-bubble--assistant');

            if (m.role !== 'user') {
                // assistant 메시지: marked + DOMPurify로 markdown 렌더링
                const html = _renderMarkdownSafe(m.content || '');
                if (html !== null) {
                    bub.innerHTML = html;
                } else {
                    // CDN 미로드 시 plain text 폴백
                    bub.textContent = m.content || '';
                }
            } else {
                // user 메시지: XSS 방지를 위해 반드시 textContent 사용
                bub.textContent = m.content || '';
            }

            row.appendChild(bub);
            $msgs.appendChild(row);
        }
        $msgs.scrollTop = $msgs.scrollHeight;
    }

    /* ── 새 대화 ── */
    function _newSession() {
        // 새 session_id 발급 → ChatClient에 반영 후 WS 재연결
        const newId = (() => {
            const a = new Uint8Array(8);
            (window.crypto || { getRandomValues: (b) => { for (let i=0;i<8;i++) b[i]=Math.floor(Math.random()*256); return b; }}).getRandomValues(a);
            return Array.from(a, b => b.toString(16).padStart(2,'0')).join('');
        })();
        AppState.sessionId = newId;

        const $msgs = document.getElementById('messages');
        if ($msgs) {
            // 모드별 welcome 패널을 복원 — index.html 원본과 동일한 마크업.
            // 이렇게 해야 _applyMode() 의 [data-welcome-mode] 분기가 다시 동작한다.
            $msgs.innerHTML = `
                <div class="welcome" id="welcome-screen">
                    <div class="welcome__panel welcome__panel--afmm" data-welcome-mode="afmm">
                        <h1 class="welcome__title">AF3 + Smina 가상 스크리닝 챗봇</h1>
                        <p class="welcome__subtitle">단백질 FASTA와 SMILES 화합물을 같이 붙여넣고 "도킹해서 순위 매겨줘" 라고 입력하면 자동으로 스크리닝이 시작됩니다.</p>
                    </div>
                    <div class="welcome__panel welcome__panel--graphrag" data-welcome-mode="graphrag" hidden>
                        <h1 class="welcome__title">🧬 GraphRAG — KIST NPI 지식그래프</h1>
                        <p class="welcome__subtitle">NPASS 3.0 + Open Targets 25.12 에 자연어로 질의합니다.</p>
                    </div>
                </div>`;
        }

        if (window.chatClient) {
            // WS 재연결 전 transient 상태 리셋 (Fix 2)
            if (typeof window.chatClient._resetTransient === 'function') {
                window.chatClient._resetTransient();
            }
            window.chatClient.sessionId = newId;
            if (window.chatClient.ws) { try { window.chatClient.ws.close(); } catch(_){} }
            window.chatClient.connect();
            // 새 대화는 AFMM 기본으로 강제 — 사용자가 GraphRAG 모드에서 + 새 대화를
            // 눌렀을 때 모드가 그대로 남아 다음 입력이 graphrag 라우팅되는 문제 방지.
            // (이 결정 자체는 사용자가 토글로 다시 GraphRAG 로 바꾸면 무효.)
            if (typeof window.chatClient._applyMode === 'function') {
                window.chatClient._applyMode('afmm');
            }
        }
        Bus.dispatchEvent(new CustomEvent('session:new', { detail: { sessionId: newId } }));
        loadSessions();
    }

    /* ── 토글 ── */
    function toggleSidebar() {
        if (!$sb) return;
        $sb.classList.toggle('sidebar--collapsed');
    }

    /* ── 초기화 ── */
    document.addEventListener('DOMContentLoaded', () => {
        $newBtn?.addEventListener('click', _newSession);
        $toggle?.addEventListener('click', toggleSidebar);

        // 첫 로드
        loadSessions();

        // user 메시지 전송 후 사이드바 갱신
        Bus.addEventListener('chat:user_sent', () => {
            setTimeout(() => loadSessions(), 200);
        });

        // 주기적 폴링 (저비용)
        pollTimer = setInterval(() => {
            loadSessions();
        }, POLL_INTERVAL_MS);
    });

    // 외부 노출
    window.Sidebar = { loadSessions, switchSession, newSession: _newSession };
})();

/**
 * afmm_chat — ResultTable (Phase 2 + inline card 지원)
 * 변경 이력:
 *   - containerId → containerEl (DOM element 직접 주입) — 카드별 ID 충돌 방지
 *   - 헤더 단축 + small 단위 표기
 *   - ligand_id 컬럼 title tooltip + text-overflow 처리
 *   - CSV / TSV 다운로드 메서드 추가
 *   - mountResultCard(): 결과 버블을 messages 컨테이너에 attach하는 헬퍼
 */
'use strict';

class ResultTable {
    /**
     * @param {HTMLElement|string} containerEl  DOM element 또는 element ID 문자열
     * @param {string} jobId
     */
    constructor(containerEl, jobId) {
        // 문자열 ID 또는 element 모두 수용 (하위 호환)
        if (typeof containerEl === 'string') {
            this._containerEl = document.getElementById(containerEl);
        } else {
            this._containerEl = containerEl;
        }
        this.jobId = jobId;
        this.data = [];
        this.sortBy = 'smina';
        this.topN = 20;
        this._loading = false;
    }

    /* ── 데이터 로드 ── */
    async load() {
        if (!this.jobId) { this._showError('잡 ID가 없습니다.'); return; }
        this._loading = true;
        this._renderLoading();

        const url = `/api/screening/${this.jobId}/results?sort_by=${this.sortBy}&top_n=${this.topN}`;
        try {
            const resp = await fetch(url);
            if (!resp.ok) {
                this._showError(`HTTP ${resp.status}: 결과를 불러올 수 없습니다.`);
                this._loading = false;
                return;
            }
            const body = await resp.json();
            if (!body.ok) { this._showError(body.error || '알 수 없는 오류'); this._loading = false; return; }
            this.data = body.results || [];
        } catch (e) {
            this._showError('네트워크 오류: ' + e.message);
            this._loading = false;
            return;
        }
        this._loading = false;
        this._render();
    }

    /* ── 메인 렌더 ── */
    _render() {
        const container = this._containerEl;
        if (!container) return;

        if (this.data.length === 0) {
            container.textContent = '';
            const empty = document.createElement('p');
            empty.className = 'result-table__empty';
            empty.appendChild(document.createTextNode('결과가 없습니다. 스크리닝을 시작하세요.'));
            container.appendChild(empty);
            return;
        }

        container.textContent = '';

        const wrapper = document.createElement('div');
        wrapper.className = 'result-table__wrapper';

        const table = document.createElement('table');
        table.className = 'result-table';
        table.setAttribute('role', 'grid');
        table.setAttribute('aria-label', '스크리닝 결과 테이블');

        table.appendChild(this._buildHead());
        table.appendChild(this._buildBody());
        wrapper.appendChild(table);
        container.appendChild(wrapper);
    }

    /* ── 헤더 — 단축 레이블 + small 단위 ── */
    _buildHead() {
        const thead = document.createElement('thead');
        const tr = document.createElement('tr');

        // label: 헤더 주 텍스트 / unit: small 태그로 표시할 단위 (null이면 생략)
        const cols = [
            { key: null,        label: '#',         unit: null,          class: 'col-rank' },
            { key: null,        label: 'Ligand',    unit: null,          class: 'col-ligand' },
            { key: 'iptm',      label: 'ipTM',      unit: '0~1 ↑',       class: 'col-num' },
            { key: null,        label: 'PAE',       unit: 'Å ↓',         class: 'col-num' },
            { key: 'smina',     label: 'Smina',     unit: 'kcal/mol ↓',  class: 'col-num' },
            { key: 'composite', label: 'Score',     unit: 'composite ↑', class: 'col-num' },
        ];

        cols.forEach(col => {
            const th = document.createElement('th');
            th.scope = 'col';
            th.className = col.class || '';
            if (col.key) {
                th.classList.add('result-table__sortable');
                th.setAttribute('aria-sort', col.key === this.sortBy ? 'descending' : 'none');
                th.style.cursor = 'pointer';
                th.addEventListener('click', () => { if (col.key) this.setSortBy(col.key); });
            }

            th.appendChild(document.createTextNode(col.label));

            if (col.unit) {
                const sm = document.createElement('small');
                sm.textContent = col.unit;
                th.appendChild(sm);
            }

            if (col.key && col.key === this.sortBy) {
                const arrow = document.createElement('span');
                arrow.className = 'sort-indicator';
                arrow.setAttribute('aria-hidden', 'true');
                th.appendChild(arrow);
            }
            tr.appendChild(th);
        });

        thead.appendChild(tr);
        return thead;
    }

    /* ── 바디 — ligand_id에 title tooltip ── */
    _buildBody() {
        const tbody = document.createElement('tbody');

        this.data.forEach((row, idx) => {
            const tr = document.createElement('tr');
            tr.className = 'result-table__row';
            tr.setAttribute('tabindex', '0');
            tr.setAttribute('aria-label', `${idx + 1}위: 리간드 ${row.ligand_id ?? ''}`);

            const onClick = () => Bus.dispatchEvent(new CustomEvent('result:row_clicked', { detail: row }));
            tr.addEventListener('click', onClick);
            tr.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); }
            });

            const ligandId = String(row.ligand_id ?? '-');
            const ligandName = (row.ligand_name && String(row.ligand_name).trim()) || null;
            // 표시 우선순위: name > id. tooltip 은 항상 양쪽 모두 보여줌.
            const ligandDisplay = ligandName || ligandId;
            const ligandTooltip = ligandName
                ? `${ligandName}\nID: ${ligandId}`
                : ligandId;

            const numericCells = [
                { value: String(row.rank ?? idx + 1), cls: 'col-rank', title: null },
                // Ligand 셀은 별도로 처리 (name + small ID)
                { value: this._fmt(row.iptm, 3),             cls: 'col-num', title: null },
                { value: this._fmt(row.pae_mean, 2),         cls: 'col-num', title: null },
                { value: this._fmt(row.smina_affinity_kcal_mol, 2), cls: 'col-num', title: null },
                { value: this._fmt(row.composite_score, 3),  cls: 'col-num', title: null },
            ];

            // #
            const tdRank = document.createElement('td');
            tdRank.className = numericCells[0].cls;
            tdRank.appendChild(document.createTextNode(numericCells[0].value));
            tr.appendChild(tdRank);

            // Ligand — name 메인 + id 작은 글씨
            const tdLig = document.createElement('td');
            tdLig.className = 'col-ligand';
            tdLig.title = ligandTooltip;
            const nameSpan = document.createElement('span');
            nameSpan.className = 'col-ligand__name';
            nameSpan.textContent = ligandDisplay;
            tdLig.appendChild(nameSpan);
            if (ligandName) {
                // ID 도 작은 글씨로 함께 표시 (name 이 있는 경우만 부가 정보)
                const idSmall = document.createElement('small');
                idSmall.className = 'col-ligand__id';
                idSmall.textContent = ` ${ligandId.slice(0, 8)}`;
                tdLig.appendChild(idSmall);
            }
            tr.appendChild(tdLig);

            // 나머지 numeric 컬럼들
            for (let i = 1; i < numericCells.length; i++) {
                const cell = numericCells[i];
                const td = document.createElement('td');
                td.className = cell.cls || '';
                td.appendChild(document.createTextNode(cell.value));
                if (cell.title) td.title = cell.title;
                tr.appendChild(td);
            }

            tbody.appendChild(tr);
        });

        return tbody;
    }

    /* ── 숫자 포매팅 ── */
    _fmt(val, decimals) {
        if (val === null || val === undefined || val === '') return '-';
        const n = Number(val);
        if (isNaN(n)) return '-';
        return n.toFixed(decimals);
    }

    /* ── 로딩 상태 ── */
    _renderLoading() {
        const container = this._containerEl;
        if (!container) return;
        container.textContent = '';
        const p = document.createElement('p');
        p.className = 'result-table__loading';
        p.appendChild(document.createTextNode('로딩 중...'));
        container.appendChild(p);
    }

    /* ── 오류 상태 ── */
    _showError(err) {
        const container = this._containerEl;
        if (!container) return;
        container.textContent = '';
        const p = document.createElement('p');
        p.className = 'result-table__error';
        p.setAttribute('role', 'alert');
        p.appendChild(document.createTextNode('오류: ' + err));
        container.appendChild(p);
    }

    /* ── 정렬 변경 ── */
    setSortBy(mode) {
        this.sortBy = mode;
        this.load();
    }

    /* ── top-N 변경 ── */
    setTopN(n) {
        const parsed = parseInt(n, 10);
        if (!isNaN(parsed) && parsed > 0) {
            this.topN = Math.min(parsed, 100);
            this.load();
        }
    }

    /* ── 잡 ID 갱신 후 로드 ── */
    setJob(jobId) {
        this.jobId = jobId;
        this.load();
    }

    /* ── CSV / TSV 다운로드 ── */
    /**
     * @param {'csv'|'tsv'} format
     */
    downloadAs(format) {
        if (!this.data || this.data.length === 0) {
            alert('다운로드할 데이터가 없습니다.');
            return;
        }
        const sep = format === 'tsv' ? '\t' : ',';
        const ext = format === 'tsv' ? 'tsv' : 'csv';
        const mime = format === 'tsv' ? 'text/tab-separated-values' : 'text/csv';

        const headers = [
            'rank', 'ligand_name', 'ligand_id', 'iptm', 'pae_mean',
            'smina_affinity_kcal_mol', 'composite_score',
        ];

        const escapeCell = (val) => {
            if (format === 'tsv') {
                // TSV: 탭/줄바꿈만 공백으로 치환
                return String(val ?? '').replace(/[\t\r\n]/g, ' ');
            }
            // CSV: 콤마·큰따옴표·줄바꿈 포함 시 따옴표로 감싸기
            const s = String(val ?? '');
            if (/[",\r\n]/.test(s)) {
                return '"' + s.replace(/"/g, '""') + '"';
            }
            return s;
        };

        const rows = [
            headers.join(sep),
            ...this.data.map((r, i) => [
                r.rank ?? i + 1,
                r.ligand_name ?? '',
                r.ligand_id ?? '',
                r.iptm ?? '',
                r.pae_mean ?? '',
                r.smina_affinity_kcal_mol ?? '',
                r.composite_score ?? '',
            ].map(escapeCell).join(sep)),
        ];

        const blob = new Blob([rows.join('\r\n')], { type: mime + ';charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        const jobPart = (this.jobId || 'job').slice(0, 8);
        a.href = url;
        a.download = `screening_${jobPart}_${this.sortBy}_top${this.topN}.${ext}`;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(url); }, 500);
    }
}

/* ═══════════════════════════════════════════════════════════════════
 * mountResultCard(msgsContainer, jobId, [opts])
 * screening 완료 시 또는 세션 복원 시 결과 카드 bubble을 msgsContainer에
 * assistant bubble로 추가한다. ResultTable 인스턴스를 반환.
 *
 * opts.sortBy  string  초기 정렬 기준 (기본: 'composite')
 * opts.topN    number  초기 top-N (기본: 20)
 * ═══════════════════════════════════════════════════════════════════ */
function mountResultCard(msgsContainer, jobId, opts) {
    if (!msgsContainer || !jobId) return null;
    opts = opts || {};

    /* ── localStorage snapshot 로드 (처음 mount 시점의 sortBy/topN 보존) ── */
    let snap = null;
    try {
        const raw = localStorage.getItem('afmm.card.snapshot.' + jobId);
        if (raw) snap = JSON.parse(raw);
    } catch (_) {}

    const effSort = (snap && snap.sortBy) || opts.sortBy || 'smina';
    const effTopN  = (snap && snap.topN)   || opts.topN   || 20;

    /* snapshot이 없으면 이번 mount 값을 즉시 저장 */
    if (!snap) {
        try {
            localStorage.setItem('afmm.card.snapshot.' + jobId, JSON.stringify({ sortBy: effSort, topN: effTopN }));
        } catch (_) {}
    }

    /* ── bubble 행 생성 ── */
    const row = document.createElement('div');
    row.className = 'msg-row msg-row--assistant';

    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble msg-bubble--assistant msg-bubble--result';
    bubble.dataset.jobId = jobId;

    /* ── 헤더 영역 ── */
    const header = document.createElement('div');
    header.className = 'result-card__header';

    const title = document.createElement('span');
    title.className = 'result-card__title';
    title.textContent = '스크리닝 결과';

    const controls = document.createElement('div');
    controls.className = 'result-card__controls';

    /* 정렬 dropdown */
    const sortLbl = document.createElement('label');
    sortLbl.className = 'result-card__label';
    sortLbl.textContent = '정렬';
    const sortSel = document.createElement('select');
    sortSel.className = 'result-card__select btn--refresh';
    sortSel.setAttribute('aria-label', '정렬 기준');
    [
        { value: 'smina',     text: 'Smina' },
        { value: 'composite', text: 'Composite' },
        { value: 'ranking_score', text: 'AF3 Score' },
        { value: 'iptm',      text: 'ipTM' },
    ].forEach(o => {
        const op = document.createElement('option');
        op.value = o.value;
        op.textContent = o.text;
        if (o.value === effSort) op.selected = true;
        sortSel.appendChild(op);
    });

    /* top-N input */
    const topNLbl = document.createElement('label');
    topNLbl.className = 'result-card__label';
    topNLbl.textContent = '상위';
    const topNInput = document.createElement('input');
    topNInput.type = 'number';
    topNInput.className = 'result-card__top-n';
    topNInput.min = '1'; topNInput.max = '100';
    topNInput.value = String(effTopN);
    topNInput.setAttribute('aria-label', '상위 N개');
    const topNUnit = document.createElement('span');
    topNUnit.className = 'result-card__label';
    topNUnit.textContent = '개';

    /* 새로고침 버튼 */
    const btnRefresh = document.createElement('button');
    btnRefresh.type = 'button';
    btnRefresh.className = 'btn btn--refresh';
    btnRefresh.textContent = '새로고침';
    btnRefresh.setAttribute('aria-label', '결과 새로고침');

    /* CSV / TSV 다운로드 버튼 */
    const btnCsv = document.createElement('button');
    btnCsv.type = 'button';
    btnCsv.className = 'btn btn--refresh';
    btnCsv.textContent = 'CSV';
    btnCsv.setAttribute('aria-label', 'CSV 저장');

    const btnTsv = document.createElement('button');
    btnTsv.type = 'button';
    btnTsv.className = 'btn btn--refresh';
    btnTsv.textContent = 'TSV';
    btnTsv.setAttribute('aria-label', 'TSV 저장');

    /* 차트 보기 버튼 — charts-panel 토글 (기존 show-charts-btn 역할 대체) */
    const btnCharts = document.createElement('button');
    btnCharts.type = 'button';
    btnCharts.className = 'btn btn--refresh';
    btnCharts.textContent = '차트';
    btnCharts.setAttribute('aria-label', '차트 패널 토글');
    btnCharts.addEventListener('click', () => {
        Bus.dispatchEvent(new CustomEvent('result:charts_toggle'));
    });

    /* Smina 진행 배지 (screening 도중 업데이트됨) */
    const sminaBadge = document.createElement('div');
    sminaBadge.className = 'result-card__smina-badge hidden';

    /* controls 조합 */
    [sortLbl, sortSel, topNLbl, topNInput, topNUnit,
     btnRefresh, btnCsv, btnTsv, btnCharts, sminaBadge].forEach(el => controls.appendChild(el));

    header.appendChild(title);
    header.appendChild(controls);

    /* ── 테이블 컨테이너 ── */
    const tableContainer = document.createElement('div');

    /* ── ResultTable 초기화 ── */
    const rt = new ResultTable(tableContainer, jobId);
    rt.sortBy = effSort;
    rt.topN = effTopN;

    /* 이벤트 바인딩 — 카드 내부 querySelector 사용 (정적 ID 미사용) */
    sortSel.addEventListener('change', (e) => rt.setSortBy(e.target.value));
    topNInput.addEventListener('change', (e) => rt.setTopN(e.target.value));
    btnRefresh.addEventListener('click', () => rt.load());
    btnCsv.addEventListener('click', () => rt.downloadAs('csv'));
    btnTsv.addEventListener('click', () => rt.downloadAs('tsv'));

    /* ── 조합 → DOM 삽입 ── */
    bubble.appendChild(header);
    bubble.appendChild(tableContainer);
    row.appendChild(bubble);
    msgsContainer.appendChild(row);

    /* 스크롤 내리기 */
    msgsContainer.scrollTop = msgsContainer.scrollHeight;

    /* Smina 진행 배지 업데이트용 참조를 버블에 저장 */
    bubble._sminaBadge = sminaBadge;

    /* 데이터 로드 시작 */
    rt.load();

    return rt;
}

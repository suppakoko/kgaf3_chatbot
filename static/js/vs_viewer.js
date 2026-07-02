/**
 * afmm_chat — VSViewer: Mol* 3D 복합체 뷰어 (Phase 3)
 * frontend_plan.md §4.6, §7  /  docs/02-design/05-frontend-design.md AD-F3, AD-F4 권위
 *
 * 설계 결정:
 *  - Mol* 1.x createPluginUI({ target, render, spec }) 단일-객체 시그니처 전용
 *    (0.x 2-인수 방식, isGhost 옵션 사용 금지)
 *  - 스크린샷: plugin.helpers.viewportScreenshot.getImageDataUri() 경유
 *    (canvas3d.toBlob 직접 접근 금지)
 *  - 리간드 선택: MolScript 1.x expression-builder 체인-테스트 패턴
 *  - CDN 로드 실패 시 /static/lib/molstar.js 로컬 폴백
 */
'use strict';

const MOLSTAR_CDN_JS  = 'https://cdn.jsdelivr.net/npm/molstar/build/viewer/molstar.js';
const MOLSTAR_CDN_CSS = 'https://cdn.jsdelivr.net/npm/molstar/build/viewer/molstar.css';
const MOLSTAR_LOCAL_JS  = '/static/lib/molstar.js';
const MOLSTAR_LOCAL_CSS = '/static/lib/molstar.css';

class VSViewer {
    /**
     * @param {string} containerId - 뷰어를 마운트할 요소의 id
     */
    constructor(containerId) {
        this.containerId = containerId;
        this._container  = null;
        this.plugin      = null;
        this._molstarReady = false;
        this._loadPromise  = null;
    }

    /* ── Mol* 스크립트 지연 로드 ────────────────────────────────────────────── */

    /**
     * Mol* JS + CSS를 동적으로 삽입한다. 이미 로드된 경우 즉시 resolve.
     * CDN 실패 시 로컬 폴백. (AD-F3, AD-F6)
     * @returns {Promise<void>}
     */
    _loadMolstarScript() {
        if (this._molstarReady) return Promise.resolve();
        if (this._loadPromise)  return this._loadPromise;

        this._loadPromise = new Promise((resolve, reject) => {
            /** @param {string} href */
            const injectCSS = (href) => {
                if (document.querySelector(`link[href="${href}"]`)) return;
                const link = document.createElement('link');
                link.rel  = 'stylesheet';
                link.href = href;
                document.head.appendChild(link);
            };

            /** @param {string} src @returns {Promise<void>} */
            const injectJS = (src) => new Promise((res, rej) => {
                if (document.querySelector(`script[src="${src}"]`)) { res(); return; }
                const s = document.createElement('script');
                s.src  = src;
                s.async = false;
                s.onload  = () => res();
                s.onerror = () => rej(new Error(`Mol* load failed: ${src}`));
                document.head.appendChild(s);
            });

            injectCSS(MOLSTAR_CDN_CSS);

            injectJS(MOLSTAR_CDN_JS)
                .then(() => { this._molstarReady = true; resolve(); })
                .catch(() => {
                    console.warn('[VSViewer] CDN 실패, 로컬 폴백 시도');
                    injectCSS(MOLSTAR_LOCAL_CSS);
                    injectJS(MOLSTAR_LOCAL_JS)
                        .then(() => { this._molstarReady = true; resolve(); })
                        .catch(reject);
                });
        });

        return this._loadPromise;
    }

    /* ── 초기화 ─────────────────────────────────────────────────────────────── */

    /**
     * Mol* 플러그인을 초기화한다.
     * Mol* 1.x+ createPluginUI({ target, render, spec }) 시그니처 사용 (AD-F4).
     * @returns {Promise<void>}
     */
    async init() {
        await this._loadMolstarScript();

        this._container = document.getElementById(this.containerId);
        if (!this._container) {
            throw new Error(`[VSViewer] 컨테이너를 찾을 수 없음: #${this.containerId}`);
        }

        // Mol* 1.x 전역 molstar 네임스페이스 접근
        const { createPluginUI }    = molstar.Viewer;
        const { renderReact18 }     = molstar.PluginUI;
        const { DefaultPluginUISpec } = molstar.PluginUISpec;

        const spec = DefaultPluginUISpec();
        // 1.x에서 isGhost 제거됨 — spec.layout.initial 에서 직접 지정
        spec.layout = {
            initial: {
                isExpanded: false,
                showControls: false,
                showSequence: true,
                showLeftPanel: false,
            },
        };

        this.plugin = await createPluginUI({
            target : this._container,
            render : renderReact18,
            spec,
        });
    }

    /* ── 복합체 로드 ─────────────────────────────────────────────────────────── */

    /**
     * CIF 파일을 로드하고 리간드를 하이라이트한다.
     * @param {string} jobId
     * @param {string} ligandId
     * @param {string} [chainId='L'] - 백엔드 result.detail_payload.ligand_chain_id
     * @returns {Promise<void>}
     */
    async loadComplex(jobId, ligandId, chainId = 'L') {
        if (!this.plugin) await this.init();

        const cifUrl = `/api/results/${encodeURIComponent(jobId)}/${encodeURIComponent(ligandId)}/cif`;

        // 이전 상태 초기화
        await this.plugin.clear();

        const data = await this.plugin.builders.data.download(
            { url: cifUrl, isBinary: false },
            { state: { isGhost: false } },  // state 옵션은 data download에는 유효
        );

        const trajectory = await this.plugin.builders.structure.parseTrajectory(data, 'mmcif');
        await this.plugin.builders.structure.hierarchy.applyPreset(trajectory, 'default');

        await this._applyLigandHighlight(chainId);
    }

    /* ── 리간드 하이라이트 ───────────────────────────────────────────────────── */

    /**
     * 지정 chain을 선택하고 포커스 + 하이라이트 적용.
     * Mol* 1.x MolScript 체인-테스트 패턴 사용 (AD-F4).
     * @param {string} chainId - 예: 'L'
     * @returns {Promise<void>}
     */
    async _applyLigandHighlight(chainId) {
        if (!this.plugin) return;

        const { StructureSelection }  = molstar.Mol.Structure.Query;
        const { MolScriptBuilder: MS } = molstar.Mol.Structure.Language;

        const expr = MS.struct.generator.atomGroups({
            'chain-test': MS.core.rel.eq([
                MS.struct.atomProperty.macromolecular.auth_asym_id(),
                chainId,
            ]),
        });

        const sel = StructureSelection.Singletons(
            this.plugin.state.data,
            expr,
        );

        // 카메라 포커스 + 하이라이트 (1.x plugin.managers 경로)
        this.plugin.managers.structure.focus.setFromSelection(sel);
    }

    /* ── 스크린샷 ───────────────────────────────────────────────────────────── */

    /**
     * 현재 뷰를 PNG로 저장한다.
     * plugin.helpers.viewportScreenshot 경유 필수 (canvas3d.toBlob 직접 접근 금지, AD-F4).
     * @returns {Promise<void>}
     */
    async screenshot() {
        if (!this.plugin) return;
        const helper  = this.plugin.helpers.viewportScreenshot;
        const dataUrl = await helper.getImageDataUri();

        const a = document.createElement('a');
        a.href     = dataUrl;
        a.download = 'structure.png';
        a.click();
    }

    /* ── 뷰어 해제 ───────────────────────────────────────────────────────────── */

    dispose() {
        try { this.plugin?.dispose(); } catch (_) { /* ignore */ }
        this.plugin = null;
    }
}

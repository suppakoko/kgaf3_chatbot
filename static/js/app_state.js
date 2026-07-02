/**
 * afmm_chat — 전역 상태 + 이벤트 버스
 * frontend_plan.md §6 기반
 */

'use strict';

/** 전역 싱글턴 상태 */
const AppState = {
    /** WebSocket 연결 여부 */
    wsConnected: false,
    /** 현재 세션 ID (chat.js가 초기화) */
    sessionId: null,
    /** 선택된 LLM 모델 */
    selectedModel: null,
    /** 현재 활성 스크리닝 잡 ID */
    activeJobId: null,
    /** 잡 목록: jobId → { id, status, totalLigands } */
    jobs: {},
    /** 업로드된 라이브러리 ID */
    uploadedLibraryId: null,
    /** 업로드된 단백질 ID */
    uploadedProteinId: null,
    /** 현재 결과 */
    currentJobId: null,
    currentMolId: null,
    /** UI 언어 */
    lang: 'ko',
    /** 채팅 모드: "afmm" (가상스크리닝) | "graphrag" (Neo4j 지식그래프) */
    mode: 'afmm',
};

/**
 * EventTarget 기반 이벤트 버스
 *
 * 이벤트 목록:
 *   ws:connected        — WebSocket 연결 성공
 *   ws:disconnected     — WebSocket 연결 끊김
 *   ws:reconnecting     — 재연결 시도 중 (detail: { attempt, max })
 *   model:changed       — 모델 선택 변경 (detail: { model })
 *   mode:changed        — 채팅 모드 변경 (detail: { mode: 'afmm' | 'graphrag' })
 *   library:uploaded    — 라이브러리 업로드 완료 (detail: { libraryId, fileName })
 *   screening:started   — 스크리닝 잡 시작 (detail: { jobId, totalLigands })
 *   screening:progress  — 진행 업데이트 (detail: screening.progress 메시지)
 *   screening:complete  — 스크리닝 완료 (detail: screening.complete 메시지)
 *   screening:error     — 스크리닝 오류 (detail: screening.error 메시지)
 *   lang:changed        — 언어 변경 (detail: { lang })
 */
const Bus = new EventTarget();

/* ── i18n 스텁 (Phase 1: 한국어 기본값) ── */
const I18N = {
    ko: {
        'status.connected':    '연결됨',
        'status.disconnected': '연결 끊김',
        'status.connecting':   '연결 중...',
        'status.reconnecting': '재연결 중...',
        'btn.send':            '전송',
        'btn.stop':            '정지',
        'btn.upload':          '라이브러리 업로드',
        'upload.success':      '업로드 완료',
        'upload.error':        '업로드 실패',
        'error.ws_failed':     'WebSocket 연결 실패',
        'error.send_failed':   '메시지 전송 실패',
    },
    en: {
        'status.connected':    'Connected',
        'status.disconnected': 'Disconnected',
        'status.connecting':   'Connecting...',
        'status.reconnecting': 'Reconnecting...',
        'btn.send':            'Send',
        'btn.stop':            'Stop',
        'btn.upload':          'Upload Library',
        'upload.success':      'Upload complete',
        'upload.error':        'Upload failed',
        'error.ws_failed':     'WebSocket connection failed',
        'error.send_failed':   'Failed to send message',
    },
};

/**
 * i18n 번역 함수 (Phase 1: 한국어 기본값 반환)
 * @param {string} key - I18N 키
 * @returns {string}
 */
function t(key) {
    const lang = AppState.lang || 'ko';
    return (I18N[lang] && I18N[lang][key]) ?? key;
}

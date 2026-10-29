# Opus 5.5 structured-v2 재평가

2026-10-08: 사용자 요청에 따라 structured-v1 하락 원인 수정 후 동일12시나리오×2회 재평가를 시작했다. 결과는 진행 중이며 성능 개선은 아직 미확정이다.

## 변경 계약

- max_prompt_tokens=0: 40,000토큰 조사 조기종료 제거. 60턴 상한 및 실제 API 컨텍스트 한도는 유지.
- mcp-observation-v2: 초기 관측·read_observation 페이지를 모두 전달한 그대로 rolling context에 보존. 오래됐다는 이유로 본문을 제거하지 않음.
- 개별필드700자/전체예산절반 제한 대신 전체6,000자 응답 예산에 들어가는 완전한 필드 전달. 넘치는 원문은 관측 저장소에서 재조회.
- 실제 API 컨텍스트 오류는 실행 오류로 드러내며, 근거 삭제 후 강제로 제출한 답을 정상 완료처럼 채점하지 않음.
- 기존 legacy/structured-v1 기본 동작은 버전으로 구분.

## 실행

- 실행기: student API, claude-opus-5-5, 기존18110전송 프록시
- 평가: 기존12캡처×2회, 동시2개; F09·F06먼저(시나리오집합동일)
- 원인정답: 기존결정적채점, 기전: GPT-6.1-sol / rubric v2, 완료분부터 캐시채점
- 계획/소스해시/스냅샷: outputs/opus55-structured-v2-opus12-20261008/
- 결과: outputs/mcp-eval/opus55-structured-v2-opus12-20261008/
- systemd: thinkfl-opus55-structured-v2-opus12.service, thinkfl-live-mechanism-opus55-structured-v2.service
- 누락된 실행만 최대3회 pass로 복구하며 기존완료score는 재실행하지 않음.

검증: 구현자 관련55테스트 통과, 루트가 관측/학생/감사41테스트 추가 확인, CLI structured-v2/0수용 및 계획해시사전검증 통과.

API의 공식 한도는 1M토큰이며 40,000은 하네스의 소프트상한이었다: https://platform.claude.com/docs/en/models/opus-5-5/overview

기존CLI22/24와 단일변수통제비교는 아니며, 새결과는 동일API실행기의 structured-v1 19/24(기전17/24)와 우선 비교한다.

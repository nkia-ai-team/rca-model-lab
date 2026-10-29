# 통합 학습 전환 — 2026-10-08

> 후속 상태: 이 실행은 관측 근거 감사에서 부적합 supervision이 확인되어 의도적으로
> 중단했다. 새 optimizer step은 확인하지 못했다. 아래 내용은 최초 전환 당시 기록이다.
> 현재 대체 레시피와 승인 절차는 [dataset-informed-recipe-20261008.md](dataset-informed-recipe-20261008.md)를 따른다.

사용자 요청에 따라 TT4 → OB4 → OpenRCA2 → Polestar 순차 학습 대신 네 데이터셋을 합친 단일 학습을 실행했다. 기존 TT4 trainer만 SIGTERM으로 종료(143)했고, 로그·설정·체크포인트 파일은 보존했다. 기존 순차 서비스는 중지하고 `thinkfl-joint-training.service`를 시작했다.

## 데이터와 설정

- 374개 사례, 664개 예제. 원본 네 파일은 변경하지 않았다.
- 데이터 SHA, 기존 writer/judge provenance, manifest readiness, source별 train membership, 통합 split partition 분리, source 간 case 충돌을 검증했다.
- 합본은 재채점하지 않았다. complete marker는 source provenance 집계이며 새로운 승인 판정을 뜻하지 않는다.
- 기반 Muse 모델에서 새로 시작하고 TT4 부분 학습 가중치는 사용하지 않는다.
- 사례별 loss 가중치, 예제당 최대 6개 eligible 턴, 기존 52,000토큰 제한을 사용한다. Polestar도 이번 실행에서는 6턴 제한을 적용한다. 모든 턴을 전부 학습한다는 뜻은 아니다.
- 설정상 최대 996 optimizer steps. 실제 학습 전처리 결과와 진행 로그를 별도로 확인한다. 통합 자체가 전체 계산량을 없애지는 않는다.
- 학습 후 fresh base와 joint adapter를 같은 validation 분할에서 비교한다. sealed 데이터는 사용하지 않는다. 새 기전 채점은 Codex GPT-6.1 Sol.

## 검증 및 현재 상태

2026-10-08 10:14 KST 원격 import·데이터 검증을 통과하고 통합 trainer를 실행했다. 실제 sampled-turn 전처리 로그를 확인했다. 첫 optimizer step은 이 전환 확인 시점에 아직 관측하지 않았다.

전체 테스트 274 passed / 1 skipped. 변경 코드 Ruff 통과. 기존 `dropped ... over max_length` 로그가 길이 초과와 샘플링 제외 수를 혼합하던 오류를 수정하고 회귀 테스트를 추가했다. 이전 TT4의 1,716건은 길이 초과만의 수가 아니라 두 제외 이유의 합계다.

## 실행 기록

- 설정: `configs/sft/recovered-joint-20261008.yaml`
- 준비 도구: `scripts/mcp_prepare_joint_recovered.py`
- 데이터 및 provenance: `outputs/joint-recovered-20261008/`
- 서비스 상태/로그: `outputs/joint-training-20261008/state.json`, `service.log`
- 전환 검증: `outputs/joint-training-20261008/switch-verification.json`

이번 실행은 기존 복구 데이터 통합 비교다. 새 관측 하네스용 학습이나 별도 evidence-admission 교정 완료 모델로 간주하지 않으며, 아직 RCA 성능 향상을 주장하지 않는다.

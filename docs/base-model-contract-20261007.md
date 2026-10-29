# 기반 모델 특성을 반영한 실험 계약

2026-10-07. 로컬 artifact 및 기존 실험 기록 기반. 공개 model card/사전학습 코퍼스/제작자의 전체 post-training recipe는 이번 확인 범위에서 검증하지 못했다. 이를 알고 있다고 가정하지 않는다.

## 무엇을 기반 모델이라고 부르는가

현재 프로젝트 기준선은 서빙 이름 `muse-glimmer-base`, 학습 원본 `/home/work/models/muse-glimmer-30b-fp8-block`인 Muse/Glimmer 30B 계열 checkpoint다. **base는 우리 RCA adapter를 붙이기 전이라는 뜻**이며 instruction/reasoning post-training 전의 raw pretrained 모델이라는 뜻이 아니다. 정확한 upstream 학습 이력은 별도 확인 필요하다.

[학습 설정](../configs/sft/muse-glimmer-30b-mcp-v1q.yaml)은 revision `8ed2e29141d4fef439b9a0e15e0a2678bc190a82`, model config/index SHA와 tokenizer SHA를 고정한다. 로컬 tokenizer 폴더의 config와 원격 FP8 config는 별도 artifact이므로 동일 hash라고 가정하지 않는다.

## 특성 → 실험 결정

| 확인된 특성 | 계획에 반영하는 결정 | 검증 상태/한계 |
|---|---|---|
| `MuseGlimmerForConditionalGeneration`, text+vision config | RCA 실험은 language_model adapter에 한정. 멀티모달 포장과 서빙용 language-only adapter의 이름 매핑 확인 | config 및 LoRA regex 확인; 실제 remote loading parity는 GPU 실행 전 별도 |
| text52층: sliding39/full13, window2048 | 긴 문맥 성능을 최대 길이만으로 가정하지 않음. 관측 거리/길이별 오류와 추가조회 빈도 측정 | 구조가 RCA 오류 원인이라는 증거는 없음. 순서/길이는 SFT 비교와 별도 ablation |
| config max_position_embeddings131072 | 모델의 설정 한도, 서버 한도, 학습 max_length, prompt+completion을 각각 기록 | 현재 recipe max_length52000. 현재 /v1/models 조회에서49,152 확인; 실험 시작 시 재확인 |
| `to=self`와 ATEM tool call / 전용 grammar | 동일 tokenizer/chat template/도구schema/grammar로 SFT와 rollout 비교. reasoning loss0은 reasoning output 제거가 아님 | 기존 grammar/Harmony 혼입·stop 충돌 실패 이력 계승 |
| FP8 block source→BF16 compute, adapter/optimizer FP32 | fake activation quant 비활성화, input-gradient-flow probe를 사전조건으로 유지 | checkpoint.py에 guard확인. 로컬 torch미설치로 실제 GPU 검증 대체 불가 |
| rank16 alpha32, attention q/k/v/o+MLP gate/up/down LoRA | P1에서 이 설정을 고정하고 reasoning loss만 변경. 용량/targetmodules는 후속별도변수 | self_attn.gate_proj는 MLP gate_proj와 다르며 현재 target regex에서 제외 |
| vocab_size202048, logit softcap20, output multiplier | 선택적 CE/softcap와 서빙 logit 처리가 맞는지 유지·검증 | 과거 문서의 '26만어휘' 수치를 현재 artifact값으로 반복하지 않음 |
| 교사의 짧고 성공한 trajectory가 학생에겐 추론 비약일 수 있음 | teacher final정답 외 상태별 행동 재현과 자유rollout을 별도 평가. student 실패상태 교정 후보 유지 | 기존 문서의 가설, 입증된 용량 한계가 아님 |

## 실험 전 체크

1. fresh base와 adapter의 원본 revision·tokenizer·chat template·runtime limits·sampling·grammar·harness SHA가 동일한가.
2. 실제 server tokenize와 train prefix가 동일한가. loss개선만으로 parity를 추정하지 않는다.
3. 입력→base Linear gradient 전달과 adapter update/load가 실제로 확인되는가.
4. heldout tool/action 성능을 먼저 보고, 다음 전체 RCA rollout으로 확인한다. teacher forcing 성공을 자유rollout으로 승격하지 않는다.
5. 근거 위치/길이별 실패는 별도로 보고, attention구조만으로 원인을 단정하지 않는다.

근거: [localconfig](../models/muse-glimmer-30b-tokenizer/config.json), [checkpoint계약](../src/rca_lab/train/checkpoint.py), [Muse grammar](../src/rca_lab/mcp/muse_grammar.py), [MCP실험기록](mcp-student-pipeline.md), [과거 실패](history-lessons-20261007.md).

## 현재 학생 설정의 구체 값

`StudentConfig` 기본값은 prompt40,000tokens/completion4,096tokens, 개별 toolresult6,000chars다. temperature1.0/top_p0.95/top_k64이며, chat template는 reasoning_strength미지정 시 high를 사용한다. 현재 /v1/models 조회에서도 서버49,152tokens를 확인했다. [조회 기록](../outputs/evidence-audit-20261007/base-runtime.json). 학습 max_length52,000과도 별도로 기록한다. 실제 실행 config가 기본값을 덮어쓸 수 있으므로 결과별 config를 함께 보존한다.

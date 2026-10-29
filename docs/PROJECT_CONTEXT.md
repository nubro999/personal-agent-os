# Project Context — 결정사항 인수인계

작성 기준: 2026-10-29. 개인 업무 자동화를 위한 프로젝트이며 회사 전체 시스템 교체가 아니다.

## 목표
사용자가 자연어로 업무를 등록하면 AI가 프로젝트·우선순위·작업단계를 추론하고, 정말 중요한 정보가 불명확할 때만 질문한다. 장기적으로는 회사 맥락 보강, 2325 자동 개선, 커뮤니케이션 지원을 추가한다.

## 범위 및 우선순위
1. **Task Agent MVP:** 자체 Task DB, 자연어 CRUD, 관련 항목 검색, 모호성 해소, 완료/마감 관리.
2. **Context Agent:** Overmind 지식 조회, 누락·충돌 및 최신성 질문 생성. 매일 아침 중요한 질문 최대 3~5개.
3. **Personal Work Assistant:** Slack 미응답 요청, 답변 초안, 개인 말투·관계별 맥락, 업무 브리핑. 초안 우선, 자동 전송 금지.
4. **2325 Improvement Agent:** Failure → Context Retrieval → 원인 분석 → 변경 제안 → Eval/회귀 검사 → 승인 후 적용.

## 명시적 제외
- 기존 Overmind/Calendar/Notion의 Task 목록 사용하지 않음.
- 처음부터 회사 전반 업무 자동화하지 않음.
- 모든 사내 정보를 별도 DB로 복제하지 않음.
- 처음부터 Temporal, 다수의 독립 마이크로서비스, Knowledge Graph를 도입하지 않음.

## 구현 환경
사용자 WSL 프로젝트 경로: `/home/nubroo/projects/personal-agent-os`. 이 채팅 세션의 실행 환경과 WSL은 별개이므로 GitHub로 문맥을 전달한다.

## 배경 요구
- 2325 업무에는 응대 로그, Failure, 프롬프트 변경과 평가 결과가 이미 연결되어 있음. 개선 제안부터 적용까지의 승인형 자동화 루프는 미완성.
- 회사 Overmind는 지식 수집/조회 시스템이며, 개인 Task DB와 분리한다.
- 사용자 개인 대화, 급여, 민감한 업무 원문, 비밀정보는 이 저장소에 포함하지 않는다.

## 다음 단계
[구현 계획](IMPLEMENTATION_PLAN.md)을 따라 Task Agent 수직 슬라이스를 완성한다. UI와 API 연동 후 Overmind Adapter를 실제 인증 방식에 맞춰 구현한다.

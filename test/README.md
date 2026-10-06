# 시험 표준 / Testing standard (wise-vibe wds-test)

```
test/
├── README.md
├── dev-env/          # 표준 환경 검증 1회 / one-time environment check
│   ├── scenario.md
│   ├── result.md
│   └── logs/         # 원본 출력 (비커밋 / not committed)
└── impl/<Nth>/       # 구현 차수별 (1st, 2nd, …) — 덮어쓰기 금지 / never overwritten
    ├── scenario.md
    ├── result.md
    └── logs/
```

사이클 / cycle: 시나리오 작성 → 실행 → 실패 시 원인 수정·재시험(같은 차수에 회차 누적) → `result.md`.
언어별 유닛 테스트도 `test/` 하위에 둔다(`tests/` 생성 금지). 예외: RN `__tests__/`, iOS `AppTests/`, Android `app/src/test`, Go/Rust in-package.

"""label-session — 세션 아카이브 정답 라벨 CLI (research §6·확률화 Phase 1 선행 조건).

실험 직후 실제 취출 품목/수량을 아카이브 YAML에 구조화해 기입한다 — 지금까지
GitHub 이슈 코멘트에 수기로 적던 "take out: zone 2, class 27 ×5"의 대체.
conformal 보정과 무게 확률화 shadow diff의 정오 판정이 이 라벨을 읽는다.

사용 예 (Jetson, 실험 직후):

    label-session --latest --zone 2 --take 27x5 --note "1.6s 간격 연속 취출"
    label-session ses-10-1784698526 --take 2:27x1 --take 3:30x1

--take 형식: `[존:]<class_id|이름>x<개수>` — 식별자가 숫자면 class_id, 아니면
이름(진단용 자유 문자열). 존 접두사가 없으면 --zone 값을 쓴다. 재실행 시
기존 라벨은 대체된다 (오기입 정정 = 다시 실행).
"""
from __future__ import annotations

import argparse
import datetime
import sys

from crk_model.ledger.archive import SessionArchive


def _parse_take(raw: str, default_zone: int | None) -> dict:
    body = raw
    zone = default_zone
    if ":" in raw:
        zone_part, body = raw.split(":", 1)
        zone = int(zone_part)
    if "x" not in body:
        raise ValueError(f"--take 형식 오류 (…x<개수> 필요): {raw!r}")
    ident, count_part = body.rsplit("x", 1)
    count = int(count_part)
    if count < 1:
        raise ValueError(f"--take 개수는 1 이상: {raw!r}")
    if zone is None:
        raise ValueError(
            f"--take {raw!r}에 존이 없습니다 — `존:...` 접두사 또는 --zone 사용"
        )
    item: dict = {"zone": zone, "count": count}
    if ident.isdigit():
        item["class_id"] = int(ident)
    else:
        item["name"] = ident
    return item


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="label-session", description="세션 아카이브에 정답 라벨 기입"
    )
    parser.add_argument("session_id", nargs="?", help="세션 id (예: ses-10-1784698526)")
    parser.add_argument(
        "--latest", action="store_true", help="가장 최근 세션 파일에 기입 (실험 직후용)"
    )
    parser.add_argument(
        "--dir", default="data/sessions", help="아카이브 루트 (기본: data/sessions)"
    )
    parser.add_argument("--zone", type=int, default=None, help="--take의 기본 존")
    parser.add_argument(
        "--take",
        action="append",
        default=[],
        metavar="[존:]<class_id|이름>x<개수>",
        help="실제 취출 항목 (반복 지정)",
    )
    parser.add_argument("--note", default="", help="실험 메모 (선택)")
    args = parser.parse_args(argv)

    if not args.take:
        parser.error("--take를 1개 이상 지정해야 합니다")
    if bool(args.session_id) == args.latest:
        parser.error("session_id 또는 --latest 중 정확히 하나를 지정해야 합니다")

    try:
        items = [_parse_take(raw, args.zone) for raw in args.take]
    except ValueError as exc:
        parser.error(str(exc))

    archive = SessionArchive(args.dir)
    if args.latest:
        path = archive.latest()
        if path is None:
            print(f"아카이브가 비어 있습니다: {args.dir}", file=sys.stderr)
            return 1
        session_id = path.stem
    else:
        session_id = args.session_id

    ground_truth = {
        "labeled_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "note": args.note,
        "items": items,
    }
    try:
        path = archive.annotate_ground_truth(session_id, ground_truth)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"라벨 기입 완료: {path}")
    for it in items:
        ident = it.get("class_id", it.get("name"))
        print(f"  zone {it['zone']}: {ident} x{it['count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

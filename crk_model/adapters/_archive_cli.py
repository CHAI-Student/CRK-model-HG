"""세션 아카이브 CLI 공용 헬퍼 — label/render/analyze 세 CLI의 세션 지정·조회 흐름.

조회 실패 시 여기서는 사유만 stderr로 출력하고 None을 돌려준다 — 종료 코드
(`return 1`)는 호출측 CLI가 정한다. CLI마다 다른 "찾을 수 없습니다" 문구는
호출측이 인자로 넘긴다 (CLI별 출력 문구 유지).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from crk_model.ledger.archive import SessionArchive


def add_dir_argument(parser: argparse.ArgumentParser) -> None:
    """--dir (아카이브 루트) — 세 CLI 공통."""
    parser.add_argument(
        "--dir", default="data/sessions", help="아카이브 루트 (기본: data/sessions)"
    )


def add_session_arguments(
    parser: argparse.ArgumentParser, *, session_help: str, latest_help: str
) -> None:
    """session_id 위치 인자 + --latest + --dir (label/render 공통, 이 순서 그대로)."""
    parser.add_argument("session_id", nargs="?", help=session_help)
    parser.add_argument("--latest", action="store_true", help=latest_help)
    add_dir_argument(parser)


def require_one_session(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """session_id와 --latest 중 정확히 하나 — 위반 시 parser.error (종료 코드 2)."""
    if bool(args.session_id) == args.latest:
        parser.error("session_id 또는 --latest 중 정확히 하나를 지정해야 합니다")


def latest_session(archive: SessionArchive, archive_dir: str) -> Path | None:
    """가장 최근 세션 파일 — 비어 있으면 stderr 안내 후 None."""
    path = archive.latest()
    if path is None:
        print(f"아카이브가 비어 있습니다: {archive_dir}", file=sys.stderr)
    return path


def find_session(archive: SessionArchive, session_id: str, not_found: str) -> Path | None:
    """세션 id로 파일 탐색 — 없으면 not_found 문구를 stderr로 출력하고 None."""
    path = archive.find(session_id)
    if path is None:
        print(not_found, file=sys.stderr)
    return path

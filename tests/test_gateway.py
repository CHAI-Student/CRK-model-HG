"""gateway: 상태기계 + 인과 배리어 확정(I17), 타임아웃 fail-closed(D9), I10·I11."""
import pytest

from crk_model.core.profiles import REFRIGERATOR
from crk_model.core.types import JudgmentResult, JudgmentStatus, ProductCount
from crk_model.gateway import DoorState, MultiZoneGateway, build_payment_payload
from crk_model.ledger import CloseSettler, EventLog, TriggerEvent


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def removal(sid, zone, ts, product, count=1):
    j = JudgmentResult(JudgmentStatus.COMPLETE, (ProductCount(product, count),), 0.9, "strict")
    return TriggerEvent(sid, zone, ts, -product.unit_weight * count, (), j)


def make_gateway(clock=None):
    clock = clock or FakeClock()
    gw = MultiZoneGateway(
        CloseSettler(), EventLog(), {1: REFRIGERATOR}, clock=clock,
        close_timeout_s=10.0, close_grace_s=0.0,  # 유예는 전용 테스트에서만
    )
    return gw, clock


class TestBarrierDrivenClose:
    def test_close_finalizes_immediately_when_barrier_satisfied(self, cola):
        # I17: 큐가 비어 있으면 debounce 대기 없이 즉시 확정 (지연 단축 부수 효과)
        gw, _ = make_gateway()
        gw.handle_open("s1")
        gw.notify_enqueued(1)
        gw.record_trigger(removal("s1", 1, 1.0, cola))
        gw.notify_processed(1)
        resp = gw.handle_close()
        assert resp.state is DoorState.FINALIZED
        payload = build_payment_payload(resp.payload)
        assert payload["totalPrice"] == 1500
        assert payload["productCount"] == 1

    def test_pending_queue_blocks_finalize(self, cola):
        # I17: 큐 미정합 동안 시간이 아무리 지나도(타임아웃 전) 확정 금지
        gw, _ = make_gateway()
        gw.handle_open("s1")
        gw.notify_enqueued(1)  # 아직 미처리
        resp = gw.handle_close()
        assert resp.state is DoorState.PENDING_CLOSE
        assert "queue_pending" in resp.detail
        # late trigger가 처리됨 → 배리어 충족 → 확정
        gw.record_trigger(removal("s1", 1, 2.0, cola))
        gw.notify_processed(1)
        resp = gw.poll()
        assert resp.state is DoorState.FINALIZED
        assert resp.payload.total_price == 1500  # late trigger 유실 없음

    def test_timeout_with_unsatisfied_barrier_is_error_not_finalize(self):
        # I17 + D9: 상한 타임아웃 만료 = 에러 세션 (fail-closed), 부분 확정 금지
        gw, clock = make_gateway()
        gw.handle_open("s1")
        gw.notify_enqueued(1)  # 영원히 미처리 (카메라/워커 장애 시뮬레이션)
        gw.handle_close()
        clock.t = 11.0
        # queue_pending은 유실이 아니라 진행 중일 수 있음 (Jetson 추론 > close_timeout)
        # → close_timeout에서는 에러 금지, stall 상한까지 대기
        assert gw.poll().state is DoorState.PENDING_CLOSE
        clock.t = 121.0  # worker_stall_timeout(120s) 초과 = 진짜 워커 사망
        resp = gw.poll()
        assert resp.state is DoorState.ERROR
        assert resp.payload is None  # 결제로 아무것도 안 나감
        assert "barrier_timeout" in resp.detail

    def test_slow_inflight_trigger_survives_close_timeout(self, cola):
        # 이슈 #3: CLOSE 후 10s 내 추론 미완 → 예전엔 barrier_timeout ERROR.
        # 처리 완료가 늦게 와도 정상 확정되어야 한다.
        gw, clock = make_gateway()
        gw.handle_open("s1")
        gw.notify_enqueued(1)
        gw.handle_close()
        clock.t = 30.0  # close_timeout(10s) 훌쩍 지남 — 워커는 아직 추론 중
        assert gw.poll().state is DoorState.PENDING_CLOSE
        gw.record_trigger(removal("s1", 1, 2.0, cola))
        gw.notify_processed(1)  # 추론 완료
        resp = gw.poll()
        assert resp.state is DoorState.FINALIZED
        assert resp.payload.total_price == 1500  # late 결과 유실 없음

    def test_seq_watermark_gates_close(self, cola):
        # D2/I17 ③: close 이전 seq 전원 도착까지 확정 보류
        gw, _ = make_gateway()
        gw.handle_open("s1")
        resp = gw.handle_close(seq_watermark={1: 2})
        assert resp.state is DoorState.PENDING_CLOSE
        gw.barrier.note_seq(1, 2)
        assert gw.poll().state is DoorState.FINALIZED


class TestPaymentContract:
    def test_interim_rejected_by_payment_builder(self, cola):
        # I10: ACTIVE 중 잠정치는 타입으로 결제 차단
        gw, _ = make_gateway()
        gw.handle_open("s1")
        gw.record_trigger(removal("s1", 1, 1.0, cola))
        resp = gw.poll()
        assert resp.state is DoorState.ACTIVE
        with pytest.raises(TypeError):
            build_payment_payload(resp.payload)

    def test_settlement_idempotent_at_settler_layer(self, cola):
        # I11: 이중 과금 불가는 wire 반복 전달이 아니라 settler의 세션 키 멱등
        # 캐시가 보장한다 — 확정 후 같은 세션을 다시 정산해도 동일 객체.
        gw, _ = make_gateway()
        gw.handle_open("s1")
        gw.notify_enqueued(1)
        gw.record_trigger(removal("s1", 1, 1.0, cola))
        gw.notify_processed(1)
        first = gw.handle_close()
        assert first.state is DoorState.FINALIZED
        assert first.payload is gw._settle()  # 캐시 재생 = 동일 정산 객체

    def test_finalize_delivers_once_then_returns_to_idle(self, cola):
        # 에지의 device busy 해제 계약: 확정 결과는 정확히 1회 전달되고 게이트웨이는
        # 즉시 idle로 복귀한다 (원본 finalize_global_session이 확정 직후 세션을
        # 비우는 것과 동형). 이후 CLOSE 재폴링은 IDLE — complete를 반복 응답하면
        # 에지가 busy 상태를 영원히 유지하는 것을 실기에서 확인함 (issue #5 계열 3차).
        gw, clock = make_gateway()
        gw.handle_open("s1")
        gw.notify_enqueued(1)
        gw.record_trigger(removal("s1", 1, 1.0, cola))
        gw.notify_processed(1)
        first = gw.handle_close()
        assert first.state is DoorState.FINALIZED  # 결제 페이로드는 이 응답에 실림
        assert gw.state is DoorState.IDLE  # 전달 직후 즉시 복귀

        clock.t = 600.0  # 문이 닫혀있는 채로 CLOSE만 반복 폴링돼도
        resp = gw.poll()
        assert resp.state is DoorState.IDLE  # 더 이상 확정 결과를 반복하지 않음
        assert resp.payload is None

        # 새 OPEN이 오면 정상적으로 새 세션 시작 (기존 동작 유지)
        gw.handle_open("s2")
        assert gw.state is DoorState.ACTIVE

    def test_new_session_resets_barrier(self, cola):
        gw, clock = make_gateway()
        gw.handle_open("s1")
        gw.notify_enqueued(1)  # s1에서 미해소
        gw.handle_close()
        clock.t = 121.0  # queue_pending은 stall 상한(120s)에서만 에러
        assert gw.poll().state is DoorState.ERROR
        # 새 세션 OPEN → 새 배리어 (이전 세션 잔재가 다음 세션을 막지 않음)
        gw.handle_open("s2")
        assert gw.handle_close().state is DoorState.FINALIZED


class TestCloseGrace:
    """issue #8: 배리어는 '도착한' 트리거만 센다 — 문 닫힘 시점에 카메라가 아직
    쓰고 있는 AVI(실측: CLOSE 0.66s 후 /trigger 도착)는 배리어에 보이지 않아
    0원 확정 + late trigger rejected(매출 누락)가 났다. seq 워터마크(D2) 배포
    전까지 CLOSE 유예 창(원본 close_initial_wait 3.0s)이 유일한 방어."""

    def make_grace_gateway(self, grace=3.0):
        clock = FakeClock()
        gw = MultiZoneGateway(
            CloseSettler(), EventLog(), {1: REFRIGERATOR}, clock=clock,
            close_timeout_s=10.0, close_grace_s=grace,
        )
        return gw, clock

    def test_close_with_no_triggers_waits_grace_before_finalize(self, cola):
        # 실기 재현: 트리거 0건 상태의 CLOSE — 유예 동안 확정 보류
        gw, clock = self.make_grace_gateway()
        gw.handle_open("s1")
        resp = gw.handle_close()
        assert resp.state is DoorState.PENDING_CLOSE
        assert resp.detail == "close_grace_pending"

        # 유예 내(0.66s 뒤) late trigger 도착 → 정상 수용
        clock.t = 0.66
        gw.notify_enqueued(1)
        gw.record_trigger(removal("s1", 1, 0.7, cola))
        gw.notify_processed(1)
        # 트리거 도착 시점부터 유예 리셋 — 아직 확정 금지
        clock.t = 2.0
        assert gw.poll().state is DoorState.PENDING_CLOSE
        # 마지막 트리거 도착 + 유예 경과 → 확정, late trigger 매출 포함
        clock.t = 0.66 + 3.0
        resp = gw.poll()
        assert resp.state is DoorState.FINALIZED
        assert resp.payload.total_price == 1500  # rejected 매출 누락 없음

    def test_grace_elapsed_without_late_trigger_finalizes_empty(self):
        gw, clock = self.make_grace_gateway()
        gw.handle_open("s1")
        assert gw.handle_close().detail == "close_grace_pending"
        clock.t = 3.0  # 유예 경과, late trigger 없음 → 0상품 정상 확정
        resp = gw.poll()
        assert resp.state is DoorState.FINALIZED
        assert resp.payload.product_count == 0

    def test_seq_watermark_skips_grace(self, cola):
        # D2 워터마크가 있으면 인과 신호가 완결 — 시간 유예 불필요, 즉시 확정
        gw, _ = self.make_grace_gateway()
        gw.handle_open("s1")
        gw.notify_enqueued(1)
        gw.record_trigger(removal("s1", 1, 0.5, cola))
        gw.notify_processed(1)
        gw.barrier.note_seq(1, 2)
        resp = gw.handle_close(seq_watermark={1: 2})
        assert resp.state is DoorState.FINALIZED  # 유예 없이 즉시

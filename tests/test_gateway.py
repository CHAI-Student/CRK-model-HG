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
        CloseSettler(), EventLog(), {1: REFRIGERATOR}, clock=clock, close_timeout_s=10.0
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

    def test_repoll_after_finalize_is_idempotent(self, cola):
        # I11: 재폴링해도 동일 정산 객체 (이중 과금 불가)
        gw, _ = make_gateway()
        gw.handle_open("s1")
        gw.notify_enqueued(1)
        gw.record_trigger(removal("s1", 1, 1.0, cola))
        gw.notify_processed(1)
        first = gw.handle_close()
        second = gw.poll()
        assert first.payload is second.payload

    def test_repoll_after_finalize_stays_finalized_without_new_open(self, cola):
        # CLOSE는 level-triggered — 문이 닫혀있는 동안 계속 재폴링된다. 시간이 아무리
        # 지나도 새 OPEN 없이는 FINALIZED가 임의로 초기화되면 안 된다(issue #5 후속
        # 회귀: 타임아웃 자동 리셋이 결제 확정 정보를 status=processing으로 덮어썼었음).
        gw, clock = make_gateway()
        gw.handle_open("s1")
        gw.notify_enqueued(1)
        gw.record_trigger(removal("s1", 1, 1.0, cola))
        gw.notify_processed(1)
        first = gw.handle_close()
        assert first.state is DoorState.FINALIZED

        clock.t = 600.0  # 문이 한참동안 닫혀있는 채로 CLOSE만 반복 폴링됨
        resp = gw.poll()
        assert resp.state is DoorState.FINALIZED
        assert resp.payload is first.payload

        # 새 OPEN이 오면 그때 정상적으로 새 세션 시작 (기존 동작 유지)
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

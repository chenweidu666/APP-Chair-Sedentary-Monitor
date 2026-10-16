"""占用检测与久坐计时的单元测试（用合成数据，无需硬件）。

运行：python3 -m unittest discover -s tests -v
"""

import unittest

from chairmon.model import STATE_EMPTY, STATE_OCCUPIED, STATE_UNKNOWN, config_for
from chairmon.occupancy import OccupancyDetector
from chairmon.sedentary import Reminder, SedentaryConfig, SedentaryTracker
from chairmon.synth import SyntheticChair, replay


def detect(scenario, mount="backrest", **cfg_overrides):
    det = OccupancyDetector(config_for(mount, **cfg_overrides))
    gen = SyntheticChair(fs=10.0)
    changes = replay(gen.stream(scenario), det)
    return det, changes


class TestOccupancy(unittest.TestCase):
    def test_learning_phase_is_unknown(self):
        det, changes = detect([("empty", 15)])
        self.assertEqual(det.state, STATE_UNKNOWN)  # 20 s 自学习期间不下结论

    def test_empty_chair_stays_empty(self):
        det, changes = detect([("empty", 90)])
        self.assertEqual(det.state, STATE_EMPTY)
        self.assertNotIn(STATE_OCCUPIED, [s for _t, s in changes])

    def test_sit_down_is_detected_quickly(self):
        det, changes = detect([("empty", 25), ("sit_impact", 1.2), ("leaning", 30)])
        self.assertEqual(det.state, STATE_OCCUPIED)
        sit_time = next(t for t, s in changes if s == STATE_OCCUPIED)
        self.assertLess(sit_time - 25.0, 8.0, f"落座后应在 8 s 内检出，实际 {changes}")

    def test_stand_up_returns_to_empty(self):
        det, changes = detect([("empty", 25), ("leaning", 90), ("empty", 60)])
        self.assertEqual(det.state, STATE_EMPTY)
        seq = [s for _t, s in changes]
        self.assertEqual(seq[-1], STATE_EMPTY)
        self.assertIn(STATE_OCCUPIED, seq)

    def test_short_noise_burst_while_empty_is_not_false_positive(self):
        # 空椅上有人走过/碰了一下桌子：有运动但没有倾角与呼吸
        det, changes = detect([("empty", 25), ("bump", 2.0), ("empty", 50)])
        self.assertEqual(det.state, STATE_EMPTY)

    def test_upright_without_backrest_detected_by_breathing(self):
        # 人坐直、几乎不靠椅背：倾角只有 ~1°，靠呼吸判据检出
        det, changes = detect([("empty", 25), ("upright", 60)])
        self.assertEqual(det.state, STATE_OCCUPIED, f"呼吸判据未生效：{changes}")
        self.assertTrue(det.features.breath_ok)
        self.assertAlmostEqual(det.features.breath_hz, 0.32, delta=0.06)

    def test_object_on_chair_is_documented_limitation(self):
        # 放重物会压出倾角 → 被判"有人"；这是纯 IMU 方案的已知局限（需压力传感融合）
        det, _changes = detect([("empty", 25), ("object", 60)])
        self.assertEqual(det.state, STATE_OCCUPIED)

    def test_stale_data_goes_unknown_and_back(self):
        det = OccupancyDetector(config_for("backrest"))
        gen = SyntheticChair(fs=10.0)
        for s in gen.stream([("empty", 25), ("leaning", 30)]):
            det.update(s)
        self.assertEqual(det.state, STATE_OCCUPIED)
        det.heartbeat(gen.t + 10.0)          # BLE 掉线
        self.assertEqual(det.state, STATE_UNKNOWN)
        for s in gen.samples("leaning", 10):
            det.update(s)
        self.assertEqual(det.state, STATE_OCCUPIED)

    def test_seat_mount_preset_also_works(self):
        det, _changes = detect([("empty", 25), ("sit_impact", 1.0), ("leaning", 40)], mount="seat")
        self.assertEqual(det.state, STATE_OCCUPIED)

    def test_features_are_populated(self):
        det, _changes = detect([("empty", 25), ("leaning", 40)])
        f = det.features
        self.assertGreater(f.tilt_deg, 3.0)
        self.assertGreater(f.motion_rms, 0.004)
        self.assertGreater(f.confidence, 0.5)
        self.assertIn("breath_amp", f.to_dict())


class TestSedentary(unittest.TestCase):
    def _run(self, tracker, states, start=1000.0, step=5.0):
        """按 (state, minutes) 列表推进时间，返回所有提醒。"""
        reminders = []
        now = start
        for state, minutes in states:
            for _ in range(int(minutes * 60 / step)):
                now += step
                r = tracker.update(state, now)
                if r:
                    reminders.append(r)
        return reminders, now

    def test_first_reminder_at_limit(self):
        t = SedentaryTracker(SedentaryConfig(sitting_limit_min=45, repeat_min=15))
        reminders, _ = self._run(t, [(STATE_OCCUPIED, 50)])
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].kind, "first")
        self.assertAlmostEqual(reminders[0].sitting_minutes, 45, delta=0.2)

    def test_repeat_reminder(self):
        t = SedentaryTracker(SedentaryConfig(sitting_limit_min=45, repeat_min=15))
        reminders, _ = self._run(t, [(STATE_OCCUPIED, 80)])
        self.assertEqual([r.kind for r in reminders], ["first", "repeat", "repeat"])

    def test_short_break_does_not_reset(self):
        t = SedentaryTracker(SedentaryConfig(sitting_limit_min=45, standing_reset_min=2))
        self._run(t, [(STATE_OCCUPIED, 40), (STATE_EMPTY, 0.7), (STATE_OCCUPIED, 6)])
        self.assertGreater(t.sitting_minutes, 45)   # 40 + 6，40 秒起身不清零
        self.assertEqual(t.stats.breaks, 0)

    def test_long_break_resets(self):
        t = SedentaryTracker(SedentaryConfig(sitting_limit_min=45, standing_reset_min=2))
        self._run(t, [(STATE_OCCUPIED, 40), (STATE_EMPTY, 3), (STATE_OCCUPIED, 5)])
        self.assertAlmostEqual(t.sitting_minutes, 5, delta=0.2)
        self.assertEqual(t.stats.breaks, 1)

    def test_unknown_pauses_without_reset(self):
        t = SedentaryTracker(SedentaryConfig(sitting_limit_min=45))
        self._run(t, [(STATE_OCCUPIED, 10), (STATE_UNKNOWN, 10), (STATE_OCCUPIED, 10)])
        self.assertAlmostEqual(t.sitting_minutes, 20, delta=0.3)  # UNKNOWN 10 分钟不计入
        self.assertEqual(t.stats.breaks, 0)

    def test_counts_and_stats(self):
        t = SedentaryTracker(SedentaryConfig(sitting_limit_min=45))
        self._run(t, [(STATE_OCCUPIED, 30), (STATE_EMPTY, 3), (STATE_OCCUPIED, 20), (STATE_EMPTY, 3)])
        self.assertEqual(t.stats.breaks, 2)
        self.assertGreater(t.stats.today_sitting_minutes, 45)
        self.assertAlmostEqual(t.stats.longest_minutes, 30, delta=0.5)

    def test_next_reminder_countdown(self):
        t = SedentaryTracker(SedentaryConfig(sitting_limit_min=45, repeat_min=15))
        self._run(t, [(STATE_OCCUPIED, 50)])
        self.assertIsNotNone(t.next_reminder_in())


if __name__ == "__main__":
    unittest.main()

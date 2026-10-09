"""Pure schedule maths: validation, next-run calculation, timezones and daylight saving. No database, no network."""

import unittest
from datetime import datetime, timedelta, timezone

from app.scheduler import recurrence as r
from app.scheduler.timeutil import iso, loads, to_dt

UTC = timezone.utc
NOW = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)


def spec(kind, **kw):
    kw.setdefault("tz_name", "Asia/Dhaka")
    return r.build_spec(kind=kind, now=NOW, **kw)


class OnceTests(unittest.TestCase):
    def test_local_time_is_converted_to_utc(self):
        s = spec("once", once_at="2027-01-16T09:00")
        self.assertEqual(s.run_at, datetime(2027, 1, 16, 3, 0, tzinfo=UTC))  # Dhaka is UTC+6
        self.assertEqual(s.timezone, "Asia/Dhaka")

    def test_runs_exactly_once(self):
        s = spec("once", once_at="2027-01-16T09:00")
        self.assertEqual(r.next_after(s, NOW), s.run_at)
        self.assertIsNone(r.next_after(s, s.run_at))  # strictly after: the same instant is never returned twice
        self.assertEqual(r.upcoming(s, NOW, 5), [s.run_at])

    def test_past_and_far_future_are_refused(self):
        with self.assertRaises(r.ScheduleError) as ctx:
            spec("once", once_at="2027-01-14T09:00")
        self.assertIn("past", ctx.exception.errors[0])
        with self.assertRaises(r.ScheduleError):
            spec("once", once_at="2031-01-16T09:00")
        with self.assertRaises(r.ScheduleError):
            spec("once", once_at="")
        with self.assertRaises(r.ScheduleError):
            spec("once", once_at="2027-02-30T09:00")  # no such date
        with self.assertRaises(r.ScheduleError):
            spec("once", once_at="not a date")


class DailyWeeklyTests(unittest.TestCase):
    def test_daily_repeats_every_day_in_the_local_zone(self):
        s = spec("daily", times="21:30, 09:00, 09:00")
        self.assertEqual(s.times, ("09:00", "21:30"))  # sorted, unique
        runs = r.upcoming(s, NOW, 4)
        self.assertEqual(runs, [datetime(2027, 1, 15, 15, 30, tzinfo=UTC), datetime(2027, 1, 16, 3, 0, tzinfo=UTC),
                                datetime(2027, 1, 16, 15, 30, tzinfo=UTC), datetime(2027, 1, 17, 3, 0, tzinfo=UTC)])

    def test_weekly_uses_the_selected_weekdays_only(self):
        s = spec("weekly", times="10:00", weekdays=["0", "4"])  # Monday and Friday
        for run in r.upcoming(s, NOW, 6):
            self.assertIn(run.astimezone(r.zone("Asia/Dhaka")).weekday(), (0, 4))
        # 2027-01-15 is a Friday, but 10:00 Dhaka (04:00 UTC) has already passed at NOW (14:00 Dhaka): next is Monday
        self.assertEqual(r.next_after(s, NOW), datetime(2027, 1, 18, 4, 0, tzinfo=UTC))
        self.assertEqual(r.next_after(s, datetime(2027, 1, 15, 3, 0, tzinfo=UTC)), datetime(2027, 1, 15, 4, 0, tzinfo=UTC))

    def test_weekly_next_week_when_today_has_passed(self):
        s = spec("weekly", times="10:00", weekdays=["4"])
        after_today = datetime(2027, 1, 15, 5, 0, tzinfo=UTC)
        self.assertEqual(r.next_after(s, after_today), datetime(2027, 1, 22, 4, 0, tzinfo=UTC))

    def test_invalid_times_and_weekdays(self):
        for bad in ("", "25:00", "9:00", "09:60", "noon", "09:00, 7pm"):
            with self.assertRaises(r.ScheduleError, msg=bad):
                spec("daily", times=bad)
        for bad in ([], ["7"], ["-1"], ["x"]):
            with self.assertRaises(r.ScheduleError, msg=str(bad)):
                spec("weekly", times="09:00", weekdays=bad)
        with self.assertRaises(r.ScheduleError):
            spec("daily", times=", ".join(f"{h:02d}:{m:02d}" for h in range(13) for m in (0, 30)))  # 26 > 24 times


class CustomTests(unittest.TestCase):
    def test_interval_from_start_in_elapsed_time(self):
        s = spec("custom", every="2", unit="hours", starts="2027-01-16T08:00")
        self.assertEqual(s.interval_minutes, 120)
        runs = r.upcoming(s, NOW, 3)
        self.assertEqual(runs[0], datetime(2027, 1, 16, 2, 0, tzinfo=UTC))
        self.assertEqual(runs[1] - runs[0], timedelta(hours=2))
        self.assertEqual(r.next_after(s, runs[0] + timedelta(minutes=1)), runs[1])

    def test_end_date_stops_the_series(self):
        s = spec("custom", every="1", unit="days", starts="2027-01-16T08:00", ends="2027-01-18T08:00")
        self.assertEqual(len(r.upcoming(s, NOW, 10)), 3)
        self.assertIsNone(r.next_after(s, datetime(2027, 1, 18, 2, 0, tzinfo=UTC)))

    def test_limits_and_garbage(self):
        for every, unit in (("5", "minutes"), ("0", "hours"), ("-3", "hours"), ("x", "hours"), ("2", "weeks"), ("999", "days")):
            with self.assertRaises(r.ScheduleError, msg=f"{every} {unit}"):
                spec("custom", every=every, unit=unit, starts="2027-01-16T08:00")
        with self.assertRaises(r.ScheduleError):
            spec("custom", every="1", unit="hours", starts="2027-01-14T08:00")  # start in the past
        with self.assertRaises(r.ScheduleError):
            spec("custom", every="1", unit="hours", starts="2027-01-16T08:00", ends="2027-01-16T07:00")

    def test_expressions_are_not_accepted(self):
        for evil in ("*/5 * * * *", "__import__('os')", "1; DROP TABLE schedules"):
            with self.assertRaises(r.ScheduleError):
                spec("custom", every=evil, unit="minutes", starts="2027-01-16T08:00")
        with self.assertRaises(r.ScheduleError):
            spec("cron", times="09:00")


class TimezoneTests(unittest.TestCase):
    def test_unknown_or_malicious_timezones_are_refused(self):
        for bad in ("", "Mars/Base", "../../etc/passwd", "Asia/Dhaka/../UTC", "C:\\Windows", "A" * 80, "EST5EDT; DROP"):
            with self.assertRaises(r.ScheduleError, msg=bad):
                spec("daily", times="09:00", tz_name=bad)

    def test_same_wall_clock_time_differs_in_utc_per_zone(self):
        dhaka = spec("daily", times="09:00", tz_name="Asia/Dhaka")
        berlin = spec("daily", times="09:00", tz_name="Europe/Berlin")
        self.assertNotEqual(r.next_after(dhaka, NOW), r.next_after(berlin, NOW))

    def test_dhaka_has_no_dst_shift(self):
        s = spec("daily", times="09:00")
        runs = r.upcoming(s, datetime(2027, 3, 1, tzinfo=UTC), 40)
        self.assertEqual({x.hour for x in runs}, {3})

    def test_spring_forward_gap_runs_after_the_gap_once(self):
        # New York 2027-03-14: 02:00 -> 03:00. 02:30 does not exist and runs at 03:30 local (07:30 UTC).
        s = r.build_spec(kind="daily", tz_name="America/New_York", now=datetime(2027, 3, 1, tzinfo=UTC), times="02:30")
        runs = r.upcoming(s, datetime(2027, 3, 13, 12, 0, tzinfo=UTC), 3)
        self.assertEqual(runs[0], datetime(2027, 3, 14, 7, 30, tzinfo=UTC))
        self.assertEqual(runs[1], datetime(2027, 3, 15, 6, 30, tzinfo=UTC))   # EDT from now on
        self.assertEqual(len({x.date() for x in runs}), 3)                    # one run per day, none doubled

    def test_fall_back_ambiguous_time_runs_once_at_first_occurrence(self):
        # New York 2027-11-07: 01:30 happens twice (EDT then EST). Only the first one runs.
        s = r.build_spec(kind="daily", tz_name="America/New_York", now=datetime(2027, 10, 1, tzinfo=UTC), times="01:30")
        runs = r.upcoming(s, datetime(2027, 11, 6, 12, 0, tzinfo=UTC), 2)
        self.assertEqual(runs[0], datetime(2027, 11, 7, 5, 30, tzinfo=UTC))   # 01:30 EDT
        self.assertEqual(runs[1], datetime(2027, 11, 8, 6, 30, tzinfo=UTC))   # 01:30 EST next day

    def test_next_after_is_strictly_increasing(self):
        s = spec("daily", times="09:00, 12:00")
        cursor, last = NOW, NOW
        for _ in range(30):
            cursor = r.next_after(s, cursor)
            self.assertGreater(cursor, last)
            last = cursor


class HelperTests(unittest.TestCase):
    def test_to_dt_accepts_datetime_and_sqlite_text(self):
        self.assertEqual(to_dt("2027-01-15 08:00:00.000000"), NOW)
        self.assertEqual(to_dt("2027-01-15T08:00:00Z"), NOW)
        self.assertEqual(to_dt(datetime(2027, 1, 15, 14, 0, tzinfo=timezone(timedelta(hours=6)))), NOW)
        self.assertIsNone(to_dt(None))
        self.assertEqual(iso(NOW), "2027-01-15T08:00:00Z")
        self.assertEqual(loads('["09:00"]', []), ["09:00"])
        self.assertEqual(loads(None, []), [])
        self.assertEqual(loads("{broken", []), [])

    def test_describe_is_plain_language(self):
        self.assertIn("Every day at 09:00", r.describe(spec("daily", times="09:00")))
        self.assertIn("Monday", r.describe(spec("weekly", times="09:00", weekdays=["0"])))
        self.assertIn("Every 2 hour", r.describe(spec("custom", every="2", unit="hours", starts="2027-01-16T08:00")))


if __name__ == "__main__":
    unittest.main()

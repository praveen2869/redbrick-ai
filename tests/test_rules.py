"""
Unit tests for deterministic Business Rules Engine
"""

import unittest
from datetime import datetime
from src.models import Tenant, RentVineRecord, RentVineNote, DecisionOutcome
from src.engine.rules import BusinessRulesEngine
from src.database import db

class TestBusinessRules(unittest.TestCase):
    def setUp(self):
        self.rules = BusinessRulesEngine(grace_period_days=3, dedup_window_hours=24)

    def test_paid_status_zero_balance(self):
        record = RentVineRecord(tenant_id="T-999", balance=0.0, monthly_rent=1500.0, due_date="2026-10-01")
        is_paid, reason = self.rules.check_paid_status(record)
        self.assertTrue(is_paid)
        self.assertIn("$0.00", reason)

    def test_unpaid_status(self):
        record = RentVineRecord(tenant_id="T-999", balance=1500.0, monthly_rent=1500.0, due_date="2026-10-01")
        is_paid, reason = self.rules.check_paid_status(record)
        self.assertFalse(is_paid)
        self.assertIn("1,500.00", reason)

    def test_exclusion_flag(self):
        t_active = Tenant(id="T-1", first_name="John", last_name="Doe", phone="555", email="a@b.com", property_name="P", unit_number="1")
        is_excl, _ = self.rules.check_exclusion_list(t_active)
        self.assertFalse(is_excl)

        t_excl = Tenant(id="T-2", first_name="Jane", last_name="Doe", phone="555", email="a@b.com", property_name="P", unit_number="2", is_excluded=True, exclusion_reason="Legal hold")
        is_excl, reason = self.rules.check_exclusion_list(t_excl)
        self.assertTrue(is_excl)
        self.assertEqual(reason, "Legal hold")

    def test_grace_period(self):
        # Rent due on 1st of November
        record_nov = RentVineRecord(tenant_id="T-NOV", balance=1500.0, monthly_rent=1500.0, due_date="2026-11-01")
        
        # 1. 1st–4th of current month: Within grace period (reminders suppressed)
        for day in (1, 2, 3, 4):
            check_date = datetime(2026, 11, day, 10, 0)
            in_grace, reason = self.rules.check_grace_period(record_nov, current_date=check_date)
            self.assertTrue(in_grace, f"Day {day} of November should be in grace period")
            self.assertIn("Within current-month grace period", reason)

        # 2. Starting on the 5th: Grace period expired (reminders can begin)
        day5 = datetime(2026, 11, 5, 9, 0)
        in_grace, reason = self.rules.check_grace_period(record_nov, current_date=day5)
        self.assertFalse(in_grace, "Day 5 of November should not be in grace period")
        self.assertIn("expired", reason.lower())

        day15 = datetime(2026, 11, 15, 12, 0)
        in_grace, reason = self.rules.check_grace_period(record_nov, current_date=day15)
        self.assertFalse(in_grace)

        # 3. Overdue rent from previous months: Handled under existing overdue rules
        # October rent (due 2026-10-01) checked on November 2nd (Day 2 of Nov)
        record_oct_overdue = RentVineRecord(tenant_id="T-OCT", balance=1200.0, monthly_rent=1200.0, due_date="2026-10-01")
        nov_day2 = datetime(2026, 11, 2, 14, 0)
        in_grace, reason = self.rules.check_grace_period(record_oct_overdue, current_date=nov_day2)
        self.assertFalse(in_grace, "Previous month overdue rent should NOT be suppressed by current month grace period")
        self.assertIn("previous month", reason.lower())

        # 4. Future rent (e.g. December rent checked in November)
        record_future = RentVineRecord(tenant_id="T-DEC", balance=1500.0, monthly_rent=1500.0, due_date="2026-12-01")
        in_grace, reason = self.rules.check_grace_period(record_future, current_date=nov_day2)
        self.assertTrue(in_grace, "Future rent dues should be suppressed")

    def test_existing_promise_in_notes(self):
        future_date = "2026-10-25"
        record = RentVineRecord(
            tenant_id="T-999", balance=1500.0, monthly_rent=1500.0, due_date="2026-10-01",
            notes=[RentVineNote(id="n1", created_at="2026-10-10", author="PM", text=f"[AI PROMISE ON FILE: Will pay by {future_date}] Tenant called")]
        )
        has_promise, pdate, _ = self.rules.check_existing_promise_in_notes(record, current_time=datetime(2026, 10, 15))
        self.assertTrue(has_promise)
        self.assertEqual(pdate, future_date)

if __name__ == "__main__":
    unittest.main()

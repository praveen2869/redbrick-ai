"""
Red Bricks Property Management - Seed Data Generator
Populates realistic properties, tenants, RentVine financials, SMS threads (Quo),
and email replies (Gmail) covering all real-world scenarios.
"""

from datetime import datetime, timedelta
from src.database import db
from src.models import (
    Tenant, RentVineRecord, RentVineNote, CommunicationMessage,
    Channel, MessageDirection, DecisionOutcome, WorkflowDecision
)

def populate_sample_data(reset: bool = False):
    """Inserts a comprehensive portfolio of tenants and communication logs."""
    if reset:
        db.reset_database()
    now = datetime(2026, 10, 15, 12, 0, 0)
    current_month_str = now.strftime("%Y-%m")

    # Sample tenants
    sample_tenants = [
        # Scenario 1: Unpaid & Eligible (Will trigger Quo SMS Reminder)
        Tenant(
            id="T-101",
            first_name="Marcus",
            last_name="Vance",
            phone="+1-415-555-0101",
            email="m.vance@example.com",
            property_name="Oakridge Luxury Apartments",
            unit_number="Unit 304"
        ),
        # Scenario 2: Rent Already Paid ($0 balance)
        Tenant(
            id="T-102",
            first_name="Elena",
            last_name="Rostova",
            phone="+1-415-555-0102",
            email="e.rostova@example.com",
            property_name="Oakridge Luxury Apartments",
            unit_number="Unit 210"
        ),
        # Scenario 3: Valid Payment Promise in SMS ("I'll pay this Friday")
        Tenant(
            id="T-103",
            first_name="Jordan",
            last_name="Hayes",
            phone="+1-415-555-0103",
            email="j.hayes@example.com",
            property_name="Sunset Highland Villas",
            unit_number="Townhouse 12"
        ),
        # Scenario 4: Valid Payment Promise already logged in RentVine Notes
        Tenant(
            id="T-104",
            first_name="Sophia",
            last_name="Chen",
            phone="+1-415-555-0104",
            email="s.chen@example.com",
            property_name="Highland Terrace",
            unit_number="Apt 4B"
        ),
        # Scenario 5: Dispute / Maintenance Withholding in SMS
        Tenant(
            id="T-105",
            first_name="Derrick",
            last_name="Miller",
            phone="+1-415-555-0105",
            email="d.miller@example.com",
            property_name="Sunset Highland Villas",
            unit_number="Villa 08"
        ),
        # Scenario 6: Tenant claims already paid in Email (Gmail)
        Tenant(
            id="T-106",
            first_name="Amina",
            last_name="Al-Mansoor",
            phone="+1-415-555-0106",
            email="amina.m@example.com",
            property_name="Oakridge Luxury Apartments",
            unit_number="Unit 512"
        ),
        # Scenario 7: Unpaid & Eligible (Quo SMS will be sent)
        Tenant(
            id="T-107",
            first_name="Carlos",
            last_name="Ramirez",
            phone="+1-415-555-0107",
            email="carlos.r@example.com",
            property_name="Highland Terrace",
            unit_number="Apt 2A"
        ),
        # Scenario 8: Excluded List (VIP / Attorney arrangement)
        Tenant(
            id="T-108",
            first_name="Victoria",
            last_name="Sterling",
            phone="+1-415-555-0108",
            email="v.sterling@example.com",
            property_name="Oakridge Luxury Apartments",
            unit_number="Penthouse 01",
            is_excluded=True,
            exclusion_reason="Special corporate lease handled directly by VP of Operations"
        ),
        # Scenario 9: Deduplication Test (Already reminded 4 hours ago)
        Tenant(
            id="T-109",
            first_name="Liam",
            last_name="O'Connor",
            phone="+1-415-555-0109",
            email="liam.oc@example.com",
            property_name="Highland Terrace",
            unit_number="Apt 3F"
        ),
        # Scenario 10: Grace Period Active (Due on 15th of the month)
        Tenant(
            id="T-110",
            first_name="Chloe",
            last_name="Kim",
            phone="+1-415-555-0110",
            email="chloe.kim@example.com",
            property_name="Sunset Highland Villas",
            unit_number="Townhouse 04"
        ),
        # Scenario 11: Unpaid & Eligible (High balance)
        Tenant(
            id="T-111",
            first_name="Devon",
            last_name="Brooks",
            phone="+1-415-555-0111",
            email="devon.b@example.com",
            property_name="Oakridge Luxury Apartments",
            unit_number="Unit 108"
        ),
        # Scenario 12: Promise made in Gmail email ("Will transfer on the 28th")
        Tenant(
            id="T-112",
            first_name="Rachel",
            last_name="Greenwood",
            phone="+1-415-555-0112",
            email="rachel.g@example.com",
            property_name="Highland Terrace",
            unit_number="Apt 1C"
        )
    ]

    for t in sample_tenants:
        db.upsert_tenant(t)

    # RentVine Records
    rv_records = [
        # T-101: Overdue $1,850
        RentVineRecord(
            tenant_id="T-101",
            balance=1850.00,
            monthly_rent=1850.00,
            due_date=f"{current_month_str}-01",
            last_payment_date=(now - timedelta(days=32)).strftime("%Y-%m-%d"),
            last_payment_amount=1850.00
        ),
        # T-102: Paid in full ($0 balance)
        RentVineRecord(
            tenant_id="T-102",
            balance=0.00,
            monthly_rent=2100.00,
            due_date=f"{current_month_str}-01",
            last_payment_date=now.strftime("%Y-%m-02"),
            last_payment_amount=2100.00
        ),
        # T-103: Overdue $2,400 (Will detect SMS promise)
        RentVineRecord(
            tenant_id="T-103",
            balance=2400.00,
            monthly_rent=2400.00,
            due_date=f"{current_month_str}-01",
            last_payment_date=(now - timedelta(days=34)).strftime("%Y-%m-%d"),
            last_payment_amount=2400.00
        ),
        # T-104: Overdue $1,650 (Has active note in RentVine)
        RentVineRecord(
            tenant_id="T-104",
            balance=1650.00,
            monthly_rent=1650.00,
            due_date=f"{current_month_str}-01",
            last_payment_date=(now - timedelta(days=33)).strftime("%Y-%m-%d"),
            last_payment_amount=1650.00
        ),
        # T-105: Overdue $2,750 (Dispute on AC)
        RentVineRecord(
            tenant_id="T-105",
            balance=2750.00,
            monthly_rent=2750.00,
            due_date=f"{current_month_str}-01",
            last_payment_date=(now - timedelta(days=35)).strftime("%Y-%m-%d"),
            last_payment_amount=2750.00
        ),
        # T-106: Overdue $1,900 (Claims paid via email)
        RentVineRecord(
            tenant_id="T-106",
            balance=1900.00,
            monthly_rent=1900.00,
            due_date=f"{current_month_str}-01",
            last_payment_date=(now - timedelta(days=31)).strftime("%Y-%m-%d"),
            last_payment_amount=1900.00
        ),
        # T-107: Overdue $1,450 (Eligible for reminder)
        RentVineRecord(
            tenant_id="T-107",
            balance=1450.00,
            monthly_rent=1450.00,
            due_date=f"{current_month_str}-01",
            last_payment_date=(now - timedelta(days=32)).strftime("%Y-%m-%d"),
            last_payment_amount=1450.00
        ),
        # T-108: Overdue $4,200 (Excluded)
        RentVineRecord(
            tenant_id="T-108",
            balance=4200.00,
            monthly_rent=4200.00,
            due_date=f"{current_month_str}-01"
        ),
        # T-109: Overdue $1,700 (Reminded 4h ago)
        RentVineRecord(
            tenant_id="T-109",
            balance=1700.00,
            monthly_rent=1700.00,
            due_date=f"{current_month_str}-01"
        ),
        # T-110: Overdue $2,250 (Mid-month cycle due on 24th, so in grace period)
        RentVineRecord(
            tenant_id="T-110",
            balance=2250.00,
            monthly_rent=2250.00,
            due_date=now.strftime("%Y-%m-%d")  # Due today, so within 3-day grace period
        ),
        # T-111: Overdue $1,950 (Eligible for reminder)
        RentVineRecord(
            tenant_id="T-111",
            balance=1950.00,
            monthly_rent=1950.00,
            due_date=f"{current_month_str}-01"
        ),
        # T-112: Overdue $1,550 (Email promise)
        RentVineRecord(
            tenant_id="T-112",
            balance=1550.00,
            monthly_rent=1550.00,
            due_date=f"{current_month_str}-01"
        )
    ]

    for rec in rv_records:
        db.upsert_rentvine_record(rec)

    # Note for T-104: Pre-existing active promise in RentVine
    future_date = (now + timedelta(days=4)).strftime("%Y-%m-%d")
    note_t104 = RentVineNote(
        id="note_seed_104",
        created_at=(now - timedelta(days=1)).isoformat(),
        author="Property Manager (Sarah Jenkins)",
        text=f"[AI PROMISE ON FILE: Will pay by {future_date}] Spoke with tenant; payroll delay. Promise accepted.",
        is_automated=False
    )
    db.add_rentvine_note("T-104", note_t104)

    # Note for T-102: Past payment confirmation note
    note_t102 = RentVineNote(
        id="note_seed_102",
        created_at=(now - timedelta(days=3)).isoformat(),
        author="RentVine MCP Portal",
        text="Automated payment $2,100.00 processed via ACH. Balance $0.00.",
        is_automated=True
    )
    db.add_rentvine_note("T-102", note_t102)

    # Communications:
    # T-103: Quo SMS thread containing a payment promise
    db.save_message(CommunicationMessage(
        id="sms_103_out",
        tenant_id="T-103",
        channel=Channel.SMS,
        direction=MessageDirection.OUTBOUND,
        sender="+1-800-REDBRICKS",
        recipient="+1-415-555-0103",
        content="Hi Jordan, friendly reminder: rent was due on the 1st. Please let us know when we can expect payment.",
        timestamp=(now - timedelta(days=2)).isoformat(),
        status="DELIVERED"
    ))
    db.save_message(CommunicationMessage(
        id="sms_103_in",
        tenant_id="T-103",
        channel=Channel.SMS,
        direction=MessageDirection.INBOUND,
        sender="+1-415-555-0103",
        recipient="+1-800-REDBRICKS",
        content="Hey sorry about the delay! My direct deposit lands this Friday, I'll pay the full $2,400 by Friday morning.",
        timestamp=(now - timedelta(hours=14)).isoformat(),
        status="DELIVERED"
    ))

    # T-105: Quo SMS thread containing a dispute
    db.save_message(CommunicationMessage(
        id="sms_105_in",
        tenant_id="T-105",
        channel=Channel.SMS,
        direction=MessageDirection.INBOUND,
        sender="+1-415-555-0105",
        recipient="+1-800-REDBRICKS",
        content="I am withholding rent until maintenance fixes the broken air conditioner in unit 08. It's been 10 days and nobody called me back.",
        timestamp=(now - timedelta(hours=8)).isoformat(),
        status="DELIVERED"
    ))

    # T-106: Gmail email thread claiming already paid
    db.save_message(CommunicationMessage(
        id="email_106_in",
        tenant_id="T-106",
        channel=Channel.EMAIL,
        direction=MessageDirection.INBOUND,
        sender="amina.m@example.com",
        recipient="rentals@redbrickpm.com",
        content="Subject: Re: Rent Notice - Unit 512\n\nHi Red Bricks Team, I already paid via the resident portal yesterday evening. Confirmation #RV-88392. Please verify your system.",
        timestamp=(now - timedelta(hours=6)).isoformat(),
        status="DELIVERED"
    ))

    # T-109: Recent reminder sent 4 hours ago (for deduplication test)
    prev_decision_t109 = WorkflowDecision(
        tenant_id="T-109",
        tenant_name="Liam O'Connor",
        property_unit="Highland Terrace Apt 3F",
        balance=1700.00,
        outcome=DecisionOutcome.SEND_REMINDER,
        reason="Unpaid balance $1,700.00 past grace period; dispatched Quo SMS reminder",
        details={"phone": "+1-415-555-0109"},
        timestamp=(now - timedelta(hours=4)).isoformat(),
        sms_sent=True,
        sms_message_id="quo_msg_prev_109",
        note_written=True,
        run_id="run_prior_batch"
    )
    db.save_decision(prev_decision_t109)

    # T-112: Gmail email thread promising payment
    db.save_message(CommunicationMessage(
        id="email_112_in",
        tenant_id="T-112",
        channel=Channel.EMAIL,
        direction=MessageDirection.INBOUND,
        sender="rachel.g@example.com",
        recipient="rentals@redbrickpm.com",
        content="Subject: Rent payment update - Apt 1C\n\nHello, I will transfer the rent on the 28th when my commission check clears. Thank you for your patience.",
        timestamp=(now - timedelta(hours=18)).isoformat(),
        status="DELIVERED"
    ))

    print(f"[OK] Successfully seeded database with {len(sample_tenants)} tenants and multi-channel records.")

if __name__ == "__main__":
    populate_sample_data(reset=True)

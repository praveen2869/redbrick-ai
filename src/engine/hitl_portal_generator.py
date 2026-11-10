"""
Red Bricks Property Management - Interactive HITL Review Portal Generator
Generates a standalone, responsive, interactive single-file HTML portal (hitl_portal.html)
allowing property managers to inspect every tenant, view promises & manual reviews,
toggle Approve/Reject on each reminder, and execute Stage 2 dispatch.
"""

import os
import json
from typing import List, Dict, Any


def generate_hitl_portal_html(
    pending_reminders: List[Dict[str, Any]],
    promises_on_file: List[Dict[str, Any]],
    manual_reviews: List[Dict[str, Any]],
    paid_cleared: List[Dict[str, Any]],
    summary_metrics: Dict[str, Any]
) -> str:
    """Generates a complete, interactive, self-contained HTML review portal."""

    payload = {
        "reminders": pending_reminders,
        "promises": promises_on_file,
        "manual_reviews": manual_reviews,
        "paid": paid_cleared[:100],  # sample first 100 paid
        "metrics": summary_metrics
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    # 1. Attempt to load base HTML template from web/hitl.html
    current_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(current_dir, "..", "..", "web", "hitl.html"),
        os.path.join(current_dir, "..", "web", "hitl.html"),
        os.path.join(os.getcwd(), "web", "hitl.html")
    ]

    template_html = None
    for cand in candidates:
        if os.path.exists(cand):
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    template_html = f.read()
                break
            except Exception:
                pass

    if template_html:
        # Use exact string replacement (NOT re.sub) to avoid Python re backslash mangling of \n in JSON
        if "/*__PAYLOAD_PLACEHOLDER__*/" in template_html:
            return template_html.replace("/*__PAYLOAD_PLACEHOLDER__*/", payload_json)
        elif "const DATA =" in template_html:
            idx1 = template_html.find("const DATA =")
            idx2 = template_html.find("let activeTab =", idx1)
            if idx2 != -1:
                return template_html[:idx1] + "const DATA = " + payload_json + ";\n\n" + template_html[idx2:]
        return template_html.replace("</head>", f"<script>const DATA = {payload_json};</script></head>")

    # Fallback minimal HTML if template is not found
    return f"""<!DOCTYPE html>
<html>
<head><title>HITL Review Portal</title></head>
<body>
<h1>Red Bricks PM — HITL Review</h1>
<p>Total Pending: {len(pending_reminders)}</p>
<pre>{payload_json}</pre>
</body>
</html>"""

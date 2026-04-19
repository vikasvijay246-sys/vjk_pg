import logging
"""
from api.utils import ok, fail, paginate, safe_route
api/phase3.py — Phase 3 Features:
  • Multi-PG management
  • WhatsApp/SMS rent reminders
  • UPI QR code generation
  • PDF monthly report export
  • i18n translation endpoint
"""

import os, io, json, uuid
from datetime import datetime, date
from flask import Blueprint, request, jsonify, send_file, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from extensions import db, limiter
from security import validate_upi, err, log_activity, sanitize
from db_models import (PGProperty, Room, Tenant, Payment, User,
                       ReminderLog, Notification)

p3_bp = Blueprint("phase3", __name__, url_prefix="/api/v2")


# ════════════════════════════════════════════════════════
#  MULTI-PG MANAGEMENT
# ════════════════════════════════════════════════════════

@p3_bp.route("/properties", methods=["GET"])
@jwt_required()
def list_properties():
    uid    = int(get_jwt_identity())
    claims = get_jwt()
    search = request.args.get("search", "").strip()
    page   = int(request.args.get("page", 1))
    per    = min(int(request.args.get("per_page", 20)), 100)

    if claims.get("role") == "owner":
        q = PGProperty.query.filter_by(owner_id=uid, is_active=True)
    else:
        q = PGProperty.query.filter_by(is_active=True)

    if search:
        q = q.filter(
            db.or_(
                PGProperty.name.ilike(f"%{search}%"),
                PGProperty.address.ilike(f"%{search}%") if hasattr(PGProperty, "address") else db.literal(False)
            )
        )

    total = q.count()
    props = q.order_by(PGProperty.name).offset((page-1)*per).limit(per).all()
    return jsonify({
        "properties": [p.to_dict() for p in props],
        "total": total, "page": page,
        "pages": max(1, (total + per - 1) // per),
    })


@p3_bp.route("/properties", methods=["POST"])
@jwt_required()
def create_property():
    if get_jwt().get("role") != "owner":
        return jsonify({"error": "Only owners can create properties"}), 403
    uid  = int(get_jwt_identity())
    data = request.get_json() or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Property name required"}), 400

    pg = PGProperty(
        owner_id=uid,
        name=name,
        address=data.get("address", ""),
        city=data.get("city", ""),
        upi_id=data.get("upi_id", ""),
        phone=data.get("phone", "")
    )
    db.session.add(pg)
    db.session.commit()
    return jsonify({"success": True, "property": pg.to_dict()}), 201


@p3_bp.route("/properties/<int:pid>", methods=["PUT"])
@jwt_required()
def update_property(pid):
    uid = int(get_jwt_identity())
    pg  = PGProperty.query.filter_by(id=pid, owner_id=uid).first_or_404()
    data = request.get_json() or {}
    for field in ["name", "address", "city", "upi_id", "phone"]:
        if field in data:
            setattr(pg, field, data[field])
    db.session.commit()
    return jsonify({"success": True, "property": pg.to_dict()})


@p3_bp.route("/properties/<int:pid>/summary", methods=["GET"])
@jwt_required()
def property_summary(pid):
    """Per-property dashboard stats."""
    pg = PGProperty.query.get_or_404(pid)
    today = date.today()
    ms = today.replace(day=1).isoformat()

    room_ids = [r.id for r in Room.query.filter_by(pg_id=pid).all()]
    tenants  = Tenant.query.filter(
        Tenant.room_id.in_(room_ids), Tenant.is_active == True
    ).all()

    paid_ids = [r[0] for r in db.session.query(Payment.tenant_id).filter(
        Payment.month >= ms, Payment.is_paid == True,
        Payment.tenant_id.in_([t.id for t in tenants])
    ).distinct()]

    total      = len(tenants)
    paid       = len(paid_ids)
    collected  = sum(t.rent_amount for t in tenants if t.id in paid_ids)
    expected   = sum(t.rent_amount for t in tenants)

    return jsonify({
        "property": pg.to_dict(),
        "total_tenants": total,
        "paid": paid,
        "unpaid": total - paid,
        "collected": round(collected, 2),
        "expected": round(expected, 2),
        "rooms": Room.query.filter_by(pg_id=pid).count(),
    })


# ════════════════════════════════════════════════════════
#  WHATSAPP / SMS RENT REMINDERS
# ════════════════════════════════════════════════════════

def _compose_reminder(tenant_name, room, amount, month, lang="en"):
    """Build reminder message in given language."""
    msgs = {
        "en": (f"Dear {tenant_name},\n"
               f"Your rent of ₹{amount:,.0f} for Room {room} is due for {month}.\n"
               f"Please pay at the earliest. Thank you! 🙏"),
        "te": (f"ప్రియమైన {tenant_name},\n"
               f"రూమ్ {room} కి {month} నెల అద్దె ₹{amount:,.0f} బాకీ ఉంది.\n"
               f"దయచేసి వీలైనంత త్వరగా చెల్లించండి. ధన్యవాదాలు! 🙏"),
        "hi": (f"प्रिय {tenant_name},\n"
               f"Room {room} का {month} का किराया ₹{amount:,.0f} बकाया है।\n"
               f"कृपया जल्द भुगतान करें। धन्यवाद! 🙏"),
    }
    return msgs.get(lang, msgs["en"])


@p3_bp.route("/reminders/send", methods=["POST"])
@jwt_required()
@limiter.limit("50 per hour")
def send_reminders():
    """
    Send WhatsApp/app reminders to unpaid tenants.
    In production: integrate Twilio / Meta WhatsApp Business API.
    Here we simulate and log the message.
    """
    if get_jwt().get("role") not in ("owner", "worker"):
        return jsonify({"error": "Access denied"}), 403

    data      = request.get_json() or {}
    channel   = data.get("channel", "app")   # "app" | "whatsapp" | "sms"
    lang      = data.get("lang", "en")
    tenant_ids= data.get("tenant_ids")       # None = all unpaid

    today = date.today()
    ms    = today.replace(day=1).isoformat()
    month = today.strftime("%B %Y")

    # Find unpaid tenants
    paid_ids = [r[0] for r in db.session.query(Payment.tenant_id).filter(
        Payment.month >= ms, Payment.is_paid == True
    ).distinct()]

    q = Tenant.query.filter(Tenant.is_active == True, ~Tenant.id.in_(paid_ids))
    if tenant_ids:
        q = q.filter(Tenant.id.in_(tenant_ids))
    unpaid_tenants = q.all()

    sent, failed = [], []
    for t in unpaid_tenants:
        try:
            msg = _compose_reminder(
                t.user.name if t.user else "Tenant",
                t.room.room_number if t.room else "—",
                t.rent_amount,
                month,
                lang
            )

            # ── WhatsApp (Twilio sandbox or Meta API) ──────────
            if channel == "whatsapp":
                # Production: uncomment and configure
                # from twilio.rest import Client
                # client = Client(os.environ["TWILIO_SID"], os.environ["TWILIO_AUTH"])
                # client.messages.create(
                #     from_="whatsapp:+14155238886",
                #     body=msg,
                #     to=f"whatsapp:+91{t.user.phone}"
                # )
                pass  # Simulated

            # ── App notification ───────────────────────────────
            elif channel == "app":
                db.session.add(Notification(
                    user_id=t.user_id,
                    notif_type="reminder",
                    title=f"⏰ Rent Reminder — {month}",
                    body=f"Your rent of ₹{t.rent_amount:,.0f} is due.",
                    data_json=json.dumps({"tenant_id": t.id})
                ))

            # Log it
            log = ReminderLog(
                tenant_id=t.id, channel=channel, message=msg,
                status="sent", sent_at=datetime.utcnow()
            )
            db.session.add(log)
            sent.append({"tenant_id": t.id, "name": t.user.name if t.user else "", "phone": t.user.phone if t.user else ""})

        except Exception as e:
            failed.append({"tenant_id": t.id, "error": str(e)})

    db.session.commit()
    return jsonify({
        "success": True,
        "sent_count": len(sent),
        "failed_count": len(failed),
        "sent": sent,
        "failed": failed,
        "channel": channel
    })


@p3_bp.route("/reminders/log", methods=["GET"])
@jwt_required()
def reminder_log():
    if get_jwt().get("role") not in ("owner", "worker"):
        return jsonify({"error": "Access denied"}), 403
    logs = ReminderLog.query.order_by(ReminderLog.created_at.desc()).limit(100).all()
    return jsonify([l.to_dict() for l in logs])


# ════════════════════════════════════════════════════════
#  UPI QR CODE GENERATION
# ════════════════════════════════════════════════════════

@p3_bp.route("/upi-qr", methods=["GET"])
@jwt_required()
def generate_upi_qr():
    """
    Generate UPI payment QR code for a tenant.
    Query params: tenant_id, amount (optional override), upi_id
    """
    import qrcode
    from qrcode.image.pil import PilImage

    tid    = request.args.get("tenant_id")
    raw_upi = request.args.get("upi_id", "pgmanager@upi")
    amount  = request.args.get("amount")
    # Validate UPI ID
    try:
        upi_id = validate_upi(raw_upi)
    except ValueError as e:
        return err(str(e), 400)

    tenant_name = "Tenant"
    if tid:
        t = Tenant.query.get(tid)
        if t:
            tenant_name = t.user.name if t.user else "Tenant"
            if not amount:
                amount = str(int(t.rent_amount))

    # UPI deep-link format
    # upi://pay?pa=UPI_ID&pn=NAME&am=AMOUNT&cu=INR&tn=RENT
    upi_str = (
        f"upi://pay?pa={upi_id}"
        f"&pn={tenant_name.replace(' ', '%20')}"
        f"&am={amount or ''}"
        f"&cu=INR"
        f"&tn=PG%20Rent"
    )

    qr = qrcode.QRCode(version=1, box_size=10, border=4,
                       error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(upi_str)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1C2340", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return send_file(buf, mimetype="image/png",
                     as_attachment=False,
                     download_name=f"upi_qr_{tid or 'pg'}.png")


# ════════════════════════════════════════════════════════
#  PDF REPORT EXPORT
# ════════════════════════════════════════════════════════

@p3_bp.route("/reports/pdf", methods=["GET"])
@jwt_required()
def export_pdf():
    """
    Export monthly rent report as a professionally formatted PDF.
    Query params: month (YYYY-MM-01), pg_id (optional)
    """
    if get_jwt().get("role") not in ("owner", "worker"):
        return jsonify({"error": "Access denied"}), 403

    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, HRFlowable)
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT

    month = request.args.get("month", date.today().replace(day=1).isoformat())
    try:
        month_dt = datetime.strptime(month, "%Y-%m-%d")
        month_label = month_dt.strftime("%B %Y")
    except Exception:
        month_label = month

    tenants = Tenant.query.filter_by(is_active=True).all()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                             rightMargin=2*cm, leftMargin=2*cm,
                             topMargin=2*cm, bottomMargin=2*cm)

    styles = getSampleStyleSheet()
    BRAND  = colors.HexColor("#E8520A")
    NAVY   = colors.HexColor("#1C2340")
    GREEN  = colors.HexColor("#16A34A")
    RED    = colors.HexColor("#DC2626")
    LGREY  = colors.HexColor("#F5F5F0")

    title_style = ParagraphStyle("title", parent=styles["Title"],
                                  textColor=NAVY, fontSize=22, spaceAfter=4)
    sub_style   = ParagraphStyle("sub", parent=styles["Normal"],
                                  textColor=BRAND, fontSize=11, spaceAfter=12)
    label_style = ParagraphStyle("label", parent=styles["Normal"],
                                  textColor=colors.grey, fontSize=8)

    story = []

    # Header
    story.append(Paragraph("🏠 PG Manager Pro", title_style))
    story.append(Paragraph(f"Monthly Rent Report — {month_label}", sub_style))
    story.append(HRFlowable(width="100%", thickness=2, color=BRAND))
    story.append(Spacer(1, 0.4*cm))

    # Summary stats
    paid_ids = [r[0] for r in db.session.query(Payment.tenant_id).filter(
        Payment.month == month, Payment.is_paid == True
    ).distinct()]
    paid_count   = len(paid_ids)
    unpaid_count = len(tenants) - paid_count
    collected    = sum(t.rent_amount for t in tenants if t.id in paid_ids)
    expected     = sum(t.rent_amount for t in tenants)

    summary_data = [
        ["Total Tenants", str(len(tenants)),
         "Collected",     f"Rs {collected:,.0f}"],
        ["Paid",          str(paid_count),
         "Pending",       f"Rs {expected - collected:,.0f}"],
        ["Unpaid",        str(unpaid_count),
         "Expected",      f"Rs {expected:,.0f}"],
    ]
    summary_table = Table(summary_data, colWidths=[4*cm, 2.5*cm, 4*cm, 3*cm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LGREY),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("FONTNAME", (3, 0), (3, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (1, 1), (1, 1), GREEN),   # paid count green
        ("TEXTCOLOR", (1, 2), (1, 2), RED),     # unpaid count red
        ("TEXTCOLOR", (3, 0), (3, 0), GREEN),   # collected green
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LGREY]),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 0.5*cm))

    # Detailed table
    story.append(Paragraph(f"Tenant Details", sub_style))
    header = ["#", "Name", "Phone", "Room", "Rent (Rs)", "Status", "Paid On"]
    table_data = [header]

    for i, t in enumerate(tenants, 1):
        p = Payment.query.filter_by(tenant_id=t.id, month=month).first()
        is_paid = p.is_paid if p else False
        paid_on = (p.paid_on if (p and p.is_paid) else "—")

        table_data.append([
            str(i),
            (t.user.name if t.user else "—")[:22],
            (t.user.phone if t.user else "—"),
            (t.room.room_number if t.room else "—"),
            f"{t.rent_amount:,.0f}",
            "PAID" if is_paid else "DUE",
            paid_on,
        ])

    col_w = [1*cm, 4.5*cm, 3*cm, 2*cm, 2.5*cm, 1.8*cm, 2.5*cm]
    detail_table = Table(table_data, colWidths=col_w, repeatRows=1)

    row_styles = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, -1), 8),
        ("GRID",       (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ("PADDING",    (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LGREY]),
        ("ALIGN",      (4, 0), (4, -1), "RIGHT"),
        ("ALIGN",      (5, 0), (5, -1), "CENTER"),
    ]
    # Color PAID/DUE cells
    for i, t in enumerate(tenants, 1):
        p = Payment.query.filter_by(tenant_id=t.id, month=month).first()
        is_paid = p.is_paid if p else False
        color = GREEN if is_paid else RED
        row_styles.append(("TEXTCOLOR", (5, i), (5, i), color))
        row_styles.append(("FONTNAME",  (5, i), (5, i), "Helvetica-Bold"))

    detail_table.setStyle(TableStyle(row_styles))
    story.append(detail_table)
    story.append(Spacer(1, 0.8*cm))

    # Footer
    footer_style = ParagraphStyle("footer", parent=styles["Normal"],
                                   textColor=colors.grey, fontSize=7,
                                   alignment=TA_CENTER)
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(
        f"Generated by PG Manager Pro on {datetime.now().strftime('%d %b %Y %H:%M')} | "
        f"This is a computer-generated report",
        footer_style
    ))

    doc.build(story)
    buf.seek(0)

    return send_file(
        buf, mimetype="application/pdf",
        as_attachment=True,
        download_name=f"rent_report_{month_label.replace(' ', '_')}.pdf"
    )


# ════════════════════════════════════════════════════════
#  i18n TRANSLATION STRINGS
# ════════════════════════════════════════════════════════

TRANSLATIONS = {
    "en": {
        "home": "Home", "tenants": "Tenants", "due": "Due", "add": "Add",
        "settings": "Settings", "messages": "Messages", "feed": "Feed",
        "add_tenant": "Add Tenant", "all_tenants": "All Tenants",
        "due_payments": "Due Payments", "reports": "Reports",
        "mark_paid": "Mark as Paid", "mark_unpaid": "Mark Unpaid",
        "remove": "Remove Tenant", "save": "Save",
        "name": "Full Name", "phone": "Phone Number", "room": "Room",
        "rent": "Rent Amount", "joining_date": "Joining Date",
        "login": "Login", "logout": "Logout", "register": "Register",
        "welcome": "Welcome", "good_morning": "Good morning",
        "good_afternoon": "Good afternoon", "good_evening": "Good evening",
        "paid": "Paid", "unpaid": "Due", "total_tenants": "Tenants",
        "collected": "Collected", "expected": "Expected",
        "write_comment": "Write a comment...", "reply": "Reply",
        "like": "Like", "comment": "Comment", "share": "Share",
        "video": "Videos", "personal": "Personal", "notice": "Notice Board",
        "send_reminder": "Send Reminder", "download_pdf": "Download PDF",
        "scan_upi": "Scan to Pay (UPI)", "language": "Language", "theme": "Theme",
        "notifications": "Notifications", "files": "Files",
        "password": "Password", "photo": "Photo", "id_proof": "ID Proof",
        "search": "Search...", "no_tenants": "No tenants found",
        "add_first": "Tap + to add your first tenant",
        "all_paid": "All rents collected!", "great_job": "Great job this month!",
        "owner": "Owner", "worker": "Worker", "tenant": "Tenant",
        "dark": "Dark", "light": "Light", "high_contrast": "High Contrast",
    },
    "te": {
        "home": "హోమ్", "tenants": "అద్దెదారులు", "due": "బాకీ", "add": "జోడించు",
        "settings": "సెట్టింగ్స్", "messages": "సందేశాలు", "feed": "ఫీడ్",
        "add_tenant": "అద్దెదారుని జోడించు", "all_tenants": "అందరు అద్దెదారులు",
        "due_payments": "బాకీ చెల్లింపులు", "reports": "నివేదికలు",
        "mark_paid": "చెల్లించినట్లు గుర్తించు", "mark_unpaid": "చెల్లించలేదు",
        "remove": "అద్దెదారుని తొలగించు", "save": "సేవ్ చేయి",
        "name": "పూర్తి పేరు", "phone": "ఫోన్ నంబర్", "room": "గది",
        "rent": "అద్దె మొత్తం", "joining_date": "చేరిన తేదీ",
        "login": "లాగిన్", "logout": "లాగ్అవుట్", "register": "నమోదు",
        "welcome": "స్వాగతం", "good_morning": "శుభోదయం",
        "good_afternoon": "శుభ మధ్యాహ్నం", "good_evening": "శుభ సాయంత్రం",
        "paid": "చెల్లించారు", "unpaid": "బాకీ", "total_tenants": "అద్దెదారులు",
        "collected": "వసూలైంది", "expected": "మొత్తం",
        "write_comment": "వ్యాఖ్య రాయండి...", "reply": "జవాబు",
        "like": "లైక్", "comment": "వ్యాఖ్య", "share": "షేర్",
        "video": "వీడియోలు", "personal": "వ్యక్తిగతం", "notice": "నోటీస్ బోర్డ్",
        "send_reminder": "రిమైండర్ పంపు", "download_pdf": "PDF డౌన్‌లోడ్",
        "scan_upi": "UPI తో చెల్లించండి", "language": "భాష", "theme": "థీమ్",
        "notifications": "నోటిఫికేషన్లు", "files": "ఫైళ్ళు",
        "password": "పాస్‌వర్డ్", "photo": "ఫోటో", "id_proof": "గుర్తింపు కార్డ్",
        "search": "వెతకండి...", "no_tenants": "అద్దెదారులు లేరు",
        "add_first": "+ నొక్కి మొదటి అద్దెదారుని జోడించండి",
        "all_paid": "అందరూ అద్దె చెల్లించారు!", "great_job": "ఈ నెల చాలా బాగుంది!",
        "owner": "యజమాని", "worker": "వర్కర్", "tenant": "అద్దెదారు",
        "dark": "డార్క్", "light": "లైట్", "high_contrast": "హై కాంట్రాస్ట్",
    },
    "hi": {
        "home": "होम", "tenants": "किरायेदार", "due": "बकाया", "add": "जोड़ें",
        "settings": "सेटिंग्स", "messages": "संदेश", "feed": "फ़ीड",
        "add_tenant": "किरायेदार जोड़ें", "all_tenants": "सभी किरायेदार",
        "due_payments": "बकाया भुगतान", "reports": "रिपोर्ट",
        "mark_paid": "भुगतान हुआ", "mark_unpaid": "अभी तक नहीं",
        "remove": "हटाएं", "save": "सहेजें",
        "name": "पूरा नाम", "phone": "फ़ोन नंबर", "room": "कमरा",
        "rent": "किराया राशि", "joining_date": "आने की तारीख",
        "login": "लॉगिन", "logout": "लॉगआउट", "register": "पंजीकरण",
        "welcome": "स्वागत", "good_morning": "सुप्रभात",
        "good_afternoon": "नमस्ते", "good_evening": "शुभ संध्या",
        "paid": "भुगतान हुआ", "unpaid": "बकाया", "total_tenants": "किरायेदार",
        "collected": "मिला", "expected": "अपेक्षित",
        "write_comment": "टिप्पणी लिखें...", "reply": "जवाब दें",
        "like": "पसंद", "comment": "टिप्पणी", "share": "शेयर",
        "video": "वीडियो", "personal": "व्यक्तिगत", "notice": "नोटिस बोर्ड",
        "send_reminder": "रिमाइंडर भेजें", "download_pdf": "PDF डाउनलोड",
        "scan_upi": "UPI से भुगतान करें", "language": "भाषा", "theme": "थीम",
        "notifications": "सूचनाएं", "files": "फ़ाइलें",
        "password": "पासवर्ड", "photo": "फ़ोटो", "id_proof": "पहचान पत्र",
        "search": "खोजें...", "no_tenants": "कोई किरायेदार नहीं",
        "add_first": "+ दबाएं और पहला किरायेदार जोड़ें",
        "all_paid": "सभी किराया मिल गया!", "great_job": "इस महीने बढ़िया रहा!",
        "owner": "मालिक", "worker": "कार्यकर्ता", "tenant": "किरायेदार",
        "dark": "डार्क", "light": "लाइट", "high_contrast": "हाई कॉन्ट्रास्ट",
    }
}

@p3_bp.route("/i18n/<lang>", methods=["GET"])
def get_translations(lang):
    """Return UI translation strings for the given language."""
    if lang not in TRANSLATIONS:
        return jsonify({"error": "Language not supported"}), 404
    return jsonify(TRANSLATIONS[lang])


# ════════════════════════════════════════════════════════
#  BACKUP / EXPORT
# ════════════════════════════════════════════════════════
@p3_bp.route("/backup", methods=["GET"])
@jwt_required()
def export_backup():
    """Export all PG data as JSON for owner backup."""
    if get_jwt().get("role") not in ("owner", "worker"):
        return jsonify({"error": "Access denied"}), 403

    from db_models import User, Room, Tenant, Payment, Notification
    import json as _json

    def _tenants():
        out = []
        for t in Tenant.query.all():
            d = t.to_dict()
            d["payments"] = [p.to_dict() for p in
                             Payment.query.filter_by(tenant_id=t.id)
                             .order_by(Payment.month.desc()).all()]
            out.append(d)
        return out

    payload = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "rooms": [r.to_dict() for r in Room.query.all()],
        "tenants": _tenants(),
        "properties": [p.to_dict() for p in PGProperty.query.all()],
    }

    buf = io.BytesIO(_json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    buf.seek(0)
    fname = f"pg_backup_{date.today().isoformat()}.json"
    log_activity(int(__import__('flask_jwt_extended').get_jwt_identity()), 'backup_downloaded')
    return send_file(buf, mimetype="application/json",
                     as_attachment=True, download_name=fname)

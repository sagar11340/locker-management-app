
from flask import Flask, render_template, request, redirect, url_for, make_response

from pymongo import MongoClient, ReturnDocument
from bson.objectid import ObjectId
from datetime import datetime, date as _date, timedelta
from dateutil.relativedelta import relativedelta
import os
from dotenv import load_dotenv
import os
from pymongo import MongoClient

load_dotenv()


# ---------- MongoDB connection ----------
MONGO_URI = os.getenv("MONGO_URI")
SECRET_KEY = os.getenv("SECRET_KEY")
client = MongoClient(MONGO_URI)
db = client.get_database("rkm_locker_db")

# ✅ define collections (VERY IMPORTANT)
lockers = db["lockers"]
payments = db["payments"]
counters = db["counters"]



# ✅ create the Flask app FIRST
app = Flask(__name__)

# ✅ then define print_routes AFTER app is defined
def print_routes():
    print("----- Registered routes -----")
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: (r.endpoint, str(r))):
        methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        print(f"{rule} -> endpoint: {rule.endpoint}  methods: {methods}")
    print("-----------------------------")




def _id_repr(doc):
    return str(doc.get('_id')) if doc and doc.get('_id') else "<no-id>"



def get_next_sequence(name):
    seq = counters.find_one_and_update(
        {"_id": name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    return seq["seq"]

# ---------- Constants ----------
DEFAULT_MONTHLY_FEE = 200
KEY_MISSING_FINE = 150
LATE_FINE_PER_DAY = 10

# ---------- Helpers ----------
def parse_date(dstr):
    """expects YYYY-MM-DD from input[type=date] and returns datetime at midnight"""
    if not dstr:
        return None
    try:
        return datetime.strptime(dstr, "%Y-%m-%d")
    except Exception:
        # try ISO parse fallback
        try:
            return datetime.fromisoformat(dstr)
        except Exception:
            return None

def normalize_to_date(dt_like):
    """Return a date object (datetime.date) from various inputs (datetime, date, str)."""
    if dt_like is None:
        return None
    if isinstance(dt_like, _date) and not isinstance(dt_like, datetime):
        return dt_like
    if isinstance(dt_like, datetime):
        return dt_like.date()
    if isinstance(dt_like, str):
        # try parse common formats
        # attempt ISO
        try:
            dt = datetime.fromisoformat(dt_like)
            return dt.date()
        except Exception:
            try:
                dt = datetime.strptime(dt_like, "%Y-%m-%d")
                return dt.date()
            except Exception:
                return None
    return None

@app.template_filter('dateformat')
def dateformat(value, fmt="%d/%m/%Y"):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime(fmt)
    if isinstance(value, _date):
        return value.strftime(fmt)
    if isinstance(value, str):
        try:
            dt = datetime.strptime(value, "%Y-%m-%d")
            return dt.strftime(fmt)
        except Exception:
            try:
                dt = datetime.fromisoformat(value)
                return dt.strftime(fmt)
            except Exception:
                return value
    return str(value)

@app.context_processor
def inject_now():
    return {"datetime": datetime, "date": _date}

# ---------- Routes ----------
@app.route('/')
def index():
    return redirect(url_for("dashboard"))


@app.route('/add', methods=['GET', 'POST'])
def add_locker():
    if request.method == 'POST':
        start_dt = parse_date(request.form.get('start_date'))

        doc = {
            "start_date": start_dt,
            "end_date": None,   # expiry handled elsewhere (receipt logic)
            "full_name": request.form.get('full_name'),
            "membership_id": request.form.get('membership_id'),
            "locker_no": request.form.get('locker_no'),
            "mobile": request.form.get('mobile') or None,
            "gender": request.form.get('gender'),
            "status": "active",
            "created_at": datetime.now(timezone.utc)
        }

        lockers.insert_one(doc)
        return redirect(url_for('dashboard'))

    return render_template('add.html')


@app.route('/view', methods=['GET'])
def view_lockers():
    q = {}
    qname = request.args.get('q', '').strip()
    membership_id = request.args.get('membership_id', '').strip()
    locker_no = request.args.get('locker_no', '').strip()
    if qname:
        q["full_name"] = {"$regex": qname, "$options": "i"}
    if membership_id:
        q["membership_id"] = {"$regex": membership_id, "$options": "i"}
    if locker_no:
        q["locker_no"] = {"$regex": locker_no, "$options": "i"}

    docs = list(lockers.find(q).sort("end_date", 1))
    today = now = datetime.utcnow().date()

    for d in docs:
        ed = d.get("end_date")
        ed_date = normalize_to_date(ed)
        try:
            d["days_to_expire"] = (ed_date - today).days if ed_date else None
        except Exception:
            d["days_to_expire"] = None

    return render_template('view.html', docs=docs)

# ---------- Updated make_payment route ----------
@app.route('/payment/<id>', methods=['GET', 'POST'])
def make_payment(id):
    doc = lockers.find_one({"_id": ObjectId(id)})
    if not doc:
        return "Not found", 404

    # normalize existing end_date if present
    existing_end_date = None
    raw_end = doc.get('end_date')
    existing_end_date = normalize_to_date(raw_end)

    if request.method == 'POST':
        # parse submitted payment_date (datetime)
        pd_str = request.form.get('payment_date', '').strip()
        payment_dt = parse_date(pd_str) or  datetime.now(timezone.utc)

        payment_date_only = payment_dt.date()

        # cancel hidden + checkbox pattern
        cancel_flag = request.form.get('cancel', '0')
        is_cancel = True if str(cancel_flag) == '1' else False

        # months selection (default 1)
        try:
            months = int(request.form.get('months', '1'))
            if months < 1:
                months = 1
        except Exception:
            months = 1

        # monthly override handling
        monthly_override = request.form.get('monthly_fee_override', '').strip()
        monthly_fee_used = DEFAULT_MONTHLY_FEE
        if is_cancel:
            monthly_fee_used = 0.0
        else:
            if monthly_override != '':
                # try parse numeric portion (allow commas / currency symbols)
                try:
                    cleaned = ''.join(ch for ch in monthly_override if (ch.isdigit() or ch in '.-'))
                    monthly_fee_used = float(cleaned) if cleaned not in ('', '-', '.') else DEFAULT_MONTHLY_FEE
                    if monthly_fee_used < 0:
                        monthly_fee_used = 0.0
                except Exception:
                    monthly_fee_used = DEFAULT_MONTHLY_FEE
            else:
                monthly_fee_used = DEFAULT_MONTHLY_FEE

        # key missing
        try:
            key_missing = int(request.form.get('key_missing', '0'))
        except Exception:
            key_missing = 0
        key_missing_fine = KEY_MISSING_FINE if key_missing == 1 else 0

        # compute late days relative to existing_end_date (not the new end we're about to compute)
        late_days_actual = 0
        if existing_end_date:
            try:
                if payment_date_only > existing_end_date:
                    late_days_actual = (payment_date_only - existing_end_date).days
                else:
                    late_days_actual = 0
            except Exception:
                late_days_actual = 0
        else:
            late_days_actual = 0

        # whether staff wants to charge late fine for this payment
        charge_late_flag = request.form.get('charge_late', '1')
        charge_late_choice = True if str(charge_late_flag) == '1' else False

        # some lockers may have permanent exempt flag (no late fine)
        permanent_exempt = bool(doc.get('no_late_fine', False))
        if permanent_exempt:
            charged_late_days = 0
            charged_late_fine = 0
        else:
            charged_late_days = late_days_actual if charge_late_choice else 0
            charged_late_fine = charged_late_days * LATE_FINE_PER_DAY

        # compute start date using rule:
        # if existing_end_date -> start = existing_end_date + 1 day unless payment_date > that -> then payment_date
        if existing_end_date:
            potential_start = existing_end_date + timedelta(days=1)
            if payment_date_only > potential_start:
                start_date = payment_date_only
            else:
                start_date = potential_start
        else:
            start_date = payment_date_only

        # if cancelled -> no extension
        if is_cancel:
            total_amount = 0
            used_monthly_fee = 0.0
            computed_end_date = None
        else:
            used_monthly_fee = float(monthly_fee_used)
            base_total = used_monthly_fee * months
            total_amount = int(round(base_total + key_missing_fine + charged_late_fine))
            computed_end_date = start_date + relativedelta(months=months)

        # prepare payment document to save (server-side canonical)
        receipt_no = get_next_sequence("receipt_no")
        payment_doc = {
            "locker_id": doc['_id'],
            "receipt_no": receipt_no,
            "payment_date": payment_dt,
            "months": months,
            "monthly_fee_used": int(round(used_monthly_fee)),
            "key_missing": bool(key_missing),
            "key_missing_fine": int(key_missing_fine),
            "late_days_actual": int(late_days_actual),
            "late_days_charged": int(charged_late_days),
            "late_fine": int(charged_late_fine),
            "charge_late_choice": bool(charge_late_choice),
            "permanent_exempt_applied": permanent_exempt,
            "total": int(total_amount),
            "membership_id": doc.get('membership_id'),
            "full_name": doc.get('full_name'),
            "locker_no": doc.get('locker_no'),
            "cancelled": bool(is_cancel),
            "created_at": datetime.now(timezone.utc)


        }

        # Insert payment
        payments.insert_one(payment_doc)

        # --- NEW: update locker with last_paid_months and last_payment_at (or clear on cancel) ---
        try:
            if is_cancel:
                # clear months info on cancel to avoid stale display
                lockers.update_one(
                    {"_id": doc['_id']},
                    {"$unset": {"last_paid_months": "", "last_payment_at": ""}}
                )
            else:
                lockers.update_one(
    {"_id": doc["_id"]},
    {
        "$set": {
            "last_paid_months": int(months),
            "last_payment_at": datetime.now(timezone.utc)
        }
    }
)
        except Exception as e:
            print("WARN: failed to update locker with last_paid_months", e)
        # --- end NEW update ---

        # debug output
        print("DEBUG payment saved:", {
            "receipt_no": receipt_no,
            "membership_id": doc.get('membership_id'),
            "locker_no": doc.get('locker_no'),
            "months": months,
            "monthly_fee_used": used_monthly_fee,
            "key_missing_fine": key_missing_fine,
            "late_fine": charged_late_fine,
            "total": total_amount,
            "is_cancel": is_cancel
        })

        # Apply locker updates
        if is_cancel:
            # clear assignment and make available
            orig_membership = doc.get('membership_id')
            res = lockers.update_one(
                {"_id": doc['_id']},
                {
                    "$set": {
                        "status": "available",
                        "cancelled_at":  datetime.utcnow()
                    },
                    "$unset": {
                        "membership_id": "",
                        "full_name": "",
                        "mobile": "",
                        "start_date": "",
                        "end_date": "",
                        "gender": "",
                        "updated_at": ""
                    }
                }
            )
            print(f"DEBUG cancel result for locker {_id_repr(doc)}: matched={res.matched_count}, modified={res.modified_count}")

            # Also clear any duplicates for same membership ID
            if orig_membership:
                res2 = lockers.update_many(
                    {
                        "membership_id": {"$regex": f'^{orig_membership}$', "$options": "i"},
                        "_id": {"$ne": doc['_id']}
                    },
                    {
                        "$set": {"status": "available", "cancelled_at":  datetime.utcnow()},
                        "$unset": {
                            "membership_id": "",
                            "full_name": "",
                            "mobile": "",
                            "start_date": "",
                            "end_date": "",
                            "gender": "",
                            "updated_at": ""
                        }
                    }
                )
                print(f"DEBUG cleared duplicates for membership {orig_membership}: matched={res2.matched_count}, modified={res2.modified_count}")

        else:
            # Renew / extend only if monthly fee > 0
            try:
                monthly_fee_num = float(used_monthly_fee)
            except Exception:
                monthly_fee_num = 0.0

            if monthly_fee_num > 0:
                # store start_date and end_date as datetimes at midnight
                start_dt_dt = datetime.combine(start_date, datetime.min.time())
                end_dt_dt = datetime.combine(computed_end_date, datetime.min.time()) if computed_end_date else None
                update_fields = {
                    "start_date": start_dt_dt,
                    "end_date": end_dt_dt,
                    "status": "active",
                    "updated_at":  datetime.utcnow()

                }
                res = lockers.update_one({"_id": doc['_id']}, {"$set": update_fields})
                print(f"DEBUG extend result: matched={res.matched_count}, modified={res.modified_count}, new_end={end_dt_dt}")

        # render receipt (server canonical values)
        payment_doc['start_date'] = datetime.combine(start_date, datetime.min.time()) if start_date else None
        payment_doc['end_date'] = datetime.combine(computed_end_date, datetime.min.time()) if computed_end_date else None

        return render_template('receipt.html', payment=payment_doc, receipt_date=payment_dt)

    # GET -> show form
    return render_template('make_payment.html', doc=doc, today=datetime.utcnow())



@app.route('/receipt/<int:receipt_no>')
def view_receipt(receipt_no):
    pay = payments.find_one({"receipt_no": receipt_no})
    if not pay:
        return "Receipt not found", 404
    return render_template('receipt.html', payment=pay, receipt_date=pay.get('payment_date'))

@app.route('/monthly_report', methods=['GET', 'POST'])
def monthly_report():
    if request.method == 'POST':
        from_date = parse_date(request.form['from_date'])
        to_date = parse_date(request.form['to_date'])
        pays = list(payments.find({"payment_date": {"$gte": from_date, "$lte": to_date}}).sort("payment_date", 1))
        total_sum = sum(p.get('total', 0) for p in pays)
        return render_template('monthly_report.html', pays=pays, total_sum=total_sum, from_date=from_date, to_date=to_date)
    return render_template('monthly_report.html', pays=None)

@app.route('/student_check', methods=['GET', 'POST'])
def student_check():
    results = []
    error = None

    if request.method == 'POST':
        membership_id = request.form.get('membership_id', '').strip()

        if not membership_id:
            error = "Please enter your Membership ID."
        else:
            docs = list(
                lockers.find(
                    {"membership_id": {"$regex": f'^{membership_id}$', "$options": "i"}}
                ).sort("created_at", -1)
            )

            if not docs:
                error = "No record found for this Membership ID."
            else:
                today = datetime.utcnow().date()

                for doc in docs:
                    sd = doc.get('start_date')
                    ed = doc.get('end_date')

                    ed_date = ed.date() if isinstance(ed, datetime) else None
                    days_left = (ed_date - today).days if ed_date else None

                    results.append({
                        "full_name": doc.get('full_name'),
                        "membership_id": doc.get('membership_id'),
                        "locker_no": doc.get('locker_no'),
                        "mobile": doc.get('mobile'),
                        "gender": doc.get('gender'),
                        "start_date": sd,
                        "end_date": ed,
                        "end_date_str": ed_date.strftime("%d/%m/%Y") if ed_date else "—",
                        "days_left": days_left,
                        "expired": days_left is not None and days_left < 0,
                        "status": doc.get('status', 'active')
                    })

    return render_template(
        'student_check.html',
        results=results,
        error=error
    )

from datetime import datetime
from bson.objectid import ObjectId
from flask import request, redirect, url_for, render_template

@app.route('/edit/<id>', methods=['GET', 'POST'])
def edit_locker(id):
    doc = lockers.find_one({"_id": ObjectId(id)})
    if not doc:
        return "Locker not found", 404

    if request.method == 'POST':
        update = {
            "full_name": request.form.get('full_name') or None,
            "membership_id": request.form.get('membership_id') or None,
            "locker_no": request.form.get('locker_no') or None,
            "mobile": request.form.get('mobile') or None,
            "gender": request.form.get('gender') or None,
            "updated_at": datetime.utcnow()
        }

        start_date_str = request.form.get('start_date')
        if start_date_str:
            try:
                new_start = parse_date(start_date_str)
                update["start_date"] = new_start
                # 🚫 DO NOT set end_date here
            except Exception:
                pass

        lockers.update_one(
            {"_id": doc['_id']},
            {"$set": update}
        )

        return redirect(url_for('view_lockers'))

    return render_template('edit_locker.html', doc=doc)


@app.route('/delete/<id>', methods=['POST', 'GET'])
def delete_locker(id):
    try:
        lockers.delete_one({"_id": ObjectId(id)})
    except Exception:
        pass
    return redirect(url_for('view_lockers'))

from datetime import datetime, timezone
from bson import ObjectId

@app.route('/dashboard')
def dashboard():
    docs = list(lockers.find({}))

    # convert ObjectId to string
    for d in docs:
        if '_id' in d and isinstance(d['_id'], ObjectId):
            d['_id'] = str(d['_id'])

    # map lockers by number
    locker_map = {}
    for d in docs:
        ln = d.get('locker_no')
        try:
            key = int(str(ln).strip())
            locker_map[key] = d
        except Exception:
            continue

    rows = 9
    cols = 6
    grid = []
    num = 1

    today = datetime.now(timezone.utc).date()

    for r in range(rows):
        row = []
        for c in range(cols):
            doc = locker_map.get(num)

            days_left = None

            if doc:
                ed = doc.get('end_date')

                try:
                    if isinstance(ed, datetime):
                        end_date = ed.date()
                    elif isinstance(ed, str):
                        end_date = datetime.fromisoformat(ed).date()
                    else:
                        end_date = None

                    if end_date:
                        days_left = (end_date - today).days
                except Exception:
                    days_left = None

                entry = {
                    "num": num,
                    "doc": doc,
                    "days_left": days_left
                }
            else:
                entry = {
                    "num": num,
                    "doc": None,
                    "days_left": None
                }

            row.append(entry)
            num += 1

        grid.append(row)

    return render_template("dashboard.html", grid=grid)



@app.route('/payment_history', methods=['GET', 'POST'])
def payment_history():
    payments_list = []
    summary = {
        "count": 0,
        "total_amount": 0,
        "first_date": None,
        "last_date": None
    }

    if request.method == 'POST':
        name = request.form.get('full_name', '').strip()
        membership_id = request.form.get('membership_id', '').strip()
        locker_no = request.form.get('locker_no', '').strip()

        query = {}

        if membership_id:
            query["membership_id"] = {"$regex": f'^{membership_id}$', "$options": "i"}
        if name:
            query["full_name"] = {"$regex": name, "$options": "i"}
        if locker_no:
            query["locker_no"] = {"$regex": f'^{locker_no}$', "$options": "i"}

        payments_list = list(
            payments.find(query).sort("payment_date", 1)
        )

        if payments_list:
            summary["count"] = len(payments_list)
            summary["total_amount"] = sum(p.get("total", 0) for p in payments_list)
            summary["first_date"] = payments_list[0].get("payment_date")
            summary["last_date"] = payments_list[-1].get("payment_date")

    return render_template(
        "payment_history.html",
        payments=payments_list,
        summary=summary
    )




if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

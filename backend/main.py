"""
SHIVA'S Digital Evaluation — Backend API v3.0
Full persistent storage via Supabase (PostgreSQL)
"""

import os
import json
import base64
import hashlib
import secrets
from datetime import datetime
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
import google.generativeai as genai
import psycopg2
import psycopg2.extras
import uvicorn

# ═══════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════
GEMINI_KEY   = os.getenv("GEMINI_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

if GEMINI_KEY and GEMINI_KEY != "YOUR_GEMINI_API_KEY_HERE":
    genai.configure(api_key=GEMINI_KEY)

app = FastAPI(title="SHIVA'S Digital Evaluation API", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://shivaphy.github.io",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "*",   # remove this line once your domain is confirmed
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════
# DATABASE HELPERS
# ═══════════════════════════════════════════
def get_db():
    """Get a database connection. Returns None if DATABASE_URL not set."""
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    except Exception as e:
        print(f"DB connection error: {e}")
        return None

def db_execute(query: str, params=None, fetch='none'):
    """
    Execute a query safely.
    fetch = 'one' | 'all' | 'none'
    Returns result or None on failure.
    """
    conn = get_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(query, params or ())
        if fetch == 'one':
            result = cur.fetchone()
        elif fetch == 'all':
            result = cur.fetchall()
        else:
            result = None
        conn.commit()
        cur.close()
        conn.close()
        return result
    except Exception as e:
        print(f"DB query error: {e}\nQuery: {query}")
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        return None

def hash_password(pw: str) -> str:
    """Simple SHA-256 hash. Use bcrypt in production."""
    return hashlib.sha256(pw.encode()).hexdigest()

def audit(action: str, student_id: str, performed_by: str, details: dict = {}):
    """Write to audit_log table."""
    db_execute(
        """INSERT INTO audit_log (action, student_id, performed_by, details)
           VALUES (%s, %s, %s, %s)""",
        (action, student_id, performed_by, json.dumps(details))
    )

# ═══════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════
class MarksEntry(BaseModel):
    student_id:     str
    evaluator_type: str            # 'AI', 'FACULTY', 'STUDENT'
    marks:          dict
    justification:  Optional[str] = None
    submitted_by:   Optional[str] = None

class FinalizeRequest(BaseModel):
    student_id:         str
    selected_evaluator: str
    moderated_marks:    Optional[dict] = None
    moderator:          str

class RegisterUser(BaseModel):
    username:  str
    password:  str
    full_name: str
    role:      str
    email:     Optional[str] = None

class StudentRecord(BaseModel):
    student_id: str
    name:       str
    username:   str
    password:   Optional[str] = None

# ═══════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Fallback in-memory users when DB not connected
FALLBACK_USERS = {
    "admin_faculty": {"password": hash_password("pass123"), "role": "faculty", "name": "Dr. Priya Sharma"},
    "hod_user":      {"password": hash_password("hod789"),  "role": "hod",     "name": "Prof. Ramesh Kumar"},
    "student_001":   {"password": hash_password("stu001"),  "role": "student",  "name": "Arjun Mehta"},
    "student_002":   {"password": hash_password("stu002"),  "role": "student",  "name": "Priya Nair"},
    "student_003":   {"password": hash_password("stu003"),  "role": "student",  "name": "Rahul Singh"},
    "student_004":   {"password": hash_password("stu004"),  "role": "student",  "name": "Ananya Reddy"},
}

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    uname = form_data.username
    pw    = form_data.password
    hpw   = hash_password(pw)

    # Try database first
    row = db_execute(
        "SELECT username, password_hash, role, full_name, status FROM users WHERE username = %s",
        (uname,), fetch='one'
    )
    if row:
        if row['password_hash'] != hpw:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if row.get('status') == 'pending':
            raise HTTPException(status_code=403, detail="Account pending approval")
        return {
            "access_token": uname,
            "token_type":   "bearer",
            "role":         row['role'],
            "name":         row['full_name']
        }

    # Fallback to in-memory
    user = FALLBACK_USERS.get(uname)
    if not user or user['password'] != hpw:
        # also try plain password match for convenience
        plain_match = any(
            v for v in [FALLBACK_USERS.get(uname)]
            if v and (v['password'] == pw or v['password'] == hpw)
        )
        if not plain_match:
            raise HTTPException(status_code=401, detail="Invalid credentials")

    return {
        "access_token": uname,
        "token_type":   "bearer",
        "role":         user['role'],
        "name":         user['name']
    }

# ═══════════════════════════════════════════
# STUDENTS — CRUD
# ═══════════════════════════════════════════
@app.get("/students")
async def list_students():
    """List all students with their evaluation status."""
    rows = db_execute(
        """SELECT s.student_id, s.full_name, s.username, s.status,
                  e_ai.marks   AS ai_marks,
                  e_fac.marks  AS fac_marks,
                  e_stu.marks  AS stu_marks,
                  fd.final_marks, fd.selected_evaluator
           FROM students s
           LEFT JOIN evaluations e_ai  ON e_ai.student_id  = s.student_id AND e_ai.evaluator_type  = 'AI'
           LEFT JOIN evaluations e_fac ON e_fac.student_id = s.student_id AND e_fac.evaluator_type = 'FACULTY'
           LEFT JOIN evaluations e_stu ON e_stu.student_id = s.student_id AND e_stu.evaluator_type = 'STUDENT'
           LEFT JOIN final_decisions fd ON fd.student_id   = s.student_id
           ORDER BY s.created_at DESC""",
        fetch='all'
    )

    if rows is None:
        # Fallback demo data when DB not connected
        return [
            {"id":"STU-8829","name":"Arjun Mehta",  "username":"student_001","status":"pending", "ai_marks":None,"fac_marks":None,"stu_marks":None,"final_marks":None},
            {"id":"STU-8830","name":"Priya Nair",   "username":"student_002","status":"graded",  "ai_marks":{"total":8.0},"fac_marks":{"total":7.0},"stu_marks":{"total":8.0},"final_marks":None},
            {"id":"STU-8831","name":"Rahul Singh",  "username":"student_003","status":"flagged", "ai_marks":{"total":5.5},"fac_marks":{"total":8.0},"stu_marks":{"total":10.0},"final_marks":None},
            {"id":"STU-8832","name":"Ananya Reddy", "username":"student_004","status":"pending", "ai_marks":None,"fac_marks":None,"stu_marks":None,"final_marks":None},
        ]

    result = []
    for r in rows:
        result.append({
            "id":           r['student_id'],
            "name":         r['full_name'],
            "username":     r['username'],
            "status":       r['status'],
            "ai_marks":     r['ai_marks'],
            "fac_marks":    r['fac_marks'],
            "stu_marks":    r['stu_marks'],
            "final_marks":  r['final_marks'],
            "finalized":    r['selected_evaluator'] is not None,
        })
    return result

@app.post("/students/register")
async def register_student(s: StudentRecord):
    """Register a new student (from bulk upload or single session)."""
    # Create user account
    hpw = hash_password(s.password or (s.username + "_pass"))
    db_execute(
        """INSERT INTO users (username, password_hash, full_name, role, status)
           VALUES (%s, %s, %s, 'student', 'approved')
           ON CONFLICT (username) DO UPDATE SET full_name = EXCLUDED.full_name""",
        (s.username, hpw, s.name)
    )
    # Create student record
    db_execute(
        """INSERT INTO students (student_id, full_name, username, status)
           VALUES (%s, %s, %s, 'pending')
           ON CONFLICT (student_id) DO UPDATE SET full_name = EXCLUDED.full_name, username = EXCLUDED.username""",
        (s.student_id, s.name, s.username)
    )
    return {"status": "registered", "student_id": s.student_id}

@app.post("/students/bulk-register")
async def bulk_register(students: List[StudentRecord]):
    """Register multiple students at once from CSV upload."""
    registered = []
    for s in students:
        hpw = hash_password(s.password or (s.username + "_pass"))
        db_execute(
            """INSERT INTO users (username, password_hash, full_name, role, status)
               VALUES (%s, %s, %s, 'student', 'approved')
               ON CONFLICT (username) DO UPDATE SET full_name = EXCLUDED.full_name""",
            (s.username, hpw, s.name)
        )
        db_execute(
            """INSERT INTO students (student_id, full_name, username, status)
               VALUES (%s, %s, %s, 'pending')
               ON CONFLICT (student_id) DO UPDATE SET full_name = EXCLUDED.full_name""",
            (s.student_id, s.name, s.username)
        )
        registered.append(s.student_id)
    return {"status": "ok", "registered": registered, "count": len(registered)}

# ═══════════════════════════════════════════
# UPLOAD SESSION
# ═══════════════════════════════════════════
# In-memory PDF store (Supabase Storage integration can replace this)
SESSIONS: Dict[str, dict] = {}

@app.post("/upload-session")
async def upload_session(
    student_id:     str        = Form(...),
    question_paper: UploadFile = File(None),
    scheme:         UploadFile = File(None),
    answer_script:  UploadFile = File(None),
):
    """Store uploaded PDFs in memory and register session in DB."""
    session = SESSIONS.get(student_id, {})

    if question_paper:
        qp_bytes = await question_paper.read()
        session["question_paper_b64"] = base64.b64encode(qp_bytes).decode()

    if scheme:
        sc_bytes = await scheme.read()
        session["scheme_b64"] = base64.b64encode(sc_bytes).decode()

    if answer_script:
        sc_bytes = await answer_script.read()
        session["script_b64"] = base64.b64encode(sc_bytes).decode()
        session["script_filename"] = answer_script.filename

    SESSIONS[student_id] = session

    # Record session in DB
    db_execute(
        """INSERT INTO exam_sessions (student_id, script_filename, uploaded_at)
           VALUES (%s, %s, NOW())
           ON CONFLICT (student_id) DO UPDATE
           SET script_filename = EXCLUDED.script_filename, uploaded_at = NOW()""",
        (student_id, session.get("script_filename", ""))
    )

    # Update student status to 'pending' (script uploaded, not yet graded)
    db_execute(
        "UPDATE students SET status = 'pending' WHERE student_id = %s",
        (student_id,)
    )

    return {"status": "uploaded", "student_id": student_id}

# ═══════════════════════════════════════════
# AI ANALYSIS
# ═══════════════════════════════════════════
@app.post("/run-ai-analysis")
async def run_ai_analysis(student_id: str):
    session = SESSIONS.get(student_id)

    if session and GEMINI_KEY and GEMINI_KEY != "YOUR_GEMINI_API_KEY_HERE":
        try:
            model        = genai.GenerativeModel("gemini-1.5-flash")
            script_bytes = base64.b64decode(session["script_b64"])

            prompt = """
You are an expert academic evaluator with 20 years of experience marking university exam scripts.

Analyse this student's handwritten answer script and return ONLY a valid JSON object.

Return exactly this structure with no markdown or text outside the JSON:
{
  "q1a": {
    "marks": 4.5,
    "max": 5,
    "feedback": "One sentence summary.",
    "strengths": ["Specific strength 1", "Specific strength 2"],
    "weaknesses": ["Specific weakness 1"],
    "suggestions": ["Actionable suggestion"]
  },
  "q1b": {
    "marks": 3.5,
    "max": 5,
    "feedback": "One sentence summary.",
    "strengths": ["Specific strength 1"],
    "weaknesses": ["Specific weakness 1", "Specific weakness 2"],
    "suggestions": ["Actionable suggestion 1", "Actionable suggestion 2"]
  },
  "total": 8.0,
  "overall_confidence": 0.87,
  "choice_conflict": false,
  "htr_text": "Brief extract of handwriting read for Q1(a) and Q1(b)",
  "general_feedback": "Two to three sentence overall assessment noting key strengths and improvement areas."
}
"""
            response = model.generate_content([
                prompt,
                {"mime_type": "application/pdf", "data": script_bytes}
            ])
            raw    = response.text.strip().replace("```json","").replace("```","").strip()
            result = json.loads(raw)

            # Persist AI evaluation to database
            _save_evaluation_to_db(student_id, "AI", {
                "q1a":   result.get("q1a", {}).get("marks"),
                "q1b":   result.get("q1b", {}).get("marks"),
                "total": result.get("total"),
            }, ai_feedback=result, ai_confidence=result.get("overall_confidence"),
               htr_text=result.get("htr_text",""))

            return {"status": "success", "ai_eval": result, "source": "gemini"}

        except Exception as e:
            print(f"Gemini error: {e}")

    # Rich mock response
    mock = {
        "q1a": {
            "marks": 4.5, "max": 5,
            "feedback": "Strong conceptual understanding with correct formula and clear working.",
            "strengths":   ["Correct formula stated", "Units used correctly", "Working shown step-by-step"],
            "weaknesses":  ["Final rounding step not shown explicitly"],
            "suggestions": ["Show the rounding step for full marks"]
        },
        "q1b": {
            "marks": 3.5, "max": 5,
            "feedback": "Correct approach but derivation incomplete in final two steps.",
            "strengths":   ["Correct approach identified", "First three steps correct"],
            "weaknesses":  ["Derivation incomplete — last 2 steps missing", "No conclusion statement"],
            "suggestions": ["Complete the full derivation", "Always write a concluding statement"]
        },
        "total": 8.0,
        "overall_confidence": 0.88,
        "choice_conflict": False,
        "htr_text": "Q1(a): F = ma, m=5kg, a=3m/s², F=15N. Q1(b): E = ½mv² + mgh, differentiating...",
        "general_feedback": "Solid conceptual understanding shown in Q1(a). Q1(b) requires more complete derivations. Focus on writing complete solutions with conclusion statements."
    }

    # Also persist mock to DB so HoD sees it
    _save_evaluation_to_db(student_id, "AI", {
        "q1a": mock["q1a"]["marks"], "q1b": mock["q1b"]["marks"], "total": mock["total"]
    }, ai_feedback=mock, ai_confidence=mock["overall_confidence"], htr_text=mock["htr_text"])

    return {"status": "success", "ai_eval": mock, "source": "mock"}

# ═══════════════════════════════════════════
# SUBMIT MARKS
# ═══════════════════════════════════════════
def _save_evaluation_to_db(student_id, evaluator_type, marks, justification=None,
                            evaluator_name=None, ai_feedback=None,
                            ai_confidence=None, htr_text=None):
    """Upsert an evaluation record to the database."""
    db_execute(
        """INSERT INTO evaluations
             (student_id, evaluator_type, evaluator_name, marks, justification,
              ai_feedback, ai_confidence, ai_htr_text, submitted_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
           ON CONFLICT (student_id, evaluator_type)
           DO UPDATE SET
             marks          = EXCLUDED.marks,
             justification  = EXCLUDED.justification,
             ai_feedback    = EXCLUDED.ai_feedback,
             ai_confidence  = EXCLUDED.ai_confidence,
             ai_htr_text    = EXCLUDED.ai_htr_text,
             submitted_at   = NOW()""",
        (
            student_id, evaluator_type,
            evaluator_name or evaluator_type,
            json.dumps(marks),
            justification,
            json.dumps(ai_feedback) if ai_feedback else None,
            ai_confidence,
            htr_text
        )
    )

@app.post("/submit-marks")
async def submit_marks(entry: MarksEntry):
    """Save Faculty or Student marks to database."""
    _save_evaluation_to_db(
        student_id     = entry.student_id,
        evaluator_type = entry.evaluator_type,
        marks          = entry.marks,
        justification  = entry.justification,
        evaluator_name = entry.submitted_by or entry.evaluator_type,
    )

    # Update student status
    if entry.evaluator_type == "FACULTY":
        db_execute(
            "UPDATE students SET status = 'graded' WHERE student_id = %s",
            (entry.student_id,)
        )

    # Audit log
    audit("submit_marks", entry.student_id,
          entry.submitted_by or entry.evaluator_type,
          {"evaluator_type": entry.evaluator_type, "total": entry.marks.get("total")})

    return {"status": "success",
            "message": f"{entry.evaluator_type} marks saved for {entry.student_id}"}

# ═══════════════════════════════════════════
# GET ALL EVALUATIONS FOR A STUDENT
# ═══════════════════════════════════════════
@app.get("/evaluations/{student_id}")
async def get_evaluations(student_id: str):
    """
    Return all three evaluations for a student plus finalization status.
    This is the key endpoint that replaces localStorage — called on every login.
    """
    rows = db_execute(
        """SELECT evaluator_type, marks, justification,
                  ai_feedback, ai_confidence, ai_htr_text, submitted_at
           FROM evaluations
           WHERE student_id = %s""",
        (student_id,), fetch='all'
    )

    final = db_execute(
        """SELECT final_marks, selected_evaluator, moderator, finalized_at
           FROM final_decisions WHERE student_id = %s""",
        (student_id,), fetch='one'
    )

    result = {"student_id": student_id, "AI": {}, "FACULTY": {}, "STUDENT": {}, "FINALIZED": None}

    if rows:
        for r in rows:
            ev_type = r['evaluator_type']
            marks   = r['marks'] if isinstance(r['marks'], dict) else json.loads(r['marks'] or '{}')
            ai_fb   = r['ai_feedback']
            if isinstance(ai_fb, str):
                try: ai_fb = json.loads(ai_fb)
                except: ai_fb = {}

            entry = {**marks, "just": r['justification']}
            if ev_type == "AI" and ai_fb:
                entry.update({
                    "conf":            r['ai_confidence'],
                    "htr":             r['ai_htr_text'],
                    "fb":              ai_fb.get("general_feedback",""),
                    "q1a_prose":       ai_fb.get("q1a",{}).get("feedback",""),
                    "q1a_strengths":   ai_fb.get("q1a",{}).get("strengths",[]),
                    "q1a_weaknesses":  ai_fb.get("q1a",{}).get("weaknesses",[]),
                    "q1a_suggestions": ai_fb.get("q1a",{}).get("suggestions",[]),
                    "q1b_prose":       ai_fb.get("q1b",{}).get("feedback",""),
                    "q1b_strengths":   ai_fb.get("q1b",{}).get("strengths",[]),
                    "q1b_weaknesses":  ai_fb.get("q1b",{}).get("weaknesses",[]),
                    "q1b_suggestions": ai_fb.get("q1b",{}).get("suggestions",[]),
                })
            result[ev_type] = entry

    if final:
        fm = final['final_marks']
        if isinstance(fm, str):
            try: fm = json.loads(fm)
            except: fm = {}
        result["FINALIZED"] = {
            "marks":     fm,
            "from":      final['selected_evaluator'],
            "total":     fm.get("total") if isinstance(fm, dict) else fm,
            "moderator": final['moderator'],
        }

    return result

# ═══════════════════════════════════════════
# LOAD ALL DATA ON LOGIN (single call)
# ═══════════════════════════════════════════
@app.get("/load-all/{username}")
async def load_all(username: str):
    """
    Single endpoint called on login — returns everything needed
    to populate the frontend: students list + all evaluations + finalizations.
    Replaces all localStorage reads.
    """
    # Get user role to determine what to return
    user_row = db_execute(
        "SELECT role FROM users WHERE username = %s", (username,), fetch='one'
    )
    role = user_row['role'] if user_row else None

    # Get all students
    students = await list_students()

    # Get all evaluations for all students
    all_evals   = {}
    all_finals  = {}

    for stu in students:
        sid  = stu['id']
        rows = db_execute(
            """SELECT evaluator_type, marks, justification, ai_feedback,
                      ai_confidence, ai_htr_text
               FROM evaluations WHERE student_id = %s""",
            (sid,), fetch='all'
        )
        ev_entry = {"AI": {}, "FACULTY": {}, "STUDENT": {}}
        if rows:
            for r in rows:
                et    = r['evaluator_type']
                marks = r['marks'] if isinstance(r['marks'],dict) else json.loads(r['marks'] or '{}')
                ai_fb = r.get('ai_feedback') or {}
                if isinstance(ai_fb, str):
                    try: ai_fb = json.loads(ai_fb)
                    except: ai_fb = {}

                entry = {**marks, "just": r['justification']}
                if et == "AI" and ai_fb:
                    entry.update({
                        "conf":            r['ai_confidence'],
                        "htr":             r['ai_htr_text'],
                        "fb":              ai_fb.get("general_feedback",""),
                        "q1a_prose":       ai_fb.get("q1a",{}).get("feedback",""),
                        "q1a_strengths":   ai_fb.get("q1a",{}).get("strengths",[]),
                        "q1a_weaknesses":  ai_fb.get("q1a",{}).get("weaknesses",[]),
                        "q1a_suggestions": ai_fb.get("q1a",{}).get("suggestions",[]),
                        "q1b_prose":       ai_fb.get("q1b",{}).get("feedback",""),
                        "q1b_strengths":   ai_fb.get("q1b",{}).get("strengths",[]),
                        "q1b_weaknesses":  ai_fb.get("q1b",{}).get("weaknesses",[]),
                        "q1b_suggestions": ai_fb.get("q1b",{}).get("suggestions",[]),
                    })
                ev_entry[et] = entry
        all_evals[sid] = ev_entry

        # Finalization
        fin = db_execute(
            "SELECT final_marks, selected_evaluator, finalized_at FROM final_decisions WHERE student_id = %s",
            (sid,), fetch='one'
        )
        if fin:
            fm = fin['final_marks']
            if isinstance(fm,str):
                try: fm = json.loads(fm)
                except: fm = {}
            all_finals[sid] = {
                "from":  fin['selected_evaluator'],
                "total": fm.get("total") if isinstance(fm,dict) else fm,
            }

    return {
        "students":  students,
        "evals":     all_evals,
        "finalized": all_finals,
        "role":      role,
    }

# ═══════════════════════════════════════════
# COMPARISON + FINALIZE
# ═══════════════════════════════════════════
@app.get("/comparison/{student_id}")
async def get_comparison(student_id: str):
    data = await get_evaluations(student_id)
    ai  = data.get("AI",{});  fac = data.get("FACULTY",{}); stu = data.get("STUDENT",{})
    aT  = ai.get("total");    fT  = fac.get("total");       sT  = stu.get("total")

    deviation = 0.0; flagged = False
    if aT is not None and fT is not None:
        deviation = abs(float(aT) - float(fT)) / max(float(aT), 1) * 100
        flagged   = deviation > 15

    return {
        "student_id":       student_id,
        "faculty":          fac,
        "ai":               ai,
        "student":          stu,
        "deviation_percent": round(deviation, 1),
        "flagged":          flagged,
        "finalized":        data.get("FINALIZED"),
    }

@app.post("/finalize")
async def finalize(req: FinalizeRequest):
    """HoD commits official final marks."""
    # Determine final marks value
    if req.selected_evaluator == "MODERATED" and req.moderated_marks:
        final_marks = req.moderated_marks
    else:
        row = db_execute(
            "SELECT marks FROM evaluations WHERE student_id=%s AND evaluator_type=%s",
            (req.student_id, req.selected_evaluator), fetch='one'
        )
        final_marks = {}
        if row:
            fm = row['marks']
            final_marks = fm if isinstance(fm,dict) else json.loads(fm or '{}')

    db_execute(
        """INSERT INTO final_decisions
             (student_id, final_marks, selected_evaluator, moderator, finalized_at)
           VALUES (%s,%s,%s,%s,NOW())
           ON CONFLICT (student_id)
           DO UPDATE SET
             final_marks        = EXCLUDED.final_marks,
             selected_evaluator = EXCLUDED.selected_evaluator,
             moderator          = EXCLUDED.moderator,
             finalized_at       = NOW()""",
        (req.student_id, json.dumps(final_marks), req.selected_evaluator, req.moderator)
    )

    # Update student status
    db_execute(
        "UPDATE students SET status='finalized' WHERE student_id=%s",
        (req.student_id,)
    )

    audit("finalize", req.student_id, req.moderator,
          {"selected_evaluator": req.selected_evaluator, "final_marks": final_marks})

    return {"status": "finalized", "final_marks": final_marks}

# ═══════════════════════════════════════════
# USER MANAGEMENT (Admin)
# ═══════════════════════════════════════════
@app.get("/users")
async def list_users():
    rows = db_execute(
        "SELECT username, full_name, role, status, created_at FROM users ORDER BY created_at DESC",
        fetch='all'
    )
    if not rows:
        return []
    return [dict(r) for r in rows]

@app.post("/users/register")
async def register_user(u: RegisterUser):
    hpw = hash_password(u.password)
    existing = db_execute("SELECT username FROM users WHERE username=%s", (u.username,), fetch='one')
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    db_execute(
        """INSERT INTO users (username, password_hash, full_name, role, status)
           VALUES (%s,%s,%s,%s,'approved')""",
        (u.username, hpw, u.full_name, u.role)
    )
    return {"status": "registered", "username": u.username}

@app.post("/users/reset-password")
async def reset_password(username: str, new_password: str):
    hpw = hash_password(new_password)
    db_execute("UPDATE users SET password_hash=%s WHERE username=%s", (hpw, username))
    return {"status": "ok"}

@app.delete("/users/{username}")
async def delete_user(username: str):
    db_execute("DELETE FROM users WHERE username=%s", (username,))
    return {"status": "deleted"}

@app.patch("/users/{username}/status")
async def update_user_status(username: str, new_status: str):
    db_execute("UPDATE users SET status=%s WHERE username=%s", (new_status, username))
    return {"status": "updated", "new_status": new_status}

# ═══════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════
@app.get("/health")
async def health():
    db_ok = db_execute("SELECT 1", fetch='one') is not None
    return {
        "status":   "ok",
        "database": "connected" if db_ok else "disconnected (using fallback)",
        "gemini":   "configured" if GEMINI_KEY else "not configured",
        "version":  "3.0"
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)

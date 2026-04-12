"""
SHIVA'S Digital Evaluation — Backend API v4.0
Priority 1: Full Supabase persistent storage (users, students, evals, finals)
Priority 2: PDF file storage in Supabase (no S3 needed)
"""

import os, json, base64, hashlib
from typing import Optional, List, Dict
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import Response
from pydantic import BaseModel
import google.generativeai as genai
import psycopg2, psycopg2.extras, uvicorn

# ═══════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════
GEMINI_KEY   = os.getenv("GEMINI_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL",   "")
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

app = FastAPI(title="SHIVA's Digital Evaluation API", version="4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════
# DATABASE HELPERS
# ═══════════════════════════════════════
def get_db():
    if not DATABASE_URL:
        return None
    try:
        return psycopg2.connect(DATABASE_URL,
                                cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as e:
        print(f"DB connect error: {e}")
        return None

def db_exec(sql, params=None, fetch="none"):
    conn = get_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        if   fetch == "one": res = cur.fetchone()
        elif fetch == "all": res = cur.fetchall()
        else:                res = None
        conn.commit(); cur.close(); conn.close()
        return res
    except Exception as e:
        print(f"DB error: {e}\nSQL: {sql[:120]}")
        try: conn.rollback(); conn.close()
        except: pass
        return None

def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def audit(action, student_id, by, details={}):
    db_exec(
        "INSERT INTO audit_log(action,student_id,performed_by,details) VALUES(%s,%s,%s,%s)",
        (action, student_id or "", by, json.dumps(details))
    )

# ═══════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════
@app.get("/")
@app.get("/health")
async def health():
    db_ok = db_exec("SELECT 1", fetch="one") is not None
    return {
        "status":   "ok",
        "database": "connected" if db_ok else "offline (fallback active)",
        "gemini":   "configured" if GEMINI_KEY else "not configured (mock mode)",
        "version":  "4.0"
    }

# ═══════════════════════════════════════
# MODELS
# ═══════════════════════════════════════
class MarksEntry(BaseModel):
    student_id:     str
    evaluator_type: str
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

# ═══════════════════════════════════════
# FALLBACK USERS (when DB offline)
# ═══════════════════════════════════════
FALLBACK_USERS = {
    "admin_faculty": {"pw": hash_pw("pass123"), "role": "faculty", "name": "Dr. Priya Sharma"},
    "hod_user":      {"pw": hash_pw("hod789"),  "role": "hod",     "name": "Prof. Ramesh Kumar"},
    "student_001":   {"pw": hash_pw("pass123"), "role": "student",  "name": "Arjun Mehta"},
    "student_002":   {"pw": hash_pw("pass123"), "role": "student",  "name": "Priya Nair"},
    "student_003":   {"pw": hash_pw("pass123"), "role": "student",  "name": "Rahul Singh"},
    "student_004":   {"pw": hash_pw("pass123"), "role": "student",  "name": "Ananya Reddy"},
}

# ═══════════════════════════════════════
# AUTH
# ═══════════════════════════════════════
@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    uname = form_data.username
    hpw   = hash_pw(form_data.password)
    row   = db_exec(
        "SELECT username,password_hash,role,full_name,status FROM users WHERE username=%s",
        (uname,), fetch="one"
    )
    if row:
        if row["password_hash"] != hpw:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if row.get("status") == "pending":
            raise HTTPException(status_code=403, detail="Account pending approval")
        return {"access_token": uname, "token_type": "bearer",
                "role": row["role"], "name": row["full_name"]}
    u = FALLBACK_USERS.get(uname)
    if not u or u["pw"] != hpw:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"access_token": uname, "token_type": "bearer",
            "role": u["role"], "name": u["name"]}

# ═══════════════════════════════════════
# USERS — FULL CRUD
# ═══════════════════════════════════════
@app.get("/users")
async def list_users():
    """Return ALL users with full details for admin eagle-eye view."""
    rows = db_exec(
        """SELECT username, full_name, role, status, email,
                  created_at,
                  (SELECT COUNT(*) FROM students WHERE username=u.username) AS student_count,
                  (SELECT COUNT(*) FROM evaluations WHERE evaluator_name=u.username) AS eval_count
           FROM users u
           ORDER BY role, created_at DESC""",
        fetch="all"
    )
    if not rows:
        return []
    return [dict(r) for r in rows]

@app.get("/users/stats")
async def user_stats():
    """Admin dashboard stats — counts by role and status."""
    rows = db_exec(
        "SELECT role, status, COUNT(*) AS cnt FROM users GROUP BY role, status",
        fetch="all"
    )
    stats = {"faculty": {"total":0,"approved":0,"pending":0},
             "hod":     {"total":0,"approved":0,"pending":0},
             "student": {"total":0,"approved":0,"pending":0},
             "total_users": 0}
    if rows:
        for r in rows:
            role   = r["role"]
            status = r["status"]
            cnt    = r["cnt"]
            if role in stats:
                stats[role]["total"]   += cnt
                stats[role][status]    = stats[role].get(status,0) + cnt
            stats["total_users"] += cnt
    return stats

@app.post("/users/register")
async def register_user(u: RegisterUser):
    existing = db_exec("SELECT username FROM users WHERE username=%s",(u.username,),fetch="one")
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    db_exec(
        "INSERT INTO users(username,password_hash,full_name,role,email,status) VALUES(%s,%s,%s,%s,%s,'approved')",
        (u.username, hash_pw(u.password), u.full_name, u.role, u.email or "")
    )
    audit("user_registered", None, u.username, {"role": u.role, "name": u.full_name})
    return {"status": "registered", "username": u.username}

@app.post("/users/reset-password")
async def reset_password(username: str = Query(...), new_password: str = Query(...)):
    db_exec("UPDATE users SET password_hash=%s WHERE username=%s",
            (hash_pw(new_password), username))
    audit("password_reset", None, username, {})
    return {"status": "ok"}

@app.delete("/users/{username}")
async def delete_user(username: str):
    db_exec("DELETE FROM users WHERE username=%s", (username,))
    audit("user_deleted", None, "admin", {"username": username})
    return {"status": "deleted"}

@app.patch("/users/{username}/status")
async def update_status(username: str, new_status: str = Query(...)):
    db_exec("UPDATE users SET status=%s WHERE username=%s", (new_status, username))
    audit(f"user_status_{new_status}", None, "admin", {"username": username})
    return {"status": "updated", "new_status": new_status}

@app.get("/users/pending")
async def list_pending():
    """All users with status=pending — for admin approvals tab."""
    rows = db_exec(
        "SELECT username,full_name,role,email,created_at FROM users WHERE status='pending' ORDER BY created_at",
        fetch="all"
    )
    return [dict(r) for r in rows] if rows else []

# ═══════════════════════════════════════
# STUDENTS
# ═══════════════════════════════════════
@app.get("/students")
async def list_students():
    rows = db_exec(
        """SELECT s.student_id, s.full_name, s.username, s.status,
                  e_ai.marks  AS ai_marks,
                  e_fc.marks  AS fac_marks,
                  e_st.marks  AS stu_marks,
                  fd.final_marks, fd.selected_evaluator,
                  es.script_filename,
                  (es.script_b64 IS NOT NULL AND es.script_b64 != '') AS has_script
           FROM students s
           LEFT JOIN evaluations e_ai ON e_ai.student_id=s.student_id AND e_ai.evaluator_type='AI'
           LEFT JOIN evaluations e_fc ON e_fc.student_id=s.student_id AND e_fc.evaluator_type='FACULTY'
           LEFT JOIN evaluations e_st ON e_st.student_id=s.student_id AND e_st.evaluator_type='STUDENT'
           LEFT JOIN final_decisions fd ON fd.student_id=s.student_id
           LEFT JOIN exam_sessions es  ON es.student_id=s.student_id
           ORDER BY s.created_at DESC""",
        fetch="all"
    )
    if rows is None:
        return [
            {"id":"STU-8829","name":"Arjun Mehta", "username":"student_001","status":"pending","has_script":False},
            {"id":"STU-8830","name":"Priya Nair",  "username":"student_002","status":"graded", "has_script":False},
        ]
    return [{
        "id":         r["student_id"],
        "name":       r["full_name"],
        "username":   r["username"],
        "status":     r["status"],
        "finalized":  r["selected_evaluator"] is not None,
        "ai_marks":   r["ai_marks"],
        "fac_marks":  r["fac_marks"],
        "stu_marks":  r["stu_marks"],
        "final_marks":r["final_marks"],
        "script_filename": r["script_filename"],
        "has_script": bool(r["has_script"]),
    } for r in rows]

@app.post("/students/register")
async def register_student(s: StudentRecord):
    hpw = hash_pw(s.password or (s.username + "_pass"))
    db_exec(
        "INSERT INTO users(username,password_hash,full_name,role,status) VALUES(%s,%s,%s,'student','approved') "
        "ON CONFLICT(username) DO UPDATE SET full_name=EXCLUDED.full_name",
        (s.username, hpw, s.name)
    )
    db_exec(
        "INSERT INTO students(student_id,full_name,username,status) VALUES(%s,%s,%s,'pending') "
        "ON CONFLICT(student_id) DO UPDATE SET full_name=EXCLUDED.full_name,username=EXCLUDED.username",
        (s.student_id, s.name, s.username)
    )
    return {"status": "registered", "student_id": s.student_id}

@app.post("/students/bulk-register")
async def bulk_register(students: List[StudentRecord]):
    for s in students:
        hpw = hash_pw(s.password or (s.username + "_pass"))
        db_exec(
            "INSERT INTO users(username,password_hash,full_name,role,status) VALUES(%s,%s,%s,'student','approved') "
            "ON CONFLICT(username) DO UPDATE SET full_name=EXCLUDED.full_name",
            (s.username, hpw, s.name)
        )
        db_exec(
            "INSERT INTO students(student_id,full_name,username,status) VALUES(%s,%s,%s,'pending') "
            "ON CONFLICT(student_id) DO UPDATE SET full_name=EXCLUDED.full_name",
            (s.student_id, s.name, s.username)
        )
    return {"status": "ok", "count": len(students)}

@app.delete("/students/{student_id}")
async def delete_student(student_id: str):
    """Delete a student and all their evaluation data."""
    # Get username to also remove user account
    row = db_exec("SELECT username FROM students WHERE student_id=%s", (student_id,), fetch="one")
    db_exec("DELETE FROM evaluations    WHERE student_id=%s", (student_id,))
    db_exec("DELETE FROM final_decisions WHERE student_id=%s", (student_id,))
    db_exec("DELETE FROM exam_sessions   WHERE student_id=%s", (student_id,))
    db_exec("DELETE FROM audit_log       WHERE student_id=%s", (student_id,))
    db_exec("DELETE FROM students        WHERE student_id=%s", (student_id,))
    if row and row.get("username"):
        db_exec("DELETE FROM users WHERE username=%s AND role='student'", (row["username"],))
    audit("student_deleted", student_id, "faculty", {"student_id": student_id})
    return {"status": "deleted", "student_id": student_id}

# ═══════════════════════════════════════
# PDF FILE STORAGE — Priority 2
# PDFs stored as base64 in exam_sessions table.
# No S3/external storage needed — uses existing Supabase DB.
# ═══════════════════════════════════════
@app.post("/upload-session")
async def upload_session(
    student_id:     str        = Form(...),
    question_paper: UploadFile = File(None),
    scheme:         UploadFile = File(None),
    answer_script:  UploadFile = File(None),
):
    """
    Upload PDFs. Stored permanently in Supabase exam_sessions table.
    Faculty can re-open any student's script at any time without re-uploading.
    """
    # Read existing session from DB first
    existing = db_exec(
        "SELECT qp_b64, scheme_b64, script_b64, script_filename FROM exam_sessions WHERE student_id=%s",
        (student_id,), fetch="one"
    )
    qp_b64       = existing["qp_b64"]     if existing else None
    scheme_b64   = existing["scheme_b64"] if existing else None
    script_b64   = existing["script_b64"] if existing else None
    script_fname = existing["script_filename"] if existing else ""

    # Override with newly uploaded files
    if question_paper and question_paper.filename:
        qp_b64 = base64.b64encode(await question_paper.read()).decode()
    if scheme and scheme.filename:
        scheme_b64 = base64.b64encode(await scheme.read()).decode()
    if answer_script and answer_script.filename:
        script_b64   = base64.b64encode(await answer_script.read()).decode()
        script_fname = answer_script.filename

    # Upsert into exam_sessions — all three PDFs stored permanently
    db_exec(
        """INSERT INTO exam_sessions(student_id, qp_b64, scheme_b64, script_b64, script_filename, uploaded_at)
           VALUES(%s,%s,%s,%s,%s,NOW())
           ON CONFLICT(student_id) DO UPDATE SET
             qp_b64          = COALESCE(EXCLUDED.qp_b64,     exam_sessions.qp_b64),
             scheme_b64      = COALESCE(EXCLUDED.scheme_b64, exam_sessions.scheme_b64),
             script_b64      = COALESCE(EXCLUDED.script_b64, exam_sessions.script_b64),
             script_filename = COALESCE(EXCLUDED.script_filename, exam_sessions.script_filename),
             uploaded_at     = NOW()""",
        (student_id, qp_b64, scheme_b64, script_b64, script_fname)
    )
    db_exec("UPDATE students SET status='pending' WHERE student_id=%s", (student_id,))
    return {"status": "uploaded", "student_id": student_id,
            "has_script": bool(script_b64),
            "has_scheme": bool(scheme_b64),
            "has_qp":     bool(qp_b64)}

@app.get("/pdf/{student_id}/{doc_type}")
async def get_pdf(student_id: str, doc_type: str):
    """
    Serve a stored PDF back to the browser as a blob URL.
    doc_type: 'script' | 'scheme' | 'qp'
    """
    col_map = {"script": "script_b64", "scheme": "scheme_b64", "qp": "qp_b64"}
    col = col_map.get(doc_type)
    if not col:
        raise HTTPException(status_code=400, detail="Invalid doc_type. Use script|scheme|qp")

    row = db_exec(f"SELECT {col} FROM exam_sessions WHERE student_id=%s",
                  (student_id,), fetch="one")
    if not row or not row[col]:
        raise HTTPException(status_code=404, detail=f"No {doc_type} found for {student_id}")

    pdf_bytes = base64.b64decode(row[col])
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{doc_type}_{student_id}.pdf"',
                 "Cache-Control": "public, max-age=3600"}
    )

@app.get("/pdf/check/{student_id}")
async def check_pdfs(student_id: str):
    """Check which PDFs exist for a student without downloading them."""
    row = db_exec(
        """SELECT
             (qp_b64 IS NOT NULL AND qp_b64 != '')     AS has_qp,
             (scheme_b64 IS NOT NULL AND scheme_b64 != '') AS has_scheme,
             (script_b64 IS NOT NULL AND script_b64 != '') AS has_script,
             script_filename
           FROM exam_sessions WHERE student_id=%s""",
        (student_id,), fetch="one"
    )
    if not row:
        return {"has_qp": False, "has_scheme": False, "has_script": False}
    return dict(row)

# ═══════════════════════════════════════
# AI ANALYSIS
# ═══════════════════════════════════════
SESSIONS: Dict[str, dict] = {}        # in-memory PDF cache per session
ACTIVE_SCHEME: dict = {}              # structured scheme set by faculty via /set-scheme

def _build_structured_prompt(scheme: dict) -> str:
    """
    Build a precise, machine-readable prompt from the structured scheme JSON.
    This is far more reliable than asking Gemini to read a scheme PDF visually.
    """
    lines = []
    lines.append("You are an expert academic examiner evaluating a HANDWRITTEN ANSWER SCRIPT.")
    lines.append("")
    lines.append(f"EXAM: {scheme.get('exam_title','Unknown')}  |  "
                 f"SUBJECT: {scheme.get('subject','')}  |  "
                 f"TOTAL MARKS: {scheme.get('total_marks',0)}")
    lines.append("")
    lines.append("═" * 60)
    lines.append("OFFICIAL MARKING SCHEME — follow this EXACTLY:")
    lines.append("═" * 60)

    for q in scheme.get("questions", []):
        qnum  = q.get("num","Q?")
        qtype = q.get("type","standard")
        parts = q.get("parts",[])
        qtotal = sum(float(p.get("marks",0)) for p in parts)
        lines.append(f"\n{qnum}  [{qtotal} marks total]"
                     + ("  ← OR CHOICE (evaluate whichever part the student attempted, NOT both)" if qtype=="or" else ""))
        for p in parts:
            key      = p.get("key","")
            label    = p.get("label","")
            marks    = p.get("marks",0)
            desc     = p.get("description","")
            keywords = p.get("keywords","")
            co       = p.get("co","")
            bloom    = p.get("bloom","")
            lines.append(f"  {label} [{marks} marks] — CO:{co} | Bloom:{bloom}")
            lines.append(f"      EXPECTED: {desc}")
            if keywords:
                kws = [k.strip() for k in keywords.split(",") if k.strip()]
                lines.append(f"      AWARD 1 mark per keyword present: {', '.join(kws)}")
                lines.append(f"      Award partial marks for correct method even if final answer wrong.")

    lines.append("\n" + "═" * 60)
    lines.append("EVALUATION RULES:")
    lines.append("1. READ the handwriting carefully (HTR — Handwriting Text Recognition).")
    lines.append("2. For each attempted sub-question, check against the scheme above.")
    lines.append("3. Award marks based on keywords present and correctness of method.")
    lines.append("4. For OR questions: evaluate ONLY the part attempted. If student answered BOTH, set choice_conflict=true.")
    lines.append("5. Do NOT invent questions not in the scheme above.")
    lines.append("6. Award partial credit generously where working is shown.")
    lines.append("")
    lines.append("RETURN ONLY valid JSON with NO markdown fences, NO text before or after:")

    # Build expected JSON structure from scheme
    q_example = {}
    for q in scheme.get("questions",[]):
        for p in q.get("parts",[]):
            key = p.get("key","q1a")
            q_example[key] = {
                "marks": f"<float 0 to {p.get('marks',5)}>",
                "max":   p.get("marks", 5),
                "feedback":    "<one sentence>",
                "strengths":   ["<str>"],
                "weaknesses":  ["<str>"],
                "suggestions": ["<str>"]
            }

    import json as _json
    result_template = {
        "questions":          q_example,
        "total":              f"<sum of awarded marks>",
        "max_total":          scheme.get("total_marks", 10),
        "overall_confidence": "<float 0.0-1.0>",
        "choice_conflict":    False,
        "htr_text":           "<key handwritten content extracted>",
        "general_feedback":   "<2-3 sentence overall assessment>"
    }
    lines.append(_json.dumps(result_template, indent=2))
    return "\n".join(lines)

def _build_pdf_fallback_prompt() -> str:
    """Used when no structured scheme is available — reads scheme from PDF."""
    return """You are an expert academic examiner. You have been given:
1. A SCHEME OF EVALUATION PDF (if uploaded) — read it for question structure and max marks.
2. A HANDWRITTEN ANSWER SCRIPT PDF.

TASK:
- Read the handwriting carefully (HTR).
- Identify every attempted sub-question.
- Evaluate strictly against the scheme.
- Award marks per sub-question with correct max values from the scheme.
- Detect OR-choice conflicts.

RETURN ONLY valid JSON (no markdown):
{
  "questions": {
    "q1a": {"marks": 4.5, "max": 5, "feedback": "sentence",
            "strengths": ["s1"], "weaknesses": ["w1"], "suggestions": ["sg1"]},
    "q1b": {"marks": 3, "max": 5, "feedback": "sentence",
            "strengths": ["s1"], "weaknesses": ["w1"], "suggestions": ["sg1"]}
  },
  "total": 7.5, "max_total": 10, "overall_confidence": 0.87,
  "choice_conflict": false,
  "htr_text": "key handwritten text extracted",
  "general_feedback": "2-3 sentence assessment"
}"""

# ── Store structured scheme ──────────────────────────────
@app.post("/set-scheme")
async def set_scheme(scheme: dict):
    """Faculty posts structured scheme JSON. Used by AI for precise evaluation."""
    global ACTIVE_SCHEME
    ACTIVE_SCHEME = scheme
    print(f"Scheme activated: {scheme.get('exam_title')} ({scheme.get('total_marks')} marks, "
          f"{len(scheme.get('questions',[]))} questions)")
    return {"status": "ok", "questions": len(scheme.get("questions", []))}

@app.get("/get-scheme")
async def get_scheme():
    return ACTIVE_SCHEME if ACTIVE_SCHEME else {"status": "none"}

@app.post("/run-ai-analysis")
async def run_ai_analysis(student_id: str):
    # Load PDFs — try in-memory cache first, then DB
    session = SESSIONS.get(student_id)
    if not session or not session.get("script_b64"):
        row = db_exec(
            "SELECT qp_b64, scheme_b64, script_b64 FROM exam_sessions WHERE student_id=%s",
            (student_id,), fetch="one"
        )
        if row:
            session = dict(row)
            SESSIONS[student_id] = session

    if session and session.get("script_b64") and GEMINI_KEY:
        try:
            model        = genai.GenerativeModel("gemini-1.5-flash")
            script_bytes = base64.b64decode(session["script_b64"])
            parts        = []

            # Always include the answer script
            parts.append({"mime_type": "application/pdf", "data": script_bytes})

            # Include scheme PDF only if NO structured scheme is active
            # (structured prompt is more reliable than visual PDF reading)
            use_structured = bool(ACTIVE_SCHEME and ACTIVE_SCHEME.get("questions"))

            if not use_structured:
                if session.get("scheme_b64"):
                    parts.insert(0, {"mime_type": "application/pdf",
                                     "data": base64.b64decode(session["scheme_b64"])})
                if session.get("qp_b64"):
                    parts.insert(0, {"mime_type": "application/pdf",
                                     "data": base64.b64decode(session["qp_b64"])})

            # Build prompt
            if use_structured:
                prompt = _build_structured_prompt(ACTIVE_SCHEME)
                print(f"Using STRUCTURED scheme: {ACTIVE_SCHEME.get('exam_title')}")
            else:
                prompt = _build_pdf_fallback_prompt()
                print("Using PDF-fallback prompt (no structured scheme active)")

            parts.insert(0, prompt)

            response = model.generate_content(parts)
            raw      = response.text.strip()
            # Strip markdown fences if present
            raw = raw.replace("```json","").replace("```","").strip()
            # Sometimes Gemini prepends a sentence — find the first {
            brace = raw.find("{")
            if brace > 0: raw = raw[brace:]
            result = json.loads(raw)
            result = _normalise_ai_result(result, ACTIVE_SCHEME)

            _save_eval(student_id, "AI", _marks_from_result(result),
                       ai_feedback=result,
                       ai_confidence=result.get("overall_confidence"),
                       htr_text=result.get("htr_text",""))
            return {"status":"success","ai_eval":result,"source":"gemini",
                    "scheme_used": "structured" if use_structured else "pdf"}

        except Exception as e:
            print(f"Gemini error: {e}")
            import traceback; traceback.print_exc()

    # Mock fallback — uses active scheme structure if available
    if ACTIVE_SCHEME and ACTIVE_SCHEME.get("questions"):
        mock_qs = {}
        total   = 0.0
        max_t   = float(ACTIVE_SCHEME.get("total_marks", 10))
        for q in ACTIVE_SCHEME["questions"]:
            for p in q.get("parts",[]):
                key    = p.get("key","q1a")
                maxM   = float(p.get("marks",5))
                awarded= round(maxM * 0.80, 1)   # 80% mock score
                total += awarded
                mock_qs[key] = {
                    "marks": awarded, "max": maxM,
                    "feedback":    f"Adequate response for {p.get('label','?')} — key concepts present.",
                    "strengths":   ["Correct approach identified","Working shown"],
                    "weaknesses":  ["Some steps missing","Conclusion not stated"],
                    "suggestions": ["Complete all derivation steps","State final answer clearly"]
                }
        mock = {
            "questions": mock_qs,
            "total": round(total, 1),
            "max_total": max_t,
            "overall_confidence": 0.85,
            "choice_conflict": False,
            "htr_text": "Mock evaluation — Gemini API not available or script not uploaded.",
            "general_feedback": "Mock result (backend AI unavailable). Marks awarded at 80% of maximum per question."
        }
    else:
        mock = {
            "questions": {
                "q1a": {"marks":4.5,"max":5,"feedback":"Strong conceptual understanding.",
                        "strengths":["Correct formula","Units correct","Working shown"],
                        "weaknesses":["Rounding not shown"],"suggestions":["Show rounding step"]},
                "q1b": {"marks":3.5,"max":5,"feedback":"Correct approach, derivation incomplete.",
                        "strengths":["Method correct","3 steps correct"],
                        "weaknesses":["Last 2 steps missing","No conclusion"],
                        "suggestions":["Complete derivation","Write conclusion"]}
            },
            "total":8.0,"max_total":10,"overall_confidence":0.88,"choice_conflict":False,
            "htr_text":"Q1(a): F=ma, m=5kg, a=3m/s², F=15N. Q1(b): E=½mv²+mgh...",
            "general_feedback":"Solid understanding. Q1(b) needs complete derivations."
        }

    _save_eval(student_id, "AI", _marks_from_result(mock),
               ai_feedback=mock, ai_confidence=mock["overall_confidence"],
               htr_text=mock["htr_text"])
    return {"status":"success","ai_eval":mock,"source":"mock"}

def _normalise_ai_result(result: dict, scheme: dict = None) -> dict:
    """Ensure result has a 'questions' dict. Fill in max marks from scheme if missing."""
    if "questions" not in result:
        questions = {}
        for key in ["q1a","q1b","q1c","q2a","q2b","q2c","q3a","q3b","q3c"]:
            if key in result:
                v = result[key]
                questions[key] = v if isinstance(v, dict) else {"marks": v, "max": 5}
        result["questions"] = questions

    # Fill in max values from scheme if Gemini didn't include them
    if scheme and scheme.get("questions"):
        scheme_maxes = {}
        for q in scheme["questions"]:
            for p in q.get("parts",[]):
                scheme_maxes[p.get("key","")] = float(p.get("marks",5))
        for key, qdata in result["questions"].items():
            if isinstance(qdata, dict) and key in scheme_maxes:
                if not qdata.get("max") or qdata["max"] == 5:
                    qdata["max"] = scheme_maxes[key]

    return result

def _marks_from_result(result: dict) -> dict:
    marks = {}
    for qkey, qdata in result.get("questions",{}).items():
        if isinstance(qdata, dict): marks[qkey] = qdata.get("marks", 0)
    marks["total"]     = result.get("total", sum(v.get("marks",0) for v in result.get("questions",{}).values() if isinstance(v,dict)))
    marks["max_total"] = result.get("max_total", 10)
    return marks

# ═══════════════════════════════════════
# SUBMIT MARKS
# ═══════════════════════════════════════
def _save_eval(student_id, evaluator_type, marks,
               justification=None, evaluator_name=None,
               ai_feedback=None, ai_confidence=None, htr_text=None):
    db_exec(
        """INSERT INTO evaluations
             (student_id,evaluator_type,evaluator_name,marks,justification,
              ai_feedback,ai_confidence,ai_htr_text,submitted_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NOW())
           ON CONFLICT(student_id,evaluator_type) DO UPDATE SET
             marks=EXCLUDED.marks, justification=EXCLUDED.justification,
             ai_feedback=EXCLUDED.ai_feedback, ai_confidence=EXCLUDED.ai_confidence,
             ai_htr_text=EXCLUDED.ai_htr_text, submitted_at=NOW()""",
        (student_id, evaluator_type, evaluator_name or evaluator_type,
         json.dumps(marks), justification,
         json.dumps(ai_feedback) if ai_feedback else None,
         ai_confidence, htr_text)
    )

@app.post("/submit-marks")
async def submit_marks(entry: MarksEntry):
    _save_eval(entry.student_id, entry.evaluator_type, entry.marks,
               entry.justification, entry.submitted_by)
    if entry.evaluator_type == "FACULTY":
        db_exec("UPDATE students SET status='graded' WHERE student_id=%s", (entry.student_id,))
    audit("submit_marks", entry.student_id,
          entry.submitted_by or entry.evaluator_type,
          {"evaluator_type": entry.evaluator_type, "total": entry.marks.get("total")})
    return {"status": "success"}

# ═══════════════════════════════════════
# EVALUATIONS
# ═══════════════════════════════════════
def _row_to_eval(r) -> dict:
    marks = r["marks"] if isinstance(r["marks"],dict) else json.loads(r["marks"] or "{}")
    ai_fb = r.get("ai_feedback") or {}
    if isinstance(ai_fb, str):
        try: ai_fb = json.loads(ai_fb)
        except: ai_fb = {}
    entry = {**marks, "just": r["justification"]}
    et    = r["evaluator_type"]
    if et == "AI" and ai_fb:
        qs = ai_fb.get("questions", {})
        entry.update({
            "conf":  r.get("ai_confidence"),
            "htr":   r.get("ai_htr_text",""),
            "fb":    ai_fb.get("general_feedback",""),
            "questions": qs,
            # Legacy flat keys
            "q1a":             qs.get("q1a",{}).get("marks") if isinstance(qs.get("q1a"),dict) else qs.get("q1a"),
            "q1b":             qs.get("q1b",{}).get("marks") if isinstance(qs.get("q1b"),dict) else qs.get("q1b"),
            "q1a_prose":       qs.get("q1a",{}).get("feedback","")   if isinstance(qs.get("q1a"),dict) else "",
            "q1a_strengths":   qs.get("q1a",{}).get("strengths",[])  if isinstance(qs.get("q1a"),dict) else [],
            "q1a_weaknesses":  qs.get("q1a",{}).get("weaknesses",[]) if isinstance(qs.get("q1a"),dict) else [],
            "q1a_suggestions": qs.get("q1a",{}).get("suggestions",[])if isinstance(qs.get("q1a"),dict) else [],
            "q1b_prose":       qs.get("q1b",{}).get("feedback","")   if isinstance(qs.get("q1b"),dict) else "",
            "q1b_strengths":   qs.get("q1b",{}).get("strengths",[])  if isinstance(qs.get("q1b"),dict) else [],
            "q1b_weaknesses":  qs.get("q1b",{}).get("weaknesses",[]) if isinstance(qs.get("q1b"),dict) else [],
            "q1b_suggestions": qs.get("q1b",{}).get("suggestions",[])if isinstance(qs.get("q1b"),dict) else [],
        })
    return entry

@app.get("/evaluations/{student_id}")
async def get_evaluations(student_id: str):
    rows  = db_exec(
        "SELECT evaluator_type,marks,justification,ai_feedback,ai_confidence,ai_htr_text "
        "FROM evaluations WHERE student_id=%s", (student_id,), fetch="all"
    )
    final = db_exec(
        "SELECT final_marks,selected_evaluator,moderator FROM final_decisions WHERE student_id=%s",
        (student_id,), fetch="one"
    )
    res = {"student_id":student_id,"AI":{},"FACULTY":{},"STUDENT":{},"FINALIZED":None}
    if rows:
        for r in rows: res[r["evaluator_type"]] = _row_to_eval(r)
    if final:
        fm = final["final_marks"]
        if isinstance(fm,str):
            try: fm = json.loads(fm)
            except: fm = {}
        res["FINALIZED"] = {"marks":fm,"from":final["selected_evaluator"],
                            "total":fm.get("total") if isinstance(fm,dict) else fm,
                            "moderator":final["moderator"]}
    return res

# ═══════════════════════════════════════
# LOAD ALL — single call on login
# Returns students, evals, finals, users (for admin), pdf availability
# ═══════════════════════════════════════
@app.get("/load-all/{username}")
async def load_all(username: str):
    students_list = await list_students()
    all_evals     = {}
    all_finals    = {}

    for stu in students_list:
        sid  = stu["id"]
        rows = db_exec(
            "SELECT evaluator_type,marks,justification,ai_feedback,ai_confidence,ai_htr_text "
            "FROM evaluations WHERE student_id=%s", (sid,), fetch="all"
        )
        ev = {"AI":{},"FACULTY":{},"STUDENT":{}}
        if rows:
            for r in rows: ev[r["evaluator_type"]] = _row_to_eval(r)
        all_evals[sid] = ev

        fin = db_exec(
            "SELECT final_marks,selected_evaluator FROM final_decisions WHERE student_id=%s",
            (sid,), fetch="one"
        )
        if fin:
            fm = fin["final_marks"]
            if isinstance(fm,str):
                try: fm = json.loads(fm)
                except: fm = {}
            all_finals[sid] = {"from":fin["selected_evaluator"],
                               "total":fm.get("total") if isinstance(fm,dict) else fm}

    # Always include full user list so admin portal stays current
    all_users    = await list_users()
    pending      = await list_pending()
    stats        = await user_stats()

    return {
        "students":      students_list,
        "evals":         all_evals,
        "finalized":     all_finals,
        "users":         all_users,
        "pending_users": pending,
        "user_stats":    stats,
    }

# ═══════════════════════════════════════
# COMPARISON + FINALIZE
# ═══════════════════════════════════════
@app.get("/comparison/{student_id}")
async def get_comparison(student_id: str):
    data = await get_evaluations(student_id)
    ai   = data.get("AI",{}); fac = data.get("FACULTY",{})
    aT   = ai.get("total");   fT  = fac.get("total")
    dev  = 0.0; flagged = False
    if aT is not None and fT is not None:
        dev     = abs(float(aT)-float(fT)) / max(float(aT),1) * 100
        flagged = dev > 15
    return {"student_id":student_id,"faculty":fac,"ai":ai,"student":data.get("STUDENT",{}),
            "deviation_percent":round(dev,1),"flagged":flagged,"finalized":data.get("FINALIZED")}

@app.post("/finalize")
async def finalize(req: FinalizeRequest):
    if req.selected_evaluator == "MODERATED" and req.moderated_marks:
        final_marks = req.moderated_marks
    else:
        row = db_exec("SELECT marks FROM evaluations WHERE student_id=%s AND evaluator_type=%s",
                      (req.student_id, req.selected_evaluator), fetch="one")
        final_marks = {}
        if row:
            fm = row["marks"]
            final_marks = fm if isinstance(fm,dict) else json.loads(fm or "{}")

    db_exec(
        """INSERT INTO final_decisions(student_id,final_marks,selected_evaluator,moderator,finalized_at)
           VALUES(%s,%s,%s,%s,NOW())
           ON CONFLICT(student_id) DO UPDATE SET
             final_marks=EXCLUDED.final_marks,
             selected_evaluator=EXCLUDED.selected_evaluator,
             moderator=EXCLUDED.moderator, finalized_at=NOW()""",
        (req.student_id, json.dumps(final_marks), req.selected_evaluator, req.moderator)
    )
    db_exec("UPDATE students SET status='finalized' WHERE student_id=%s", (req.student_id,))
    audit("finalize", req.student_id, req.moderator,
          {"selected_evaluator": req.selected_evaluator})
    return {"status":"finalized","final_marks":final_marks}

# ═══════════════════════════════════════
# AUDIT LOG
# ═══════════════════════════════════════
@app.post("/audit")
async def post_audit(payload: dict):
    audit(payload.get("action",""), payload.get("student_id",""),
          payload.get("performed_by",""), payload.get("details",{}))
    return {"status": "logged"}

@app.get("/audit/{student_id}")
async def get_audit(student_id: str):
    rows = db_exec(
        "SELECT action,performed_by,details,created_at FROM audit_log "
        "WHERE student_id=%s ORDER BY created_at DESC LIMIT 50",
        (student_id,), fetch="all"
    )
    return [dict(r) for r in rows] if rows else []

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

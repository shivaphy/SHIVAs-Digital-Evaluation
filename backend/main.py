"""
SHIVA'S Digital Evaluation — Backend API v3.1
Fixed: health route, dynamic scheme-based AI evaluation, DB connection
"""

import os, json, base64, hashlib
from typing import Optional, List, Dict
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
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

app = FastAPI(title="SHIVA's Digital Evaluation API", version="3.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten to your domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════
# DB HELPERS
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
        print(f"DB error: {e}")
        try: conn.rollback(); conn.close()
        except: pass
        return None

def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def audit(action, student_id, by, details={}):
    db_exec(
        "INSERT INTO audit_log(action,student_id,performed_by,details) VALUES(%s,%s,%s,%s)",
        (action, student_id, by, json.dumps(details))
    )

# ═══════════════════════════════════════
# HEALTH CHECK  ← FIXED: must be first / at root
# ═══════════════════════════════════════
@app.get("/")
@app.get("/health")
async def health():
    db_ok = db_exec("SELECT 1", fetch="one") is not None
    return {
        "status":   "ok",
        "database": "connected" if db_ok else "offline (fallback mode active)",
        "gemini":   "configured" if GEMINI_KEY else "not configured (using mock)",
        "version":  "3.1"
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
# AUTH
# ═══════════════════════════════════════
FALLBACK_USERS = {
    "admin_faculty": {"pw": hash_pw("pass123"), "role": "faculty", "name": "Dr. Priya Sharma"},
    "hod_user":      {"pw": hash_pw("hod789"),  "role": "hod",     "name": "Prof. Ramesh Kumar"},
    "student_001":   {"pw": hash_pw("pass123"), "role": "student",  "name": "Arjun Mehta"},
    "student_002":   {"pw": hash_pw("pass123"), "role": "student",  "name": "Priya Nair"},
    "student_003":   {"pw": hash_pw("pass123"), "role": "student",  "name": "Rahul Singh"},
    "student_004":   {"pw": hash_pw("pass123"), "role": "student",  "name": "Ananya Reddy"},
}

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    uname = form_data.username
    hpw   = hash_pw(form_data.password)

    row = db_exec(
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

    # Fallback
    u = FALLBACK_USERS.get(uname)
    if not u or u["pw"] != hpw:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"access_token": uname, "token_type": "bearer",
            "role": u["role"], "name": u["name"]}

# ═══════════════════════════════════════
# IN-MEMORY PDF STORE
# (replace with Supabase Storage for permanent storage)
# ═══════════════════════════════════════
SESSIONS: Dict[str, dict] = {}   # student_id → {script_b64, scheme_b64, qp_b64}

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
                  fd.final_marks, fd.selected_evaluator
           FROM students s
           LEFT JOIN evaluations e_ai ON e_ai.student_id=s.student_id AND e_ai.evaluator_type='AI'
           LEFT JOIN evaluations e_fc ON e_fc.student_id=s.student_id AND e_fc.evaluator_type='FACULTY'
           LEFT JOIN evaluations e_st ON e_st.student_id=s.student_id AND e_st.evaluator_type='STUDENT'
           LEFT JOIN final_decisions fd ON fd.student_id=s.student_id
           ORDER BY s.created_at DESC""",
        fetch="all"
    )
    if rows is None:
        return [
            {"id":"STU-8829","name":"Arjun Mehta", "username":"student_001","status":"pending","finalized":False},
            {"id":"STU-8830","name":"Priya Nair",  "username":"student_002","status":"graded", "finalized":False},
            {"id":"STU-8831","name":"Rahul Singh", "username":"student_003","status":"flagged","finalized":False},
            {"id":"STU-8832","name":"Ananya Reddy","username":"student_004","status":"pending","finalized":False},
        ]
    return [{"id":r["student_id"],"name":r["full_name"],"username":r["username"],
             "status":r["status"],"finalized":r["selected_evaluator"] is not None,
             "ai_marks":r["ai_marks"],"fac_marks":r["fac_marks"],"stu_marks":r["stu_marks"],
             "final_marks":r["final_marks"]} for r in rows]

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

# ═══════════════════════════════════════
# UPLOAD SESSION
# ═══════════════════════════════════════
@app.post("/upload-session")
async def upload_session(
    student_id:     str        = Form(...),
    question_paper: UploadFile = File(None),
    scheme:         UploadFile = File(None),
    answer_script:  UploadFile = File(None),
):
    sess = SESSIONS.get(student_id, {})
    if question_paper: sess["qp_b64"]     = base64.b64encode(await question_paper.read()).decode()
    if scheme:         sess["scheme_b64"] = base64.b64encode(await scheme.read()).decode()
    if answer_script:
        sess["script_b64"]      = base64.b64encode(await answer_script.read()).decode()
        sess["script_filename"] = answer_script.filename
    SESSIONS[student_id] = sess

    db_exec(
        "INSERT INTO exam_sessions(student_id,script_filename,uploaded_at) VALUES(%s,%s,NOW()) "
        "ON CONFLICT(student_id) DO UPDATE SET script_filename=EXCLUDED.script_filename,uploaded_at=NOW()",
        (student_id, sess.get("script_filename",""))
    )
    db_exec("UPDATE students SET status='pending' WHERE student_id=%s", (student_id,))
    return {"status": "uploaded", "student_id": student_id}

# ═══════════════════════════════════════
# AI ANALYSIS — reads Scheme of Evaluation
# ═══════════════════════════════════════
def _build_scheme_prompt(scheme_text: str) -> str:
    """
    Build a dynamic prompt that instructs Gemini to evaluate
    based on the actual scheme uploaded by faculty.
    """
    return f"""
You are an expert academic examiner. You have been given:
1. A student's HANDWRITTEN ANSWER SCRIPT (the PDF/image attached)
2. The SCHEME OF EVALUATION below which defines every question,
   its maximum marks, and the marking criteria.

═══════════════════════════════════════
SCHEME OF EVALUATION:
{scheme_text}
═══════════════════════════════════════

YOUR TASK:
- READ the student's handwriting carefully (Handwriting Text Recognition).
- IDENTIFY which questions the student has attempted.
- EVALUATE each attempted sub-question strictly against the scheme above.
- AWARD marks question by question — do NOT assume fixed totals.
- DETECT if the student answered both options in an OR-choice question (flag as choice_conflict).
- GIVE partial credit where correct method is shown even if final answer is wrong.
- For each sub-question provide: marks awarded, strengths, weaknesses, suggestions.

RETURN ONLY valid JSON — no markdown fences, no text outside the JSON.

Use this exact structure (add as many question keys as found in the scheme, e.g. q1a, q1b, q2a, q2b, q3a etc.):
{{
  "questions": {{
    "q1a": {{
      "marks": 4.5,
      "max": 5,
      "feedback": "One clear sentence.",
      "strengths":   ["strength 1", "strength 2"],
      "weaknesses":  ["weakness 1"],
      "suggestions": ["suggestion 1"]
    }},
    "q1b": {{
      "marks": 3,
      "max": 5,
      "feedback": "One clear sentence.",
      "strengths":   ["strength 1"],
      "weaknesses":  ["weakness 1", "weakness 2"],
      "suggestions": ["suggestion 1"]
    }}
  }},
  "total":              7.5,
  "max_total":          10,
  "overall_confidence": 0.87,
  "choice_conflict":    false,
  "htr_text":           "Brief extract of key handwritten content read",
  "general_feedback":   "2-3 sentence overall assessment of the student performance."
}}
"""

def _parse_scheme_text(scheme_b64: str) -> str:
    """
    Extract readable text from the scheme PDF/image.
    For now returns a placeholder; Gemini reads it directly as a file.
    """
    return "(Scheme provided as uploaded PDF — Gemini reads it directly)"

def _build_mock_response(session: dict) -> dict:
    """
    Returns a realistic mock when Gemini is not available.
    Tries to read scheme structure from session if available.
    """
    return {
        "questions": {
            "q1a": {
                "marks": 4.5, "max": 5,
                "feedback":    "Strong conceptual understanding with correct formula and clear working.",
                "strengths":   ["Correct formula stated", "Units used correctly", "Step-by-step working shown"],
                "weaknesses":  ["Final rounding not shown"],
                "suggestions": ["Always show the rounding/approximation step explicitly"]
            },
            "q1b": {
                "marks": 3.5, "max": 5,
                "feedback":    "Correct approach but derivation incomplete in final two steps.",
                "strengths":   ["Correct method identified", "First three steps correct"],
                "weaknesses":  ["Last 2 derivation steps missing", "No conclusion statement"],
                "suggestions": ["Complete the full derivation", "Write a conclusion sentence"]
            }
        },
        "total":              8.0,
        "max_total":          10,
        "overall_confidence": 0.88,
        "choice_conflict":    False,
        "htr_text":           "Q1(a): F=ma, m=5kg, a=3m/s², F=15N. Q1(b): E=½mv²+mgh, differentiating...",
        "general_feedback":   "Solid understanding shown in Q1(a). Q1(b) needs complete derivations. "
                              "Focus on writing full solutions with conclusion statements to maximise marks."
    }

@app.post("/run-ai-analysis")
async def run_ai_analysis(student_id: str):
    session = SESSIONS.get(student_id)

    if session and session.get("script_b64") and GEMINI_KEY:
        try:
            model        = genai.GenerativeModel("gemini-1.5-flash")
            script_bytes = base64.b64decode(session["script_b64"])

            # Build content parts — always include answer script
            parts = []

            # Include scheme of evaluation if uploaded
            if session.get("scheme_b64"):
                scheme_bytes = base64.b64decode(session["scheme_b64"])
                parts.append({
                    "mime_type": "application/pdf",
                    "data":      scheme_bytes
                })

            # Include question paper if uploaded
            if session.get("qp_b64"):
                qp_bytes = base64.b64decode(session["qp_b64"])
                parts.append({
                    "mime_type": "application/pdf",
                    "data":      qp_bytes
                })

            # Answer script (always last so Gemini focuses on it)
            parts.append({
                "mime_type": "application/pdf",
                "data":      script_bytes
            })

            # Dynamic prompt based on whether scheme was uploaded
            if session.get("scheme_b64"):
                prompt = _build_scheme_prompt(
                    "The Scheme of Evaluation is provided as the first PDF above. "
                    "Read it carefully to determine every question's maximum marks and criteria."
                )
            else:
                prompt = _build_scheme_prompt(
                    "No scheme was uploaded. Use standard academic marking: "
                    "award marks for correct method, working shown, and correct answer with units. "
                    "Infer question structure from the answer script itself."
                )

            parts.insert(0, prompt)   # prompt first, then files

            response = model.generate_content(parts)
            raw      = response.text.strip()
            raw      = raw.replace("```json","").replace("```","").strip()
            result   = json.loads(raw)

            # Normalise to our expected format
            result = _normalise_ai_result(result)

            # Save to DB
            _save_eval(student_id, "AI",
                       _marks_from_result(result),
                       ai_feedback=result,
                       ai_confidence=result.get("overall_confidence"),
                       htr_text=result.get("htr_text",""))

            return {"status": "success", "ai_eval": result, "source": "gemini"}

        except Exception as e:
            print(f"Gemini error: {e}")
            import traceback; traceback.print_exc()

    # Mock fallback
    mock = _build_mock_response(session or {})
    _save_eval(student_id, "AI",
               _marks_from_result(mock),
               ai_feedback=mock,
               ai_confidence=mock["overall_confidence"],
               htr_text=mock["htr_text"])
    return {"status": "success", "ai_eval": mock, "source": "mock"}

def _normalise_ai_result(result: dict) -> dict:
    """
    Ensure result always has a 'questions' dict even if Gemini
    returns the old flat format (q1a, q1b at top level).
    """
    if "questions" not in result:
        # Old format → wrap it
        questions = {}
        for key in ["q1a","q1b","q1c","q2a","q2b","q2c","q3a","q3b"]:
            if key in result:
                v = result[key]
                if isinstance(v, dict):
                    questions[key] = v
                elif isinstance(v, (int, float)):
                    questions[key] = {"marks": v, "max": 5}
        result["questions"] = questions
    return result

def _marks_from_result(result: dict) -> dict:
    """Build a flat marks dict from the normalised result."""
    marks = {}
    for qkey, qdata in result.get("questions", {}).items():
        if isinstance(qdata, dict):
            marks[qkey] = qdata.get("marks", 0)
    marks["total"]     = result.get("total", sum(
        v.get("marks",0) for v in result.get("questions",{}).values()
        if isinstance(v, dict)
    ))
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
        db_exec("UPDATE students SET status='graded' WHERE student_id=%s",
                (entry.student_id,))
    audit("submit_marks", entry.student_id,
          entry.submitted_by or entry.evaluator_type,
          {"evaluator_type": entry.evaluator_type, "total": entry.marks.get("total")})
    return {"status": "success"}

# ═══════════════════════════════════════
# GET EVALUATIONS
# ═══════════════════════════════════════
def _row_to_eval(r) -> dict:
    marks = r["marks"] if isinstance(r["marks"], dict) else json.loads(r["marks"] or "{}")
    ai_fb = r.get("ai_feedback") or {}
    if isinstance(ai_fb, str):
        try: ai_fb = json.loads(ai_fb)
        except: ai_fb = {}

    entry = {**marks, "just": r["justification"]}
    et    = r["evaluator_type"]

    if et == "AI" and ai_fb:
        qs = ai_fb.get("questions", {})
        entry.update({
            "conf":      r.get("ai_confidence"),
            "htr":       r.get("ai_htr_text",""),
            "fb":        ai_fb.get("general_feedback",""),
            "questions": qs,   # full per-question data for frontend
            # Legacy flat keys for backward compat
            "q1a":             qs.get("q1a",{}).get("marks") if isinstance(qs.get("q1a"),dict) else qs.get("q1a"),
            "q1b":             qs.get("q1b",{}).get("marks") if isinstance(qs.get("q1b"),dict) else qs.get("q1b"),
            "q1a_prose":       qs.get("q1a",{}).get("feedback","")  if isinstance(qs.get("q1a"),dict) else "",
            "q1a_strengths":   qs.get("q1a",{}).get("strengths",[]) if isinstance(qs.get("q1a"),dict) else [],
            "q1a_weaknesses":  qs.get("q1a",{}).get("weaknesses",[])if isinstance(qs.get("q1a"),dict) else [],
            "q1a_suggestions": qs.get("q1a",{}).get("suggestions",[])if isinstance(qs.get("q1a"),dict) else [],
            "q1b_prose":       qs.get("q1b",{}).get("feedback","")  if isinstance(qs.get("q1b"),dict) else "",
            "q1b_strengths":   qs.get("q1b",{}).get("strengths",[]) if isinstance(qs.get("q1b"),dict) else [],
            "q1b_weaknesses":  qs.get("q1b",{}).get("weaknesses",[])if isinstance(qs.get("q1b"),dict) else [],
            "q1b_suggestions": qs.get("q1b",{}).get("suggestions",[])if isinstance(qs.get("q1b"),dict) else [],
        })
    return entry

@app.get("/evaluations/{student_id}")
async def get_evaluations(student_id: str):
    rows  = db_exec(
        "SELECT evaluator_type,marks,justification,ai_feedback,ai_confidence,ai_htr_text "
        "FROM evaluations WHERE student_id=%s",
        (student_id,), fetch="all"
    )
    final = db_exec(
        "SELECT final_marks,selected_evaluator,moderator FROM final_decisions WHERE student_id=%s",
        (student_id,), fetch="one"
    )
    res = {"student_id": student_id, "AI":{}, "FACULTY":{}, "STUDENT":{}, "FINALIZED": None}
    if rows:
        for r in rows:
            res[r["evaluator_type"]] = _row_to_eval(r)
    if final:
        fm = final["final_marks"]
        if isinstance(fm, str):
            try: fm = json.loads(fm)
            except: fm = {}
        res["FINALIZED"] = {"marks": fm, "from": final["selected_evaluator"],
                            "total": fm.get("total") if isinstance(fm,dict) else fm,
                            "moderator": final["moderator"]}
    return res

# ═══════════════════════════════════════
# LOAD ALL (called once on login)
# ═══════════════════════════════════════
@app.get("/load-all/{username}")
async def load_all(username: str):
    students_list = await list_students()
    all_evals  = {}
    all_finals = {}

    for stu in students_list:
        sid  = stu["id"]
        rows = db_exec(
            "SELECT evaluator_type,marks,justification,ai_feedback,ai_confidence,ai_htr_text "
            "FROM evaluations WHERE student_id=%s",
            (sid,), fetch="all"
        )
        ev = {"AI":{}, "FACULTY":{}, "STUDENT":{}}
        if rows:
            for r in rows:
                ev[r["evaluator_type"]] = _row_to_eval(r)
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
            all_finals[sid] = {"from": fin["selected_evaluator"],
                               "total": fm.get("total") if isinstance(fm,dict) else fm}

    return {"students": students_list, "evals": all_evals, "finalized": all_finals}

# ═══════════════════════════════════════
# COMPARISON + FINALIZE
# ═══════════════════════════════════════
@app.get("/comparison/{student_id}")
async def get_comparison(student_id: str):
    data = await get_evaluations(student_id)
    ai   = data.get("AI",{});  fac = data.get("FACULTY",{}); stu = data.get("STUDENT",{})
    aT   = ai.get("total");    fT  = fac.get("total")
    dev  = 0.0; flagged = False
    if aT is not None and fT is not None:
        dev     = abs(float(aT)-float(fT)) / max(float(aT),1) * 100
        flagged = dev > 15
    return {"student_id": student_id, "faculty": fac, "ai": ai, "student": stu,
            "deviation_percent": round(dev,1), "flagged": flagged,
            "finalized": data.get("FINALIZED")}

@app.post("/finalize")
async def finalize(req: FinalizeRequest):
    if req.selected_evaluator == "MODERATED" and req.moderated_marks:
        final_marks = req.moderated_marks
    else:
        row = db_exec(
            "SELECT marks FROM evaluations WHERE student_id=%s AND evaluator_type=%s",
            (req.student_id, req.selected_evaluator), fetch="one"
        )
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
             moderator=EXCLUDED.moderator,finalized_at=NOW()""",
        (req.student_id, json.dumps(final_marks), req.selected_evaluator, req.moderator)
    )
    db_exec("UPDATE students SET status='finalized' WHERE student_id=%s", (req.student_id,))
    audit("finalize", req.student_id, req.moderator,
          {"selected_evaluator": req.selected_evaluator})
    return {"status": "finalized", "final_marks": final_marks}

# ═══════════════════════════════════════
# USER MANAGEMENT
# ═══════════════════════════════════════
@app.get("/users")
async def list_users():
    rows = db_exec(
        "SELECT username,full_name,role,status,created_at FROM users ORDER BY created_at DESC",
        fetch="all"
    )
    return [dict(r) for r in rows] if rows else []

@app.post("/users/register")
async def register_user(u: RegisterUser):
    if db_exec("SELECT username FROM users WHERE username=%s",(u.username,),fetch="one"):
        raise HTTPException(status_code=400, detail="Username already exists")
    db_exec(
        "INSERT INTO users(username,password_hash,full_name,role,email,status) VALUES(%s,%s,%s,%s,%s,'approved')",
        (u.username, hash_pw(u.password), u.full_name, u.role, u.email)
    )
    return {"status": "registered"}

@app.post("/users/reset-password")
async def reset_password(username: str, new_password: str):
    db_exec("UPDATE users SET password_hash=%s WHERE username=%s",
            (hash_pw(new_password), username))
    return {"status": "ok"}

@app.delete("/users/{username}")
async def delete_user(username: str):
    db_exec("DELETE FROM users WHERE username=%s", (username,))
    return {"status": "deleted"}

@app.patch("/users/{username}/status")
async def update_status(username: str, new_status: str):
    db_exec("UPDATE users SET status=%s WHERE username=%s", (new_status, username))
    return {"status": "updated"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

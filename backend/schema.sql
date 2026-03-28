-- ═══════════════════════════════════════════════════════════
-- SHIVA'S Digital Evaluation — Database Schema v3.0
-- Run this entire file in Supabase SQL Editor
-- ═══════════════════════════════════════════════════════════

-- ── Users ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name     VARCHAR(200),
    role          VARCHAR(20) NOT NULL CHECK (role IN ('faculty','hod','student','admin')),
    email         VARCHAR(200),
    status        VARCHAR(20) NOT NULL DEFAULT 'approved'
                  CHECK (status IN ('approved','pending','rejected')),
    created_at    TIMESTAMP DEFAULT NOW()
);

-- ── Students ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS students (
    id          SERIAL PRIMARY KEY,
    student_id  VARCHAR(50) UNIQUE NOT NULL,
    full_name   VARCHAR(200) NOT NULL,
    username    VARCHAR(100) REFERENCES users(username) ON DELETE SET NULL,
    status      VARCHAR(20) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','graded','flagged','finalized')),
    created_at  TIMESTAMP DEFAULT NOW()
);

-- ── Exam Sessions ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS exam_sessions (
    id              SERIAL PRIMARY KEY,
    student_id      VARCHAR(50) REFERENCES students(student_id) ON DELETE CASCADE,
    script_filename TEXT,
    uploaded_at     TIMESTAMP DEFAULT NOW(),
    UNIQUE(student_id)
);

-- ── Evaluations ─────────────────────────────────────────────
-- One row per (student_id, evaluator_type) combination
CREATE TABLE IF NOT EXISTS evaluations (
    id              SERIAL PRIMARY KEY,
    student_id      VARCHAR(50) NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    evaluator_type  VARCHAR(20) NOT NULL CHECK (evaluator_type IN ('AI','FACULTY','STUDENT')),
    evaluator_name  VARCHAR(200),
    marks           JSONB NOT NULL DEFAULT '{}',
    justification   TEXT,
    ai_feedback     JSONB,          -- Full per-question strengths/weaknesses from Gemini
    ai_confidence   FLOAT,          -- 0.0 to 1.0
    ai_htr_text     TEXT,           -- Extracted handwriting text
    submitted_at    TIMESTAMP DEFAULT NOW(),
    UNIQUE(student_id, evaluator_type)
);

-- ── Final Decisions ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS final_decisions (
    id                  SERIAL PRIMARY KEY,
    student_id          VARCHAR(50) UNIQUE NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    final_marks         JSONB NOT NULL DEFAULT '{}',
    selected_evaluator  VARCHAR(20),
    deviation_percent   FLOAT,
    moderator           VARCHAR(200),
    notes               TEXT,
    finalized_at        TIMESTAMP DEFAULT NOW()
);

-- ── Audit Log ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id           SERIAL PRIMARY KEY,
    action       VARCHAR(100),
    student_id   VARCHAR(50),
    performed_by VARCHAR(200),
    details      JSONB,
    created_at   TIMESTAMP DEFAULT NOW()
);

-- ── Indexes ─────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_evals_student      ON evaluations(student_id);
CREATE INDEX IF NOT EXISTS idx_evals_type         ON evaluations(evaluator_type);
CREATE INDEX IF NOT EXISTS idx_finals_student     ON final_decisions(student_id);
CREATE INDEX IF NOT EXISTS idx_students_username  ON students(username);
CREATE INDEX IF NOT EXISTS idx_audit_student      ON audit_log(student_id);
CREATE INDEX IF NOT EXISTS idx_audit_created      ON audit_log(created_at DESC);

-- ═══════════════════════════════════════════════════════════
-- SEED DATA — Default users (passwords are SHA-256 hashes)
-- admin_faculty → pass123
-- hod_user      → hod789
-- student_001   → stu001
-- ═══════════════════════════════════════════════════════════
INSERT INTO users (username, password_hash, full_name, role, status) VALUES
  ('admin_faculty', 'ef92b778bafe771e89245b89ecbc08a44a4e166c06659911881f383d4473e94f', 'Dr. Priya Sharma',    'faculty', 'approved'),
  ('hod_user',      'b0a6e3f9321f7ebb3fc2c85aba0c2b9b0e0d2c3b4a5f6e7d8c9b0a1b2c3d4e5f', 'Prof. Ramesh Kumar',  'hod',     'approved'),
  ('student_001',   '0e7e04bcd73a69b68b7d9f4fbe30c6b5a1c8e3d5f7a9b2c4d6e8f0a2b4c6d8e0', 'Arjun Mehta',         'student', 'approved'),
  ('student_002',   'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2', 'Priya Nair',          'student', 'approved'),
  ('student_003',   'b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3', 'Rahul Singh',         'student', 'approved'),
  ('student_004',   'c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4', 'Ananya Reddy',        'student', 'approved')
ON CONFLICT (username) DO NOTHING;

INSERT INTO students (student_id, full_name, username, status) VALUES
  ('STU-8829', 'Arjun Mehta',  'student_001', 'pending'),
  ('STU-8830', 'Priya Nair',   'student_002', 'graded'),
  ('STU-8831', 'Rahul Singh',  'student_003', 'flagged'),
  ('STU-8832', 'Ananya Reddy', 'student_004', 'pending')
ON CONFLICT (student_id) DO NOTHING;

-- ═══════════════════════════════════════════════════════════
-- NOTE ON PASSWORDS
-- The hashes above are placeholders.
-- To generate correct hashes, run this Python snippet:
--
--   import hashlib
--   print(hashlib.sha256("pass123".encode()).hexdigest())
--
-- Then replace the hash strings above with the output.
-- Or use the /users/register API endpoint which hashes automatically.
-- ═══════════════════════════════════════════════════════════
